"""Search fallbacks: "Artist Album" → "Album" → "*rtist Album", matching folders that
don't name the artist, and explaining why nothing was found.

Run:  python3 tests/test_search.py
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.update({"CRATE_MB_INTERVAL": "0", "CRATE_UPDATE_CHECKS": "false", "CRATE_SEARCH_SPACING": "0"})
tmp = tempfile.mkdtemp(prefix="crate-search-")
os.makedirs(os.path.join(tmp, "music"))
os.environ.update({"CRATE_LIBRARY_DIR": os.path.join(tmp, "music"), "CRATE_SLSKD_DOWNLOADS_DIR": tmp})

from crate.matcher import explain, rank_candidates, search_queries  # noqa: E402
from crate.core import App  # noqa: E402
from crate.db import now_iso  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


TRACKS = ["Wanna Be Startin' Somethin'", "Baby Be Mine", "The Girl Is Mine", "Thriller", "Beat It",
          "Billie Jean", "Human Nature", "P.Y.T. (Pretty Young Thing)", "The Lady in My Life"]


def folder(user, directory, names, ext="flac", size=30_000_000, free=True):
    return {"username": user, "hasFreeUploadSlot": free, "uploadSpeed": 3_000_000, "queueLength": 0,
            "files": [{"filename": f"{directory}\\{i:02} - {n}.{ext}", "size": size, "extension": ext,
                       "bitRate": None if ext == "flac" else 320} for i, n in enumerate(names, 1)]}


print("== queries")
q = search_queries("Michael Jackson", "Thriller")
check(q == ["Michael Jackson Thriller", "Thriller", "*ichael *ackson Thriller"], f"Michael Jackson – Thriller → {q}")
q = search_queries("The Beatles", "Abbey Road (Remastered)")
check(q == ["The Beatles Abbey Road", "Abbey Road", "The *eatles Abbey Road"], f"articles keep their first letter → {q}")
q = search_queries("Adele", "21")
check(q == ["Adele 21", "*dele 21"], f"very short titles aren't searched alone → {q}")
q = search_queries("U2", "War")
check(q == ["U2 War", "War"], f"nothing to wildcard in short names → {q}")

print("== matching folders that don't name the artist")
plain = folder("anna", "Music\\Thriller (1982) [FLAC]", TRACKS)
named = folder("ben", "Music\\Michael Jackson\\Thriller", TRACKS, ext="mp3", size=9_000_000)
other = folder("cara", "Rips\\Thriller", ["Intro", "Night Drive", "Neon", "Outro", "Static", "Run", "Fade",
                                          "Echo", "End"])
stats = {}
c = rank_candidates([plain, named, other], "Michael Jackson", "Thriller", track_count=9, track_titles=TRACKS,
                    stats=stats)
users = [x["username"] for x in c]
check("anna" in users, "folder without the artist accepted when the tracklist matches")
check("cara" not in users, "another 'Thriller' with different tracks rejected")
check(stats.get("tracks") == 1 and stats.get("folders") == 3, f"rejections counted ({stats})")
a = next(x for x in c if x["username"] == "anna")
check(a["artist_in_path"] is False, "marked as artist not in path")
c2 = rank_candidates([folder("anna", "Music\\Thriller", TRACKS), folder("dan", "Music\\Michael Jackson - Thriller", TRACKS)],
                     "Michael Jackson", "Thriller", track_count=9, track_titles=TRACKS)
check(c2[0]["username"] == "dan", "with the same format, a folder naming the artist ranks first")
few = folder("eve", "x\\Thriller", ["Thriller", "Beat It"])
s2 = {}
check(not rank_candidates([few], "Michael Jackson", "Thriller", track_count=None, track_titles=["Thriller", "Beat It"], stats=s2),
      "short tracklists still need the artist in the path")
check(explain({"folders": 9, "tracks": 2, "too_few": 5, "artist": 1}) ==
      "5 too few tracks, 2 tracklist didn't match, 1 artist not in the path", "explain()")

print("== the whole search: artist name gets no results, title alone does")


class FakeSlskd:
    def __init__(self, results):
        self.results, self.searches, self.log = results, {}, []

    def start_search(self, text, timeout_s=45):
        sid = f"s{len(self.log)}"
        self.log.append(text)
        self.searches[sid] = text
        return sid

    def search_state(self, sid):
        return {"id": sid, "isComplete": True, "state": "Completed, Succeeded",
                "responseCount": len(self.results.get(self.searches[sid], []))}

    def search_responses(self, sid):
        return self.results.get(self.searches[sid], [])

    def delete_search(self, sid):
        pass


app = App(os.path.join(tmp, "crate.db"))
aid = app.db.x("INSERT INTO artists (mbid, name, sort_name, added_at, sync_status) VALUES ('mj','Michael Jackson','Jackson, Michael',?,'ok')",
               (now_iso(),))


def album(title, tracks):
    return app.db.x("INSERT INTO albums (mbid, artist_id, title, primary_type, year, release_date, status, track_count, tracks) "
                    "VALUES (?,?,?, 'Album', '1982', '1982-11-30', 'wanted', ?, ?)",
                    (f"rg-{title}", aid, title, len(tracks), json.dumps(tracks)))


def drive(sl, album_id, rounds=6):
    for _ in range(rounds):
        app.last_search_start = 0
        app._start_searches(sl)
        app._poll_searches(sl)
        a = app.album(album_id)
        if a["status"] in ("queued", "not_found"):
            return a
    return app.album(album_id)


thr = album("Thriller", TRACKS)
sl = FakeSlskd({"Michael Jackson Thriller": [], "Thriller": [plain, other]})
a = drive(sl, thr)
check(sl.log[:2] == ["Michael Jackson Thriller", "Thriller"], f"searched with the artist, then without ({sl.log})")
check(a["status"] == "queued" and json.loads(a["candidates"])[0]["username"] == "anna", f"found a source ({a['status_detail']})")

bad = album("Bad", ["Bad", "The Way You Make Me Feel", "Speed Demon", "Liberian Girl"])
sl = FakeSlskd({"*ichael *ackson Bad": [folder("fay", "z\\Bad Company - Bad", ["Can't Get Enough", "Rock Steady", "Ready for Love", "Movin' On"])]})
a = drive(sl, bad)
check(sl.log == ["Michael Jackson Bad", "Bad", "*ichael *ackson Bad"], f"tried every query ({sl.log})")
check(a["status"] == "not_found", "not found")
d = a["status_detail"]
check('"Michael Jackson Bad": no results' in d and '"Bad": no results' in d and "tracklist didn't match" in d,
      f"explains what each search found ({d})")
ev = app.db.one("SELECT message FROM events WHERE album_id = ? ORDER BY id DESC", (bad,))
check(ev and "no suitable source" in ev["message"] and "retrying in" in ev["message"], "logged in Activity")

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f_ in failures:
        print("  - " + f_)
    sys.exit(1)
print("ALL PASSED")
