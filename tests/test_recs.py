"""Recommendations: similar artists from ListenBrainz (+ Last.fm), ranked across
everything you follow, with add / follow / not interested.

Run:  python3 tests/test_recs.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.update({"CRATE_TICK_SECONDS": "3600", "CRATE_MB_INTERVAL": "0", "CRATE_UPDATE_CHECKS": "false",
                   "CRATE_RECS_INTERVAL": "0"})

import mock_services  # noqa: E402

tmp = tempfile.mkdtemp(prefix="crate-recs-")
os.makedirs(os.path.join(tmp, "music"))
srv, state = mock_services.start(os.path.join(tmp, "dl"))
mu = f"http://127.0.0.1:{srv.server_address[1]}"
os.environ.update({"CRATE_LISTENBRAINZ_URL": mu, "CRATE_LASTFM_URL": mu + "/2.0/", "CRATE_MB_URL": mu + "/ws/2",
                   "CRATE_WIKIDATA_URL": mu + "/w/api.php", "CRATE_LIBRARY_DIR": os.path.join(tmp, "music")})

from http.server import ThreadingHTTPServer  # noqa: E402
from crate.core import App  # noqa: E402
from crate.db import now_iso  # noqa: E402
from crate.server import make_handler  # noqa: E402
from client import login_opener  # noqa: E402

app = App(os.path.join(tmp, "crate.db"))
httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"
ADMIN = login_opener(base)
failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def call(op, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with op.open(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        return {"_status": e.code, **(json.loads(raw) if raw else {})}


def api(method, path, body=None):
    return call(ADMIN, method, path, body)


def artist(mbid, name, followed=1):
    return app.db.x("INSERT INTO artists (mbid, name, sort_name, added_at, sync_status, last_synced, followed) "
                    "VALUES (?,?,?,?, 'ok', '2099-01-01', ?)", (mbid, name, name, now_iso(), followed))


def refresh_all():
    while app.recs.refresh_one():
        pass


print("== nothing followed yet")
d = api("GET", "/api/recommendations")
check(d["enabled"] and d["items"] == [] and d["progress"]["total"] == 0, "empty, enabled")

print("== similar artists from ListenBrainz")
band = artist("a-1", "Test Band")
jazz = artist("a-2", "Test Band (jazz)")
p = api("GET", "/api/recommendations")["progress"]
check(p == {"checked": 0, "total": 2, "error": None}, f"progress before fetching ({p})")
refresh_all()
check(sorted(m for m, _ in state.lb_calls) == ["a-1", "a-2"], f"asked ListenBrainz once per followed artist ({state.lb_calls})")
check(all(a.startswith("session_based_days_") for _, a in state.lb_calls), "with the session-based algorithm")
d = api("GET", "/api/recommendations")
names = [r["name"] for r in d["items"]]
check(d["progress"]["checked"] == 2, "progress: all checked")
check(names[0] == "Glass Harbour", f"similar to both of your artists ranks first ({names})")
check(set(names) == {"Glass Harbour", "Night Owls", "Paper Kites Club", "Velvet Static"}, "everything similar listed")
gh = d["items"][0]
check(set(gh["because"]) == {"Test Band", "Test Band (jazz)"} and gh["seeds"] == 2, f"says why ({gh['because']})")
check(names.index("Night Owls") < names.index("Paper Kites Club"), "more similar ranks higher")
check(not app.recs.refresh_one(), "nothing more to fetch (cached)")

print("== an artist you follow is never recommended")
owl = artist("a-3", "Night Owls")
d = api("GET", "/api/recommendations")
check("Night Owls" not in [r["name"] for r in d["items"]], "followed artist removed")
check(d["progress"]["checked"] == 2 and d["progress"]["total"] == 3, "new artist waiting to be checked")
refresh_all()
check(state.lb_calls[-1][0] == "a-3", "newly followed artist fetched next")

print("== request-only artists show as Follow")
app.db.x("UPDATE artists SET followed = 0 WHERE id = ?", (owl,))
d = api("GET", "/api/recommendations")
o = next(r for r in d["items"] if r["name"] == "Night Owls")
check(o["artist_id"] == owl, "recommended with its Crate artist id")

print("== add from recommendations")
VIEWER = None
r = api("POST", "/api/artists", {"mbid": "r-3", "monitor": "new"})
check(r.get("created") and r["artist"]["monitor"] == "new", "added (new releases only)")
d = api("GET", "/api/recommendations")
check("Velvet Static" not in [x["name"] for x in d["items"]], "added artist drops off the list")
r = api("POST", "/api/artists", {"mbid": "merged-r-3"})
check(r.get("created") is False and r["artist"]["mbid"] == "r-3",
      "an old (merged) MusicBrainz id resolves to the artist you already have")

print("== not interested")
check(api("POST", "/api/recommendations/dismiss", {"mbid": "r-2"}).get("ok"), "dismissed")
d = api("GET", "/api/recommendations")
check("Paper Kites Club" not in [x["name"] for x in d["items"]], "dismissed artist hidden")
api("POST", "/api/users", {"username": "viewer", "password": "viewer-pass-123", "role": "user",
                           "perms": {"artists": False, "downloads": False}})
VIEWER = login_opener(base, "viewer", "viewer-pass-123")
check(isinstance(call(VIEWER, "GET", "/api/recommendations").get("items"), list), "users can see recommendations")
check(call(VIEWER, "POST", "/api/recommendations/dismiss", {"mbid": "r-1"}).get("_status") == 403,
      "users without 'follow artists' can't dismiss")
check(call(VIEWER, "POST", "/api/recommendations/reset").get("_status") == 403, "reset is admin-only")
api("POST", "/api/recommendations/reset")
check("Paper Kites Club" in [x["name"] for x in api("GET", "/api/recommendations")["items"]], "reset brings it back")

print("== similar artists on an artist's page")
sim = api("GET", f"/api/artists/{band}/similar")
by = {x["name"]: x for x in sim["items"]}
check(sim["fetched"] and list(by)[:1] == ["Night Owls"], f"in order ({list(by)})")
check(by["Night Owls"]["artist_id"] == owl and not by["Night Owls"]["followed"], "marks artists already in Crate")
check(by["Velvet Static"]["followed"] if "Velvet Static" in by else True, "followed flag")

print("== Last.fm blended in when there's a key")
api("PUT", "/api/settings", {"lastfm_api_key": "lfm-key"})
check(app.recs.progress()["checked"] == 0, "changing the key re-checks every artist")
refresh_all()
names = [x["name"] for x in api("GET", "/api/recommendations")["items"]]
check("Lantern Choir" in names, f"Last.fm's suggestion added ({names})")
check("No MBID Band" not in names, "Last.fm artists without a MusicBrainz id skipped")
check(any(c.get("mbid") == "a-1" for c in state.lastfm_calls), "Last.fm asked by MBID")
check(api("GET", "/api/settings")["lastfm_api_key"].startswith("•"), "key hidden in settings")
d = api("GET", "/api/recommendations")
check(d["lastfm"] is True, "reports Last.fm in use")

print("== a service outage doesn't break anything")
api("PUT", "/api/settings", {"lastfm_api_key": ""})
state.lb_fail = True
app.recs.refresh_one()
p = app.recs.progress()
check(p["error"] and "ListenBrainz" in p["error"], f"error reported ({p['error']})")
check(not app.recs.refresh_one(), "backs off instead of retrying straight away")
check(len(api("GET", "/api/recommendations")["items"]) > 0, "earlier recommendations still shown")
state.lb_fail = False
app.recs._next = 0
refresh_all()
check(app.recs.progress()["error"] is None, "recovers")

print("== turned off")
api("PUT", "/api/settings", {"recommendations": "false"})
d = api("GET", "/api/recommendations")
check(d["enabled"] is False and d["items"] == [], "no recommendations when off")
check(not app.recs.refresh_one(), "nothing fetched when off")
api("PUT", "/api/settings", {"recommendations": "true"})

print("== caching")
with ADMIN.open(urllib.request.Request(base + "/api/recommendations")) as resp:
    etag = resp.headers.get("ETag")
check(call(ADMIN, "GET", "/api/recommendations", headers={"If-None-Match": etag}).get("_status") == 304, "unchanged → 304")
api("POST", "/api/recommendations/dismiss", {"mbid": "r-1"})
check("items" in call(ADMIN, "GET", "/api/recommendations", headers={"If-None-Match": etag}), "after a change → 200")

print("== background loop")
state.lb_calls.clear()
app.db.x("UPDATE similar SET fetched_at = '2000-01-01T00:00:00+00:00'")
stop = threading.Event()
threading.Thread(target=app.recs.loop, args=(stop,), daemon=True).start()
end = time.time() + 10
while time.time() < end and app.recs.progress()["checked"] < app.recs.progress()["total"]:
    time.sleep(0.1)
stop.set()
app.recs.wake.set()
check(app.recs.progress()["checked"] == app.recs.progress()["total"], "refreshes stale artists by itself")

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f_ in failures:
        print("  - " + f_)
    sys.exit(1)
print("ALL PASSED")
