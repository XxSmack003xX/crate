"""End-to-end test: add an artist through the HTTP API and let the worker
search, download (from the fake slskd) and import everything.

Run:  python3 tests/test_e2e.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

os.environ["CRATE_TICK_SECONDS"] = "0.3"
os.environ["CRATE_MB_INTERVAL"] = "0"
os.environ["CRATE_SEARCH_SPACING"] = "0"


import mock_services  # noqa: E402

tmp = tempfile.mkdtemp(prefix="crate-test-")
downloads = os.path.join(tmp, "downloads")
music = os.path.join(tmp, "music")
os.makedirs(downloads)
owned = os.path.join(music, "Test Band", "Owned Already (2010)")
os.makedirs(owned)
open(os.path.join(owned, "01 One.flac"), "wb").close()

srv, state = mock_services.start(downloads)
mock_url = f"http://127.0.0.1:{srv.server_address[1]}"
os.environ["CRATE_WIKIDATA_URL"] = mock_url + "/w/api.php"

os.environ.update({
    "CRATE_SLSKD_URL": mock_url,
    "CRATE_SLSKD_API_KEY": mock_services.API_KEY,
    "CRATE_SLSKD_DOWNLOADS_DIR": downloads,
    "CRATE_LIBRARY_DIR": music,
    "CRATE_MB_URL": mock_url + "/ws/2",
    "CRATE_SEARCH_TIMEOUT": "2",
})

from http.server import ThreadingHTTPServer  # noqa: E402
from crate.core import App  # noqa: E402
from crate.server import make_handler  # noqa: E402

app = App(os.path.join(tmp, "crate.db"))
threading.Thread(target=app.run_worker, daemon=True).start()
httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


print("== status / settings")
st = api("GET", "/api/status")
check(st["configured"], "configured from env")
t = api("POST", "/api/settings/test", {})
check(t["slskd"]["ok"] and t["slskd"]["logged_in"], "slskd connection test passes")
check(t["library_dir"]["exists"], "library dir detected")
s = api("GET", "/api/settings")
check(s["slskd_api_key"].startswith("•"), "API key is masked in settings")

print("== artist search + add")
res = api("GET", "/api/mb/artists?q=test%20band")
check(res and res[0]["name"] == "Test Band", "MusicBrainz artist search")
check(len(res) == 2 and res[1]["disambiguation"], "same-named artists both listed")

print("== artist pictures")
info = api("GET", f"/api/mb/artists/{res[0]['mbid']}/info")
check(info["image"] == "https://commons.wikimedia.org/wiki/Special:FilePath/Test_Band_live_at_Brixton_2003.jpg?width=320",
      f"photo found via MusicBrainz -> Wikidata ({info['image']})")
check(info["albums"][0]["title"] == "First Light", f"notable releases listed, real albums first ({info['albums'][0]['title']})")
check("rg-live" not in info["covers"][:3], "fallback covers prefer studio albums")
info2 = api("GET", f"/api/mb/artists/{res[1]['mbid']}/info")
check(info2["image"] is None and info2["covers"] == ["rg-jazz"], "artist without photo falls back to album art")
calls = len(state.wikidata_calls)
api("GET", f"/api/mb/artists/{res[0]['mbid']}/info")
check(len(state.wikidata_calls) == calls, "artist info is cached")

print("== slskd not logged in to Soulseek")
state.logged_in = False
added = api("POST", "/api/artists", {"mbid": res[0]["mbid"]})
time.sleep(3)
st = api("GET", "/api/status")
check(not st["slskd"]["ok"] and "logged in" in st["slskd"]["message"], "status reports Soulseek disconnected")
check(state.search_posts == 0, f"no searches sent while disconnected ({state.search_posts})")

print("== slskd rejects searches (HTTP 400)")
state.logged_in = True
state.reject_searches = True
time.sleep(3)
check(state.search_posts == 1, f"one failed attempt, then paused instead of retrying every tick ({state.search_posts})")
check(not state.searches, "failed search removed from slskd's list")
failed = [a for a in api("GET", f"/api/artists/{added['artist']['id']}")["albums"]
          if "rejected the search" in (a["status_detail"] or "")]
check(len(failed) == 1 and failed[0]["status"] == "wanted" and failed[0]["next_retry"],
      "album stays wanted with a retry time")
ev = api("GET", "/api/activity")
check(any("back" in e["message"] for e in ev), "reconnect logged in activity")
# Let time "pass": clear the pause and the album's retry time.
state.reject_searches = False
app.search_pause_until = 0
app.search_failures = 0
app.db.x("UPDATE albums SET next_retry = NULL WHERE status = 'wanted'")

check(added["created"], "artist created")
aid = added["artist"]["id"]
dup = api("POST", "/api/artists", {"mbid": res[0]["mbid"]})
check(not dup["created"], "adding twice is a no-op")

expected = {
    "First Light": "have",
    "Second Wind (Deluxe Edition)": "have",
    "Small Things": "have",
    "Live at Home": "skipped",
    "Hit": "skipped",
    "Owned Already": "have",
    "Rare Gem": "not_found",
    "Flaky Connection": "have",
    "Not Out Yet": "wanted",
}

deadline = time.time() + 120
albums = {}
while time.time() < deadline:
    data = api("GET", f"/api/artists/{aid}")
    albums = {a["title"]: a for a in data["albums"]}
    if albums and all(albums.get(k, {}).get("status") == v for k, v in expected.items()):
        break
    time.sleep(0.5)

print("== album outcomes")
for title, want in expected.items():
    a = albums.get(title, {})
    check(a.get("status") == want, f"{title}: {a.get('status')} (want {want}) — {a.get('status_detail')}")

print("== files on disk")
fl = os.path.join(music, "Test Band", "First Light (2001)")
check(sorted(os.listdir(fl)) == ["01 - Opening.flac", "02 - Daybreak.flac", "03 - Closing Time.flac", "cover.jpg"]
      if os.path.isdir(fl) else False, "First Light imported as FLAC with cover")
sw = os.path.join(music, "Test Band", "Second Wind (Deluxe Edition) (2005)")
check(os.path.isfile(os.path.join(sw, "CD2", "02 Calm.flac")), "multi-disc album keeps CD1/CD2 folders")
st_dir = os.path.join(music, "Test Band", "Small Things (2003)")
check(os.path.isfile(os.path.join(st_dir, "1 Tiny.mp3")), "EP imported (320 kbps source, 192 rejected)")
fc = os.path.join(music, "Test Band", "Flaky Connection (2014)")
check(os.path.isfile(os.path.join(fc, "01 Drop.mp3")), "flaky FLAC source abandoned, MP3 fallback imported")
left = [os.path.join(dp, f) for dp, _, fs in os.walk(downloads) for f in fs]
check(left == [], f"downloads folder cleaned up (left: {left})")

print("== slskd interaction")
users = [u for u, _ in state.enqueue_log]
check("carol" not in users and "dave" not in users, "incomplete / wrong-album folders never downloaded")
check("erin" not in users, "low-bitrate source never downloaded")
check(users.count("flaky") == 2, f"failed files retried once before moving on ({users.count('flaky')} enqueues)")
check("Rare Gem" in state.search_log and "Test Band Rare Gem" in state.search_log,
      "broader fallback search used for missing album")
check(not any("Not Out Yet" in q for q in state.search_log), "unreleased album not searched")
check(not any("Owned" in q for q in state.search_log), "album already in library not searched")
check(not state.searches, "searches cleaned up in slskd")

print("== actions")
rare = albums["Rare Gem"]
r = api("POST", f"/api/albums/{rare['id']}/skip")
check(r["status"] == "skipped", "skip album")
r = api("POST", f"/api/albums/{albums['Hit']['id']}/download")
check(r["status"] == "wanted", "un-skip a single -> wanted")
q = api("GET", "/api/queue")
check(isinstance(q["active"], list), "queue endpoint")
ev = api("GET", "/api/activity")
check(any("Added Test Band" in e["message"] for e in ev), "activity log populated")
arts = api("GET", "/api/artists")
check(arts[0]["image"] and "Brixton" in arts[0]["image"], "added artist keeps its picture")
check(arts[0]["have"] >= 5, f"artist counts ({arts[0]['have']} have)")
r = api("POST", "/api/library/rescan")
check(r["folders"] >= 5, f"library rescan finds folders ({r['folders']})")

print("== clear slskd searches")
for i in range(30):  # leftovers like the failed-search flood
    state.searches[f"old-{i}"] = {"id": f"old-{i}", "text": "ALISONAERO", "created": 0, "failed": True}
state.searches["manual-running"] = {"id": "manual-running", "text": "something", "created": time.time() + 60}
r = api("POST", "/api/slskd/clear-searches")
check(r["cleared"] == 30, f"cleared all 30 finished searches ({r})")
left = set(state.searches)
crate_active = {a["search_id"] for a in app.db.q("SELECT search_id FROM albums WHERE status = 'searching'")}
check("manual-running" in left and not any(k.startswith("old-") for k in left),
      "running search kept, finished ones gone")
check(left - {"manual-running"} <= crate_active, f"anything else kept is a search Crate is using ({len(left)} left)")

print("== remove artist")
api("DELETE", f"/api/artists/{aid}")
check(api("GET", "/api/artists") == [], "artist removed")
check(os.path.isdir(fl), "library files kept after removal")

print()
print("FAILED:" if fails else "ALL PASSED", len(fails) or "")
for f in fails:
    print("  -", f)
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fails else 0)
