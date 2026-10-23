"""Duplicate-song tests: the same track in several places, copy-marked files,
compilations, songs already covered by album duplicates, and removal actions.

Run:  python3 tests/test_song_dupes.py
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

tmp = tempfile.mkdtemp(prefix="crate-songs-")
music = os.path.join(tmp, "music")
os.environ["CRATE_LIBRARY_DIR"] = music
os.environ["CRATE_TICK_SECONDS"] = "3600"


def mk(rel, size=1000):
    p = os.path.join(music, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(b"\0" * size)


A = "Song Artist"
# "Hit Song" on an album, as a loose single (FLAC), and on a Various Artists compilation
for i, t in enumerate(["Opener", "Hit Song", "Closer"], 1):
    mk(f"{A}/Greatest Hits (2010)/0{i} {t}.mp3", 8000)
mk(f"{A}/Singles/Hit Song.flac", 30000)
mk("Various Artists/Summer Hits/05 Song Artist - Hit Song.mp3", 7000)
# A copy-marked duplicate in the same folder
mk(f"{A}/Greatest Hits (2010)/03 Closer (1).mp3", 8000)
# Not duplicates
mk(f"{A}/Live/02 Hit Song (Live).mp3", 9000)              # different version
mk(f"{A}/Album A/01 Intro.mp3"); mk(f"{A}/Album B/01 Intro.mp3")   # generic title
mk("Other Artist/Stuff/01 Hit Song.mp3")                   # different artist
# Covered by album-level duplicates (should NOT be listed as songs)
for i, t in enumerate(["Alpha", "Beta"], 1):
    mk(f"{A}/Twin (2001) [FLAC]/0{i} {t}.flac", 20000)
    mk(f"{A}/Twin/0{i} {t}.mp3", 6000)
for i, t in enumerate(["Gamma", "Delta"], 1):
    mk(f"{A}/Mixed/0{i} {t}.flac", 20000)
    mk(f"{A}/Mixed/0{i} {t}.mp3", 6000)
# Remastered = same recording
mk("Oldies Band/Original (1969)/07 Big Tune.mp3", 5000)
mk("Oldies Band/Anniversary Edition/07 Big Tune (Remastered 2019).flac", 25000)

from http.server import ThreadingHTTPServer  # noqa: E402
from crate import dupes as dupes_mod  # noqa: E402
from crate.core import App  # noqa: E402
from crate.server import make_handler  # noqa: E402

app = App(os.path.join(tmp, "crate.db"))
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
    api("POST", "/api/dupes/scan")
    for _ in range(200):
        d = api("GET", "/api/dupes")
        if d["state"] != "scanning":
            return d
        time.sleep(0.05)
    return d


def song(d, title):
    return next((g for g in d["song_groups"] if g["title"].startswith(title)), None)


def exists(rel):
    return os.path.exists(os.path.join(music, rel))


print("== scan")
d = scan()
print("    song groups:", [(g["artist"], g["title"], len(g["songs"])) for g in d["song_groups"]])
hit = song(d, "Hit Song")
check(hit and len(hit["songs"]) == 3, "Hit Song: album track + loose single + compilation track grouped")
check(hit and not any("Live" in s["rel"] or "Other Artist" in s["rel"] for s in hit["songs"]),
      "live version and other artist's song kept apart")
keep = hit and next(s for s in hit["songs"] if s["id"] == hit["suggest_keep"])
check(keep and keep["ext"] == "flac", "suggestion keeps the FLAC copy (preferred format)")
closer = song(d, "Closer")
check(closer and len(closer["songs"]) == 2, "'Closer' and 'Closer (1)' in the same folder grouped")
ck = closer and next(s for s in closer["songs"] if s["id"] == closer["suggest_keep"])
check(ck and not ck["copy_marked"], "suggestion keeps the original, not the '(1)' copy")
check(not song(d, "Intro"), "generic titles like 'Intro' on different albums aren't duplicates")
check(not song(d, "Alpha") and not song(d, "Gamma"), "songs covered by album duplicates aren't listed twice")
check(song(d, "Big Tune") is not None, "remastered copy counts as the same song")
check(d["song_savings"] > 0 and d["files_scanned"] >= 20, f"savings and file count ({d['files_scanned']} files)")

print("== remove one copy")
comp = next(s for s in hit["songs"] if "Various" in s["rel"])
r = api("POST", "/api/dupes/resolve", {"song_group": hit["id"], "remove": comp["id"]})
check(r["result"]["files"] == 1 and not exists("Various Artists/Summer Hits/05 Song Artist - Hit Song.mp3"),
      "removed the compilation copy")
check(not exists("Various Artists"), "emptied folders cleaned up")
check(any("Various Artists" in t for dp, _, fs in os.walk(os.path.join(music, ".crate-trash")) for t in [dp]),
      "removed copy is in the trash")
hit2 = song(r, "Hit Song")
check(hit2 and len(hit2["songs"]) == 2, "group stays while 2 copies remain")

print("== keep only this")
single = next(s for s in hit2["songs"] if s["ext"] == "flac")
r = api("POST", "/api/dupes/resolve", {"song_group": hit2["id"], "keep": single["id"]})
check(exists(f"{A}/Singles/Hit Song.flac") and not exists(f"{A}/Greatest Hits (2010)/02 Hit Song.mp3"),
      "kept the FLAC single, removed the album's MP3 copy")
check(not song(r, "Hit Song"), "resolved group disappears")

print("== guard rails")
cl = song(r, "Closer")
r2 = api("POST", "/api/dupes/resolve", {"song_group": cl["id"], "remove": "s9999"})
check(r2.get("_status") == 400, "can't remove a file that isn't in the group")
r2 = api("POST", "/api/dupes/resolve", {"song_group": "nope", "remove": cl["songs"][0]["id"]})
check(r2.get("_status") == 400, "unknown group rejected")

print("== remove all duplicate songs")
r = api("POST", "/api/dupes/resolve", {"all": "songs_suggested"})
check(r["result"]["groups"] >= 2 and r["song_groups"] == [], f"all song duplicates resolved ({r['result']})")
check(exists(f"{A}/Greatest Hits (2010)/03 Closer.mp3") and not exists(f"{A}/Greatest Hits (2010)/03 Closer (1).mp3"),
      "kept 'Closer', removed 'Closer (1)'")
check(exists("Oldies Band/Anniversary Edition/07 Big Tune (Remastered 2019).flac") and
      not exists("Oldies Band/Original (1969)/07 Big Tune.mp3"), "kept the FLAC remaster")
check(exists(f"{A}/Live/02 Hit Song (Live).mp3") and exists(f"{A}/Album A/01 Intro.mp3"), "non-duplicates untouched")
d = scan()
check(d["song_groups"] == [], "rescan finds no duplicate songs")
check(len(d["groups"]) == 2, "album-level duplicates still listed on the Albums tab")

print("== keep MP3 everywhere (songs)")
mk("Fresh Artist/One/01 Tune.flac", 20000); mk("Fresh Artist/Two/03 Tune.mp3", 6000)
mk("Fresh Artist/Three/02 Other.flac", 20000); mk("Fresh Artist/Four/02 Other.flac", 21000)
d = scan()
r = api("POST", "/api/dupes/resolve", {"all": "songs_keep_format", "format": "mp3"})
check(exists("Fresh Artist/Two/03 Tune.mp3") and not exists("Fresh Artist/One/01 Tune.flac"), "kept the MP3 copy")
check(exists("Fresh Artist/Three/02 Other.flac") and exists("Fresh Artist/Four/02 Other.flac"),
      "songs without an MP3 copy left alone")

print("== length check (with tags)")
orig = dupes_mod._song_info
lengths = {"Long Song.mp3": 190, "Long Song.flac": 192, "Long Song (Extended).mp3": 410}


def fake_info(path):
    name = os.path.basename(path)
    if name.startswith("0") and "Long Song" in name:
        return "Tag Artist", "Long Song", lengths["Long Song (Extended).mp3"] if "x" in name else 190, None
    return orig(path)


mk("Tag Artist/A/01 Long Song.mp3"); mk("Tag Artist/B/01 Long Song.flac"); mk("Tag Artist/C/0x Long Song.mp3")
dupes_mod._song_info = fake_info
d = scan()
dupes_mod._song_info = orig
ls = song(d, "Long Song")
check(ls and len(ls["songs"]) == 2 and not any("C/" in s["rel"] for s in ls["songs"]),
      "same title but much longer (different version) kept separate")

print()
print("FAILED:" if fails else "ALL PASSED", len(fails) or "")
for f in fails:
    print("  -", f)
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fails else 0)
