"""Library import tests: scan a library folder, match artists on (fake) MusicBrainz,
add them, and check owned vs missing albums.

Run:  python3 tests/test_import.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ["CRATE_TICK_SECONDS"] = "0.3"
os.environ["CRATE_MB_INTERVAL"] = "0"

import mock_services  # noqa: E402

tmp = tempfile.mkdtemp(prefix="crate-import-")
music = os.path.join(tmp, "music")


def mk(rel, size=1000):
    p = os.path.join(music, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(b"\0" * size)


# Two MusicBrainz artists are called "Test Band"; your albums say which one.
for i in range(1, 4):
    mk(f"Test Band/First Light (2001)/0{i} t.flac")
    mk(f"Test Band/Owned Already (2010)/0{i} t.flac")
# Folder name differs from MusicBrainz ("NightOwls" vs "Night Owls"), multi-disc album
for d in (1, 2):
    mk(f"NightOwls/Midnight Hours/CD{d}/01 t.mp3")
# Not artists / no match
mk("Various Artists/Summer Hits 2001/01 a.mp3")
mk("Nobody Here/Some Album/01 a.mp3")
mk("loose track.mp3")

srv, state = mock_services.start(os.path.join(tmp, "dl"))
mock_url = f"http://127.0.0.1:{srv.server_address[1]}"
os.environ.update({"CRATE_LIBRARY_DIR": music, "CRATE_MB_URL": mock_url + "/ws/2",
                   "CRATE_WIKIDATA_URL": mock_url + "/w/api.php"})
# slskd deliberately NOT configured: nothing may be searched or downloaded.

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
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read())}


fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def scan():
    api("POST", "/api/import/scan")
    for _ in range(200):
        d = api("GET", "/api/import")
        if d["state"] in ("done", "error"):
            return d
        time.sleep(0.05)
    return d


def wait(pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


print("== scan library")
d = scan()
check(d["state"] == "done", f"scan finishes ({d.get('error')})")
by = {r["local_name"]: r for r in d["results"]}
print("    found:", {k: (v["status"], v.get("confidence")) for k, v in by.items()})
check(set(by) == {"Test Band", "NightOwls", "Nobody Here"}, "artists found; 'Various Artists' and loose files ignored")
tb = by["Test Band"]
check(tb["match"]["mbid"] == "a-1" and tb["confidence"] == "albums",
      "same-name artists: picked the one whose releases match your albums")
check(any(a["mbid"] == "a-2" for a in tb["alternatives"]), "other 'Test Band' offered as an alternative")
check(tb["match"]["image"] and tb["match"]["overlap"] == 2, "match carries picture and album overlap")
no = by["NightOwls"]
check(no["match"]["mbid"] == "a-3" and no["selected"], "folder name 'NightOwls' matched to 'Night Owls' by its albums")
check(by["Nobody Here"]["status"] == "none" and not by["Nobody Here"]["selected"], "no match → not selected")
check(by["NightOwls"]["album_count"] == 1, "multi-disc album counted once")

print("== add (auto-download off)")
items = [{"id": r["id"], "mbid": r["match"]["mbid"]} for r in d["results"] if r["selected"]]
r = api("POST", "/api/import/add", {"items": items, "auto_download": False})
check(r["result"]["added"] == 2, f"two artists added ({r['result']})")
check(wait(lambda: not api("GET", "/api/import")["adding"] and all(
    a["sync_status"] == "ok" for a in api("GET", "/api/artists"))), "discographies fetched")
arts = {a["name"]: a for a in api("GET", "/api/artists")}
check(arts.get("Night Owls", {}).get("local_name") == "NightOwls", "folder name remembered for the artist")


def albums(name):
    return {a["title"]: a for a in api("GET", f"/api/artists/{arts[name]['id']}")["albums"]}


tba = albums("Test Band")
check(tba["First Light"]["status"] == "have" and tba["Owned Already"]["status"] == "have", "owned albums: In library")
check(tba["Rare Gem"]["status"] == "missing", "albums you don't have: missing (not auto-downloading)")
check(tba["Live at Home"]["status"] == "skipped", "release types you don't follow still skipped")
owl = albums("Night Owls")
check(owl["Midnight Hours"]["status"] == "have", "owned album found via the folder name 'NightOwls'")
check(owl["Dawn Chorus"]["status"] == "missing", "Night Owls missing album listed")
check(not any(a["status"] in ("wanted", "searching") for a in list(tba.values()) + list(owl.values())),
      "nothing queued without asking")

print("== missing albums")
m = api("GET", "/api/missing")
names = {g["artist_name"]: [a["title"] for a in g["albums"]] for g in m["artists"]}
print("    missing:", names)
check("Not Out Yet" not in names.get("Test Band", []), "unreleased albums not offered for download")
check(set(names.get("Night Owls", [])) == {"Dawn Chorus", "Sunrise"}, "missing grouped by artist")
one = owl["Dawn Chorus"]["id"]
r = api("POST", "/api/missing/download", {"album_ids": [one]})
check(r["queued"] == 1 and albums("Night Owls")["Dawn Chorus"]["status"] == "wanted", "download one album")
r = api("POST", "/api/missing/download", {"artist_id": arts["Night Owls"]["id"]})
check(r["queued"] == 1 and albums("Night Owls")["Sunrise"]["status"] == "wanted", "download all for one artist")
before = api("GET", "/api/missing")["total"]
r = api("POST", "/api/missing/download", {})
check(r["queued"] == before and api("GET", "/api/missing")["total"] == 0, f"download all missing ({before})")

print("== rescan")
d = scan()
by = {r["local_name"]: r for r in d["results"]}
check(by["Test Band"]["status"] == "following" and by["NightOwls"]["status"] == "following",
      "artists you follow show as 'following' (incl. by folder name)")
check(api("POST", "/api/import/add", {"items": [{"id": by["Test Band"]["id"], "mbid": "a-1"}]})["result"]["added"] == 0,
      "can't add an artist twice")

print("== auto-download on")
mk("Fresh Owls/Dawn Chorus/01 x.mp3")  # no MB match for this name in the mock, just checks the flag path
app.db.x("DELETE FROM albums WHERE artist_id = ?", (arts["Night Owls"]["id"],))
app.db.x("DELETE FROM artists WHERE id = ?", (arts["Night Owls"]["id"],))
d = scan()
no = next(r for r in d["results"] if r["local_name"] == "NightOwls")
api("POST", "/api/import/add", {"items": [{"id": no["id"], "mbid": "a-3"}], "auto_download": True})
wait(lambda: not api("GET", "/api/import")["adding"])
owl_id = next(a["id"] for a in api("GET", "/api/artists") if a["name"] == "Night Owls")
st = {a["title"]: a["status"] for a in api("GET", f"/api/artists/{owl_id}")["albums"]}
check(st.get("Dawn Chorus") == "wanted" and st.get("Midnight Hours") == "have", f"auto-download: missing → wanted ({st})")

print()
print("FAILED:" if fails else "ALL PASSED", len(fails) or "")
for f in fails:
    print("  -", f)
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fails else 0)
