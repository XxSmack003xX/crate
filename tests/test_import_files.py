"""Finding and importing what slskd downloaded, in all the ways slskd can save it.

Run:  python3 tests/test_import_files.py
(The permission checks only run as a non-root user; root can read everything.)
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.update({"CRATE_MB_INTERVAL": "0", "CRATE_UPDATE_CHECKS": "false"})

import mock_services  # noqa: E402

tmp = tempfile.mkdtemp(prefix="crate-files-")
DL, LIB = os.path.join(tmp, "downloads"), os.path.join(tmp, "music")
os.makedirs(DL)
os.makedirs(LIB)
os.environ.update({"CRATE_SLSKD_DOWNLOADS_DIR": DL, "CRATE_LIBRARY_DIR": LIB, "CRATE_IMPORT_MODE": "move"})

from crate.core import App  # noqa: E402
from crate.db import now_iso  # noqa: E402
from crate.slskd import Slskd  # noqa: E402

app = App(os.path.join(tmp, "crate.db"))
AID = app.db.x("INSERT INTO artists (mbid, name, sort_name, added_at, sync_status) VALUES ('m','Dire Straits','Dire Straits',?, 'ok')",
               (now_iso(),))
failures = []
ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


class FakeSlskd:
    def __init__(self, saved=None):
        self.saved = saved
        self.calls = 0

    def completed_files(self):
        self.calls += 1
        return self.saved

    def user_downloads(self, user):
        return []

    def cancel_download(self, *a, **k):
        pass


def write(rel, size):
    p = os.path.join(DL, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(b"\1" * size)
    return p


n = [0]


def album(title, remote, user="alice", waited=0):
    """An album slskd has finished downloading. remote: [(remote path, size)]"""
    n[0] += 1
    files = [{"filename": r, "size": s} for r, s in remote]
    last = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - waited)) + "+00:00"
    return app.db.x(
        "INSERT INTO albums (mbid, artist_id, title, primary_type, year, status, dl_username, dl_files, dl_last_change) "
        "VALUES (?,?,?, 'Album', '1978', 'importing', ?, ?, ?)",
        (f"rg-{n[0]}", AID, title, user, json.dumps(files), last))


def run(album_id, sl=None):
    app._import(sl or FakeSlskd(), album_id)
    return app.album(album_id)


def in_lib(title, *names):
    d = os.path.join(LIB, "Dire Straits", f"{title} (1978)")
    return all(os.path.isfile(os.path.join(d, x)) for x in names)


print("== default layout: <downloads>/<remote folder>/<file>")
write("Dire Straits [FLAC]/01 Down to the Waterline.flac", 300)
write("Dire Straits [FLAC]/02 Water of Love.flac", 301)
a = run(album("Default", [("@@abcde\\Music\\Dire Straits [FLAC]\\01 Down to the Waterline.flac", 300),
                          ("@@abcde\\Music\\Dire Straits [FLAC]\\02 Water of Love.flac", 301)]))
check(a["status"] == "have" and in_lib("Default", "01 Down to the Waterline.flac", "02 Water of Love.flac"), "imported")
check(not os.path.exists(os.path.join(DL, "Dire Straits [FLAC]")), "empty download folder tidied up")

print("== slskd's subdirectory setting adds the username")
write("bob/Communique/01 Once Upon a Time in the West.flac", 400)
a = run(album("Username", [("share\\Communique\\01 Once Upon a Time in the West.flac", 400)], user="bob"))
check(a["status"] == "have" and in_lib("Username", "01 Once Upon a Time in the West.flac"), "found under <user>/")

print("== name already taken: slskd adds _<ticks>")
write("Making Movies/01 Tunnel of Love.flac", 999)               # an earlier source's copy
write("Making Movies/01 Tunnel of Love_638650123456789012.flac", 500)  # the one we just downloaded
a = run(album("Ticks", [("x\\Making Movies\\01 Tunnel of Love.flac", 500)]))
moved = os.path.join(LIB, "Dire Straits", "Ticks (1978)", "01 Tunnel of Love_638650123456789012.flac")
check(a["status"] == "have" and os.path.getsize(moved) == 500, "took the renamed file with the right size")
check(os.path.isfile(os.path.join(DL, "Making Movies", "01 Tunnel of Love.flac")), "left the other source's file alone")

print("== characters slskd replaced, and Unicode forms")
write("Love Over Gold/01 Telegraph Road_ (Live_ Part 1).flac", 210)
nfd = unicodedata.normalize("NFD", "02 Café Industrial.flac")
write("Love Over Gold/" + nfd, 211)
a = run(album("Sanitised", [("y\\Love Over Gold\\01 Telegraph Road? (Live: Part 1).flac", 210),
                            ("y\\Love Over Gold\\" + unicodedata.normalize("NFC", "02 Café Industrial.flac"), 211)]))
check(a["status"] == "have", f"matched '?'→'_', ':'→'_' and NFC/NFD names ({a['status_detail']})")

print("== slskd's event log says where it saved the file")
write("odd place/xyz/track one.flac", 77)
saved = {("carol", "z\\Alchemy\\01 Once Upon.flac"): "/app/downloads/odd place/xyz/track one.flac"}
sl = FakeSlskd(saved)
a = run(album("Events", [("z\\Alchemy\\01 Once Upon.flac", 77)], user="carol"), sl)
check(a["status"] == "have" and in_lib("Events", "track one.flac"), "slskd's path mapped onto Crate's downloads folder")
check(sl.calls == 1, "event log only asked when the usual places miss")

print("== sizes reported loosely")
write("On Every Street/01 Calling Elvis.mp3", 10_050)
a = run(album("Loose", [("w\\On Every Street\\01 Calling Elvis.mp3", 10_000)]))
check(a["status"] == "have", "within 2% in the album's folder → accepted")
write("Brothers/01 So Far Away.mp3", 5_000)
a = run(album("Wrong size", [("w\\Brothers in Arms\\01 So Far Away.mp3", 9_000)]))
check(a["status"] == "importing" and "size doesn't match" in a["status_detail"], f"clearly different size → explained ({a['status_detail']})")

print("== files that aren't there (yet)")
write("Golden Heart/01 Darling Pretty.flac", 50)
aid = album("Partial", [("g\\Golden Heart\\01 Darling Pretty.flac", 50), ("g\\Golden Heart\\02 Imelda.flac", 60)])
a = run(aid)
check(a["status"] == "importing" and "Found 1 of 2 files" in a["status_detail"] and "02 Imelda.flac" in a["status_detail"],
      f"says what it found and what it's looking for ({a['status_detail']})")
app.db.update_album(aid, dl_last_change="2020-01-01T00:00:00+00:00")
a = run(aid)
check(a["status"] == "failed" and a["status_detail"].startswith("Couldn't import") and "Import again" in a["status_detail"],
      "gives up after 10 minutes with the reason")
ev = app.db.one("SELECT message FROM events WHERE album_id = ? ORDER BY id DESC", (aid,))
check(ev and "Missing: 02 Imelda.flac" in ev["message"], "activity log lists the missing file")
write("Golden Heart/02 Imelda.flac", 60)
a = app.album_action(aid, "reimport")
check(a["status"] == "have" and in_lib("Partial", "01 Darling Pretty.flac", "02 Imelda.flac"), "Import again picks it up")

print("== the downloads folder isn't there")
app.db.save_settings({"slskd_downloads_dir": os.path.join(tmp, "nope")})
a = run(album("No folder", [("q\\X\\01 a.flac", 1)]))
check(a["status"] == "importing" and "can't see the downloads folder" in a["status_detail"], "explains the missing folder")
app.db.save_settings({"slskd_downloads_dir": DL})

print("== slskd client reads the event log")
srv, state = mock_services.start(DL)
mu = f"http://127.0.0.1:{srv.server_address[1]}"
state.events.append({"id": "e1", "type": "DownloadFileComplete", "data": json.dumps({
    "localFilename": "/app/downloads/A/01.flac", "remoteFilename": "r\\A\\01.flac",
    "transfer": {"username": "dan", "filename": "r\\A\\01.flac"}})})
state.events.append({"id": "e2", "type": "PrivateMessageReceived", "data": "{}"})
got = Slskd(mu, mock_services.API_KEY).completed_files()
check(got == {("dan", "r\\A\\01.flac"): "/app/downloads/A/01.flac"}, f"parsed DownloadFileComplete events ({got})")
state.events_enabled = False
check(Slskd(mu + "/", mock_services.API_KEY).completed_files() is None, "older slskd without /events → None")

if not ROOT_USER:
    print("== permissions")
    write("Locked/01 Private.flac", 10)
    os.chmod(os.path.join(DL, "Locked"), 0o000)
    a = run(album("Locked", [("l\\Locked\\01 Private.flac", 10)]))
    check("permission denied" in a["status_detail"] and "PUID/PGID" in a["status_detail"],
          f"unreadable folder explained ({a['status_detail']})")
    os.chmod(os.path.join(DL, "Locked"), 0o755)
    p = write("Stuck/01 Keep.flac", 12)
    os.chmod(os.path.dirname(p), 0o555)
    a = run(album("Readonly", [("l\\Stuck\\01 Keep.flac", 12)]))
    check(a["status"] == "have" and in_lib("Readonly", "01 Keep.flac") and os.path.isfile(p),
          "can't delete from downloads → copied, original left")
    os.chmod(os.path.dirname(p), 0o755)
else:
    print("== permissions: skipped (running as root)")

shutil.rmtree(tmp, ignore_errors=True)
print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f_ in failures:
        print("  - " + f_)
    sys.exit(1)
print("ALL PASSED")
