"""Follow rules: per-artist release types, live/compilations, auto-download,
"new releases only", one-off album requests, and following later.

Run:  python3 tests/test_rules.py
"""
import datetime
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
os.environ.update({"CRATE_TICK_SECONDS": "0.3", "CRATE_MB_INTERVAL": "0", "CRATE_UPDATE_CHECKS": "false"})

import mock_services  # noqa: E402

tmp = tempfile.mkdtemp(prefix="crate-rules-")
music = os.path.join(tmp, "music")
for i in range(1, 4):
    p = os.path.join(music, "Test Band", "Owned Already (2010)", f"0{i} t.flac")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(b"\0" * 1000)

srv, state = mock_services.start(os.path.join(tmp, "dl"))
mock_url = f"http://127.0.0.1:{srv.server_address[1]}"
os.environ.update({"CRATE_LIBRARY_DIR": music, "CRATE_MB_URL": mock_url + "/ws/2",
                   "CRATE_WIKIDATA_URL": mock_url + "/w/api.php"})
# slskd not configured: wanted albums stay wanted, which keeps states easy to check.

from http.server import ThreadingHTTPServer  # noqa: E402
from crate.core import App  # noqa: E402
from crate.server import make_handler  # noqa: E402
from client import login_opener  # noqa: E402

