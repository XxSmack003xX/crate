"""Duplicate scanner tests against a throwaway library folder.

Run:  python3 tests/test_dupes.py
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

tmp = tempfile.mkdtemp(prefix="crate-dupes-")
music = os.path.join(tmp, "music")
os.environ["CRATE_LIBRARY_DIR"] = music
os.environ["CRATE_TICK_SECONDS"] = "3600"


def mk(rel, size=1000):
    p = os.path.join(music, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(b"\0" * size)


tracks = ["Opening", "Daybreak", "Closing Time"]
# 1. Same album in two folders: FLAC copy and MP3 copy
for i, t in enumerate(tracks, 1):
    mk(f"Test Band/First Light (2001) [FLAC]/0{i} {t}.flac", 30000)
    mk(f"Test Band/First Light/0{i} {t}.mp3", 8000)
mk("Test Band/First Light/cover.jpg", 500)
# 2. One folder with FLAC and MP3 copies of every track
for i, t in enumerate(["Gust", "Breeze", "Gale", "Calm"], 1):
    mk(f"Test Band/Second Wind/0{i} {t}.flac", 20000)
    mk(f"Test Band/Second Wind/0{i} {t}.mp3", 5000)
# 3. Multi-disc FLAC vs single-folder MP3 (also "Artist - Album" naming)
for d in (1, 2):
    for i in (1, 2):
        mk(f"Test Band/Small Things (2003)/CD{d}/0{i} Track.flac", 10000)
for i in range(1, 5):
    mk(f"Test Band - Small Things/0{i} Track.mp3", 3000)
# 4. Not duplicates
for i in range(1, 4):
    mk(f"Test Band/Unique Album/0{i} x.flac", 1000)        # only copy
    mk(f"Other Artist/First Light/0{i} y.mp3", 1000)        # same title, different artist
mk("Test Band/Bonus Folder/01 a.flac"); mk("Test Band/Bonus Folder/02 b.flac")
mk("Test Band/Bonus Folder/03 c.flac"); mk("Test Band/Bonus Folder/bonus.mp3")  # 1 stray mp3 ≠ mixed
# 5. Three copies, one incomplete
for folder, n in (("Rare Gem", 3), ("Rare Gem (2012)", 3), ("Rare Gem [incomplete]", 1)):
    for i in range(1, n + 1):
        mk(f"Test Band/{folder}/0{i} s.mp3", 4000)

from http.server import ThreadingHTTPServer  # noqa: E402
from crate.core import App  # noqa: E402
from crate.server import make_handler  # noqa: E402

app = App(os.path.join(tmp, "crate.db"))
# A Crate-managed album pointing at the MP3 copy of First Light
aid = app.db.x("INSERT INTO artists (mbid, name, added_at) VALUES ('a-1','Test Band','x')")
mp3_copy = os.path.join(os.path.realpath(music), "Test Band", "First Light")
alb = app.db.x("INSERT INTO albums (mbid, artist_id, title, status, path) VALUES ('rg-1', ?, 'First Light', 'have', ?)",
               (aid, mp3_copy))
httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"

sys.path.insert(0, os.path.join(ROOT, "tests"))
from client import login_opener  # noqa: E402
OPENER = login_opener(base)


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def scan():
    api("POST", "/api/dupes/scan")
    for _ in range(100):
        _, d = api("GET", "/api/dupes")
        if d["state"] != "scanning":
            return d
        time.sleep(0.05)
    return d


def find(d, album, typ):
    return next((g for g in d["groups"] if g["album"].startswith(album) and g["type"] == typ), None)


print("== scan")
d = scan()
check(d["state"] == "done", "scan finishes")
summary = sorted((g["album"], g["type"], len(g["copies"])) for g in d["groups"])
print("    groups:", summary)
fl = find(d, "First Light", "copies")
check(fl and len(fl["copies"]) == 2, "First Light: FLAC folder + MP3 folder grouped")
check(fl and {c["main"] for c in fl["copies"]} == {"flac", "mp3"}, "formats detected per copy")
sw = find(d, "Second Wind", "mixed")
check(sw and sw["copies"][0]["mixed_formats"] == ["flac", "mp3"], "Second Wind: mixed FLAC+MP3 folder")
check(sw and sw["suggest_remove_formats"] == ["mp3"], "mixed folder suggests removing MP3 (FLAC preferred)")
st = find(d, "Small Things", "copies")
check(st and any(c["discs"] == 2 for c in st["copies"]), "multi-disc folder counts as one copy")
rg = find(d, "Rare Gem", "copies")
check(rg and len(rg["copies"]) == 3, "three copies grouped")
keep = rg and next(c for c in rg["copies"] if c["id"] == rg["suggest_keep"])
check(keep and keep["tracks"] == 3, "suggestion never keeps the incomplete copy")
check(not any(g["album"].startswith("Unique") for g in d["groups"]), "single copy not flagged")
check(not any(g["album"] == "Bonus Folder" for g in d["groups"]), "one stray MP3 doesn't make a folder 'mixed'")
check(sum(1 for g in d["groups"] if g["album"] == "First Light") == 1, "other artist's 'First Light' not grouped")
crate_copy = next(c for c in fl["copies"] if c["main"] == "mp3")
check(crate_copy["crate_album"] == alb, "copy downloaded by Crate is marked")

print("== guard rails")
code, r = api("POST", "/api/dupes/resolve", {"group": sw["id"], "keep": "c999"})
check(code == 400, "keep must be one of the group's copies")
code, r = api("POST", "/api/dupes/resolve", {"group": "nope", "keep": "c1"})
check(code == 400, "unknown group rejected")

print("== keep FLAC copy of First Light")
flac_copy = next(c for c in fl["copies"] if c["main"] == "flac")
code, r = api("POST", "/api/dupes/resolve", {"group": fl["id"], "keep": flac_copy["id"]})
check(code == 200 and r["result"]["folders"] == 1, f"MP3 folder removed ({r.get('result')})")
check(not os.path.exists(os.path.join(music, "Test Band", "First Light")), "MP3 folder gone from library")
check(os.path.isfile(os.path.join(music, "Test Band", "First Light (2001) [FLAC]", "01 Opening.flac")), "FLAC kept")
trashed = [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(music, ".crate-trash")) for f in fs]
check(any(t.endswith("First Light/cover.jpg") for t in trashed) and len(trashed) == 4,
      "removed folder (incl. cover) is in .crate-trash, not deleted")
row = app.db.one("SELECT path FROM albums WHERE id = ?", (alb,))
check(row["path"].endswith("First Light (2001) [FLAC]"), "Crate's album record now points at the kept copy")
check(not find(r, "First Light", "copies"), "resolved group disappears")

print("== remove MP3 files from the mixed folder")
code, r = api("POST", "/api/dupes/resolve", {"group": sw["id"], "remove_format": "mp3"})
left = sorted(os.listdir(os.path.join(music, "Test Band", "Second Wind")))
check(code == 200 and all(f.endswith(".flac") for f in left) and len(left) == 4, f"only FLAC left ({left})")

print("== bulk: keep MP3 everywhere")
code, r = api("POST", "/api/dupes/resolve", {"all": "keep_format", "format": "mp3"})
check(code == 200, f"bulk keep MP3 ({r.get('result')})")
check(not os.path.exists(os.path.join(music, "Test Band", "Small Things (2003)")) and
      os.path.isdir(os.path.join(music, "Test Band - Small Things")), "Small Things: MP3 copy kept, FLAC discs removed")
rg_left = [f for f in os.listdir(os.path.join(music, "Test Band")) if f.startswith("Rare Gem")]
check(len(rg_left) == 1, f"Rare Gem: one copy left ({rg_left})")

print("== rescan is clean, library scan ignores trash")
d = scan()
check(d["groups"] == [], f"no duplicates left ({[g['album'] for g in d['groups']]})")
app.library.scan()
check(not any(".crate-trash" in e[2] for e in app.library.entries), "trash folder not treated as library")

print("== empty trash")
check(d["trash"]["size"] > 0, f"trash size reported ({d['trash']['size']} bytes)")
code, r = api("POST", "/api/dupes/trash/empty")
check(code == 200 and not os.path.exists(os.path.join(music, ".crate-trash")), "trash emptied")

print("== permanent delete mode")
for i in (1, 2):
    mk(f"Test Band/Dup Again/0{i} t.flac"); mk(f"Test Band/Dup Again (copy)/0{i} t.flac")
app.db.save_settings({"dupes_action": "delete"})
d = scan()
g = find(d, "Dup Again", "copies")
api("POST", "/api/dupes/resolve", {"group": g["id"], "keep": g["suggest_keep"]})
check(not os.path.exists(os.path.join(music, ".crate-trash")), "delete mode doesn't use trash")
check(sum(1 for f in os.listdir(os.path.join(music, "Test Band")) if f.startswith("Dup Again")) == 1, "one copy left")

print()
print("FAILED:" if fails else "ALL PASSED", len(fails) or "")
for f in fails:
    print("  -", f)
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fails else 0)