app = App(os.path.join(tmp, "crate.db"))
threading.Thread(target=app.run_worker, daemon=True).start()
httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"
ADMIN = login_opener(base)
TODAY = datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def call(opener, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with opener.open(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        return {"_status": e.code, **(json.loads(raw) if raw else {})}


def api(method, path, body=None):
    return call(ADMIN, method, path, body)


failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def wait(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def albums(artist_id):
    return {a["title"]: a for a in api("GET", f"/api/artists/{artist_id}")["albums"]}


def st(artist_id):
    return {t: (a["status"], a.get("skip_reason")) for t, a in albums(artist_id).items()}


api("PUT", "/api/settings", {"auto_download": "false"})

print("== follow with the global rules")
r = api("POST", "/api/artists", {"mbid": "a-1"})
band = r["artist"]["id"]
check(wait(lambda: api("GET", f"/api/artists/{band}")["artist"]["sync_status"] == "ok"), "discography fetched")
s = st(band)
check(s["Live at Home"] == ("skipped", "type") and s["Hit"] == ("skipped", "type"), "live album and single skipped by type")
check(s["First Light"] == ("missing", None) and s["Small Things"] == ("missing", None), "albums and EPs missing")
check(s["Owned Already"][0] == "have", "owned album found in library")

api("POST", f"/api/albums/{albums(band)['Rare Gem']['id']}/skip")
api("POST", f"/api/albums/{albums(band)['First Light']['id']}/download")
s = st(band)
check(s["Rare Gem"] == ("skipped", "user"), "skip by hand records skip_reason=user")
check(s["First Light"][0] == "wanted", "First Light requested")

print("== per-artist release types + live/compilations")
r = api("PATCH", f"/api/artists/{band}", {"release_types": "album,ep,single", "include_secondary": True})
check(r.get("changed") == 2, f"two releases re-sorted ({r.get('changed')})")
s = st(band)
check(s["Hit"] == ("missing", None), "single now followed")
check(s["Live at Home"] == ("missing", None), "live album now followed")
check(s["Rare Gem"] == ("skipped", "user"), "hand-skipped album left alone")
check(s["First Light"][0] == "wanted", "requested album left alone")
a = api("GET", f"/api/artists/{band}")["artist"]
check(a["release_types"] == "album,ep,single" and a["include_secondary"] == 1, "rules saved on the artist")

print("== new releases only")
api("PATCH", f"/api/artists/{band}", {"monitor": "new"})
s = st(band)
a = api("GET", f"/api/artists/{band}")["artist"]
check(a["monitor"] == "new" and a["monitor_since"] == TODAY, "monitor=new since today")
check(all(s[t] == ("skipped", "before_follow") for t in ("Second Wind (Deluxe Edition)", "Small Things", "Hit", "Live at Home", "Flaky Connection")),
      "older missing albums skipped (before_follow)")
check(s["Not Out Yet"] == ("missing", None), "upcoming release still followed")
check(s["First Light"][0] == "wanted" and s["Rare Gem"] == ("skipped", "user") and s["Owned Already"][0] == "have",
      "requested / hand-skipped / owned untouched")
api("PATCH", f"/api/artists/{band}", {"monitor": "all"})
s = st(band)
check(s["Small Things"] == ("missing", None) and s["Hit"] == ("missing", None), "back to the whole discography")
check(api("GET", f"/api/artists/{band}")["artist"]["monitor"] == "all", "monitor=all saved")

print("== per-artist auto-download")
api("PATCH", f"/api/artists/{band}", {"auto_download": True})
s = st(band)
check(s["Small Things"][0] == "wanted" and s["Hit"][0] == "wanted", "auto-download on → missing albums queued")
api("PATCH", f"/api/artists/{band}", {"auto_download": False})
s = st(band)
check(s["Small Things"] == ("missing", None), "auto-download off → unsearched albums back to missing")
check(s["First Light"][0] == "wanted", "explicit request still queued")

print("== back to global rules; global settings re-sort")
api("PATCH", f"/api/artists/{band}", {"release_types": None, "include_secondary": None, "auto_download": None})
a = api("GET", f"/api/artists/{band}")["artist"]
check(a["release_types"] is None and a["include_secondary"] is None and a["auto_download"] is None, "rules cleared")
s = st(band)
check(s["Hit"] == ("skipped", "type") and s["Live at Home"] == ("skipped", "type"), "single + live skipped again")
api("PUT", "/api/settings", {"release_types": "album"})
check(wait(lambda: st(band)["Small Things"] == ("skipped", "type")), "global change: EP now skipped")
api("PUT", "/api/settings", {"release_types": "album,ep"})
check(wait(lambda: st(band)["Small Things"] == ("missing", None)), "global change back: EP missing again")
bad = api("PATCH", f"/api/artists/{band}", {"monitor": "sometimes"})
check(bad.get("_status") == 400, "invalid monitor rejected")

print("== add with 'only new releases'")
r = api("POST", "/api/artists", {"mbid": "a-2", "monitor": "new"})
jazz = r["artist"]["id"]
check(wait(lambda: api("GET", f"/api/artists/{jazz}")["artist"]["sync_status"] == "ok"), "synced")
check(st(jazz)["Late Night Standards"] == ("skipped", "before_follow"), "2017 album skipped (before you followed)")

print("== users")
for name, perms, limit in (("jamie", {"artists": True, "downloads": True}, 1),
                           ("kim", {"artists": True, "downloads": True}, 0),
                           ("viewer", {"artists": False, "downloads": False}, 0)):
    u = api("POST", "/api/users", {"username": name, "password": name + "-pass-123", "role": "user",
                                   "perms": perms, "daily_limit": limit})
    check(u.get("username") == name, f"created {name}")
JAMIE = login_opener(base, "jamie", "jamie-pass-123")
KIM = login_opener(base, "kim", "kim-pass-123")
VIEWER = login_opener(base, "viewer", "viewer-pass-123")
check(call(JAMIE, "PATCH", f"/api/artists/{band}", {"monitor": "new"}).get("_status") == 403, "rules are admin-only")

print("== one-off album request")
hits = call(JAMIE, "GET", "/api/mb/albums?q=dawn")
dawn = next((h for h in hits if h["mbid"] == "rg-owl2"), None)
check(dawn and dawn["artist_name"] == "Night Owls" and dawn["artist_id"] is None and dawn["album_id"] is None,
      "album search finds Dawn Chorus by Night Owls (not in Crate yet)")
check(call(VIEWER, "POST", "/api/albums/request", {"mbid": "rg-owl2"}).get("_status") == 403,
      "users without download permission can't request")
r = call(JAMIE, "POST", "/api/albums/request", {"mbid": "rg-owl2"})
check(r.get("created") and r["album"]["status"] == "wanted" and r["album"]["requested_by"] == "jamie",
      f"requested → wanted, by jamie ({r.get('album', {}).get('status')})")
check(r.get("remaining") == 0, "counts towards the daily limit")
owl = r["album"]["artist_id"]
oa = api("GET", f"/api/artists/{owl}")["artist"]
check(oa["followed"] == 0 and oa["name"] == "Night Owls", "artist created but not followed")
check(owl not in [x["id"] for x in api("GET", "/api/artists")], "unfollowed artist not on the Artists page")
check(api("GET", "/api/status")["artists"] == 2, "status counts followed artists only")
reqs = api("GET", "/api/requests")
check([x["title"] for x in reqs] == ["Dawn Chorus"], "listed under requested albums")
again = call(JAMIE, "POST", "/api/albums/request", {"mbid": "rg-owl2"})
check(again.get("created") is False and again["album"]["status"] == "wanted", "requesting it again is a no-op")
lim = call(JAMIE, "POST", "/api/albums/request", {"mbid": "rg-owl1"})
check(lim.get("_status") == 429, "daily limit stops a second request")
check(app.db.one("SELECT COUNT(*) n FROM albums WHERE mbid = 'rg-owl1'")["n"] == 0, "nothing created when limited")
r = api("POST", "/api/albums/request", {"mbid": "rg-owl3"})
check(r.get("created") and r["album"]["artist_id"] == owl, "admin requests another album by the same artist")
api("POST", f"/api/artists/{owl}/sync")
time.sleep(1)
app._periodic_sync()
time.sleep(0.5)
check(sorted(albums(owl)) == ["Dawn Chorus", "Sunrise"], "unfollowed artist's discography isn't pulled in")
hits = call(KIM, "GET", "/api/mb/albums?q=dawn")
check(next(h for h in hits if h["mbid"] == "rg-owl2")["status"] == "wanted", "search shows it's already queued")

print("== follow later")
r = call(KIM, "POST", f"/api/artists/{owl}/follow", {"monitor": "new"})
check(r.get("changed") and r["artist"]["followed"] == 1, "kim follows Night Owls (new releases only)")
check(wait(lambda: "Midnight Hours" in albums(owl) and api("GET", f"/api/artists/{owl}")["artist"]["sync_status"] == "ok"),
      "discography fetched after following")
s = st(owl)
check(s["Midnight Hours"] == ("skipped", "before_follow"), "older album skipped (before you followed)")
check(s["Dawn Chorus"][0] == "wanted" and s["Sunrise"][0] == "wanted", "requested albums kept")
check(owl in [x["id"] for x in api("GET", "/api/artists")], "now on the Artists page")
check(api("GET", "/api/requests") == [], "no longer under requested albums")
mid = albums(owl)["Midnight Hours"]["id"]
r = call(KIM, "POST", f"/api/albums/{mid}/download")
check(r.get("status") == "wanted" and r.get("skip_reason") is None, "a user can still download an older album")
check(call(KIM, "POST", f"/api/albums/{mid}/skip").get("_status") == 403, "users still can't skip")

print("== request on a followed artist")
r = api("POST", "/api/albums/request", {"mbid": "rg-jazz"})  # already exists (followed) → just queued
check(r["album"]["status"] == "wanted", "request on a followed artist's album just queues it")

print("== HTTP caching")
req = urllib.request.Request(base + "/api/artists")
with ADMIN.open(req) as resp:
    etag = resp.headers.get("ETag")
check(bool(etag), "ETag sent")
r = call(ADMIN, "GET", "/api/artists", headers={"If-None-Match": etag})
check(r.get("_status") == 304, "unchanged → 304")
api("POST", f"/api/albums/{albums(band)['Hit']['id']}/download")
r = call(ADMIN, "GET", "/api/artists", headers={"If-None-Match": etag})
check(isinstance(r, list), "after a change → 200 with the list")
with ADMIN.open(urllib.request.Request(base + "/api/artists")) as resp:
    etag = resp.headers.get("ETag")
r = call(JAMIE, "GET", "/api/artists", headers={"If-None-Match": etag})
check(isinstance(r, list), "ETag is per user (another user's ETag doesn't match)")

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f_ in failures:
        print("  - " + f_)
    sys.exit(1)
print("ALL PASSED")
