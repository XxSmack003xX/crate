"""Crate's engine: artist sync, library checks and the slskd download pipeline.

Album lifecycle (albums.status):

    wanted ──search──▶ searching ──results──▶ queued ──enqueue──▶ downloading
       ▲                   │                    ▲                     │
       │                   ▼                    └──── next source ◀───┤ (failed / stalled)
       │              not_found (retries later)                       ▼
       │                                                          importing ──▶ have
    missing  (auto-download off: waits for you to click Download)
    skipped  (release type you don't follow, or you skipped it)
    failed   (downloaded but couldn't import — usually a folder setting)
"""
import json
import os
import re
import shutil
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from .db import DB, DEFAULTS, Settings, now_iso, parse_iso
from .dupes import DupeScanner
from .auth import Auth
from .importer import LibraryImporter
from .updater import Updater
from .library import Library
from .locate import Finder, split_remote
from .matcher import explain, rank_candidates, search_queries
from .mb import MusicBrainz
from .slskd import Slskd, SlskdError

ACTIVE = ("searching", "queued", "downloading", "importing")
# Minimum seconds between starting two Soulseek searches.
SEARCH_SPACING = float(os.environ.get("CRATE_SEARCH_SPACING", "8"))
BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _move(src, target):
    """Move a file; if it can't be removed from the downloads folder, leave a copy there.
    Returns False when the source was kept."""
    try:
        os.rename(src, target)
        return True
    except OSError:
        pass
    shutil.copy2(src, target)
    try:
        os.unlink(src)
        return True
    except OSError:
        return False


def _note(n):
    """'"Thriller": 41 folders from 30 users (25 tracklist didn't match, …)'"""
    if not n.get("users"):
        return f'"{n["q"]}": no results'
    out = f'"{n["q"]}": {n["folders"]} folder{"" if n["folders"] == 1 else "s"} from {n["users"]} user{"" if n["users"] == 1 else "s"}'
    return out + (f" ({n['why']})" if n.get("why") else "")


def _utcnow():
    return datetime.now(timezone.utc)


def _loads(s, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def _safe(part):
    part = BAD_CHARS.sub("_", part or "").strip().rstrip(". ")
    return part or "_"


class App:
    def __init__(self, db_path):
        self.db = DB(db_path)
        self.settings = Settings(self.db)
        self.mb = MusicBrainz(self.settings)
        self.library = Library(self.settings)
        self.dupes = DupeScanner(self)
        self.importer = LibraryImporter(self)
        self.auth = Auth(self.db)
        self.updater = Updater(self, db_path)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._sync_lock = threading.Lock()
        self.worker_error = None
        self.last_tick = None
        self.last_scan = 0.0
        # Search throttling / back-off (see _start_searches).
        self.last_search_start = 0.0
        self.search_pause_until = 0.0
        self.search_failures = 0
        self.slskd_status = {"ok": None, "message": "Not checked yet"}
        # Clean up states interrupted by a restart.
        self.db.x("UPDATE artists SET sync_status = 'pending' WHERE sync_status = 'syncing'")

    # ------------------------------------------------------------------ utils
    def slskd(self):
        return Slskd(self.settings.slskd_url, self.settings.slskd_api_key)

    def configured(self):
        return bool(self.settings.slskd_url and self.settings.slskd_api_key)

    def album(self, album_id):
        return self.db.one(
            "SELECT al.*, ar.name AS artist_name, ar.local_name FROM albums al "
            "JOIN artists ar ON ar.id = al.artist_id WHERE al.id = ?", (album_id,))

    def check_slskd(self):
        prev = self.slskd_status.get("ok")
        if not self.configured():
            self.slskd_status = {"ok": False, "message": "Add your slskd URL and API key in Settings"}
            return self.slskd_status
        try:
            st = self.slskd().server_state()
            state = st.get("state") or "unknown"
            logged_in = st.get("isLoggedIn", "LoggedIn" in state)
            if logged_in:
                self.slskd_status = {"ok": True, "message": f"Connected to Soulseek as {st.get('username') or 'user'}"}
            else:
                self.slskd_status = {"ok": False, "message": (
                    f"slskd isn't logged in to the Soulseek network ({state}). Searches and downloads are "
                    "paused until it reconnects; check the Soulseek username/password in slskd.")}
        except SlskdError as e:
            self.slskd_status = {"ok": False, "message": str(e) + " Searches and downloads are paused."}
        now_ok = self.slskd_status["ok"]
        if prev is True and not now_ok:
            self.db.event(f"Lost connection: {self.slskd_status['message']}", "error")
        elif prev is False and now_ok:
            self.search_pause_until = 0.0
            self.search_failures = 0
            self.db.event(f"slskd is back: {self.slskd_status['message']}. Resuming.", "success")
        return self.slskd_status

    def clear_searches(self):
        """Delete every finished search from slskd's search list (Crate's or yours).
        Searches still running, and the ones Crate is currently waiting on, are kept."""
        sl = self.slskd()
        mine = {r["search_id"] for r in self.db.q(
            "SELECT search_id FROM albums WHERE status = 'searching' AND search_id IS NOT NULL")}
        cleared = kept = 0
        for s in sl.list_searches():
            sid = s.get("id")
            done = s.get("isComplete") or str(s.get("state", "")).startswith("Completed")
            if not sid or sid in mine or not done:
                kept += 1
                continue
            sl.delete_search(sid)
            cleared += 1
        if cleared:
            self.db.event(f"Cleared {cleared} finished search(es) from slskd"
                          + (f"; kept {kept} still running" if kept else ""))
        return {"cleared": cleared, "kept": kept}

    def find_in_library(self, artist_name, title, local_name=None):
        """Path of this album in the library, matching the MusicBrainz artist name or the
        artist's folder name in your library (they can differ, e.g. 'AC_DC' vs 'AC/DC')."""
        path = self.library.find(artist_name, title)
        if not path and local_name and local_name != artist_name:
            path = self.library.find(local_name, title)
        return path

    def rescan_library(self):
        n = self.library.scan()
        self.last_scan = time.monotonic()
        changed = 0
        for a in self.db.q(
            "SELECT al.id, al.title, ar.name AS artist_name, ar.local_name FROM albums al "
            "JOIN artists ar ON ar.id = al.artist_id "
            "WHERE al.status IN ('wanted', 'missing', 'not_found', 'failed')"):
            path = self.find_in_library(a["artist_name"], a["title"], a["local_name"])
            if path:
                self.db.update_album(a["id"], status="have", status_detail="Already in your library", path=path)
                changed += 1
        return n, changed

    # --------------------------------------------------------------- artists
    def artist_info(self, mbid):
        """Picture + notable releases for an artist, cached for 30 days."""
        row = self.db.one("SELECT data, fetched_at FROM artist_info WHERE mbid = ?", (mbid,))
        if row:
            fetched = parse_iso(row["fetched_at"])
            if fetched and (_utcnow() - fetched) < timedelta(days=30):
                info = _loads(row["data"], {})
                if "titles" not in info:
                    info = None  # cached before 1.1; refetch to get release titles
            else:
                info = None
            if info is not None:
                if info.get("image"):
                    self.db.x("UPDATE artists SET image = ? WHERE mbid = ? AND (image IS NULL OR image = '')",
                              (info["image"], mbid))
                return info
        info = self.mb.artist_info(mbid)
        self.db.x("INSERT INTO artist_info (mbid, data, fetched_at) VALUES (?,?,?) "
                  "ON CONFLICT(mbid) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
                  (mbid, json.dumps(info), now_iso()))
        if info.get("image"):
            self.db.x("UPDATE artists SET image = ? WHERE mbid = ?", (info["image"], mbid))
        return info

    def add_artist(self, mbid, added_by=None, auto_download=None, monitor="all"):
        monitor = "new" if monitor == "new" else "all"
        existing = self.db.one("SELECT * FROM artists WHERE mbid = ?", (mbid,))
        if existing:
            if existing.get("followed") == 0:  # only had one-off requests so far
                return self.follow_artist(existing["id"], added_by, monitor, auto_download)
            return existing, False
        info = self.mb.get_artist(mbid)
        aid = self.db.x(
            "INSERT INTO artists (mbid, name, sort_name, disambiguation, country, added_at, sync_status, "
            "added_by, auto_download, monitor, monitor_since) VALUES (?,?,?,?,?,?, 'pending', ?, ?, ?, ?)",
            (info["mbid"], info["name"], info["sort_name"], info["disambiguation"], info["country"], now_iso(),
             added_by, None if auto_download is None else (1 if auto_download else 0),
             monitor, _utcnow().date().isoformat() if monitor == "new" else None),
        )
        self.db.event(f"Added artist {info['name']}" + (f" (by {added_by})" if added_by else ""),
                      artist_id=aid, username=added_by)
        threading.Thread(target=self.sync_artist, args=(aid,), daemon=True).start()
        return self.db.one("SELECT * FROM artists WHERE id = ?", (aid,)), True

    def remove_artist(self, artist_id):
        for a in self.db.q("SELECT id FROM albums WHERE artist_id = ? AND status IN (?,?,?,?)", (artist_id, *ACTIVE)):
            self._cancel_active(self.album(a["id"]))
        artist = self.db.one("SELECT name FROM artists WHERE id = ?", (artist_id,))
        self.db.x("DELETE FROM albums WHERE artist_id = ?", (artist_id,))
        self.db.x("DELETE FROM artists WHERE id = ?", (artist_id,))
        if artist:
            self.db.event(f"Removed artist {artist['name']} (files on disk were not touched)")

    # ------------------------------------------------------------ follow rules
    def artist_rules(self, artist):
        """What to fetch for this artist: its own settings, falling back to the global ones."""
        own = [t.strip().lower() for t in (artist.get("release_types") or "").split(",") if t.strip()]
        inc = artist.get("include_secondary")
        if inc is None:
            excl = self.settings.list("exclude_secondary")
        elif inc:
            excl = []
        else:  # explicitly "leave them out", even if the global list was emptied
            excl = self.settings.list("exclude_secondary") or [
                p.strip() for p in DEFAULTS["exclude_secondary"].split(",")]
        auto = (bool(artist["auto_download"]) if artist.get("auto_download") is not None
                else self.settings.bool("auto_download"))
        return {"types": own or self.settings.list("release_types"), "exclude": excl, "auto": auto,
                "monitor": artist.get("monitor") or "all", "since": artist.get("monitor_since") or ""}

    @staticmethod
    def classify(rules, g):
        """(status, detail, skip_reason) for an album that isn't in the library."""
        sec = g.get("secondary_types") or []
        if isinstance(sec, str):
            sec = _loads(sec, [])
        ptype = g.get("primary_type") or "Other"
        if ptype.lower() not in rules["types"] or any(s.lower() in rules["exclude"] for s in sec):
            kind = ", ".join([ptype] + list(sec))
            return "skipped", f"{kind} — not a release type you follow", "type"
        if rules["monitor"] == "new":
            date = g.get("release_date") or ""
            if not date or date < rules["since"][:len(date)]:
                return "skipped", "Released before you followed (new releases only)", "before_follow"
        if rules["auto"]:
            return "wanted", None, None
        return "missing", "Click Download to fetch", None

    def reapply_rules(self, artist_id):
        """Re-sort missing/skipped albums after the rules changed. Leaves alone what you
        have, what's in progress, what you skipped by hand and what someone requested."""
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))
        if not artist or not (artist.get("followed") if artist.get("followed") is not None else 1):
            return 0
        rules = self.artist_rules(artist)
        changed = 0
        for a in self.db.q(
                "SELECT * FROM albums WHERE artist_id = ? AND requested_by IS NULL AND ("
                "status = 'missing' OR (status = 'wanted' AND COALESCE(attempts, 0) = 0 "
                "AND COALESCE(query_index, 0) = 0 AND search_id IS NULL) "
                "OR (status = 'skipped' AND skip_reason IN ('type', 'before_follow')))", (artist_id,)):
            status, detail, reason = self.classify(rules, a)
            if status == a["status"] and reason == a.get("skip_reason"):
                continue
            if status == "wanted":
                detail = "Waiting to search"
            self.db.update_album(a["id"], status=status, status_detail=detail, skip_reason=reason)
            changed += 1
        if changed:
            self.wake.set()
        return changed

    def reapply_all(self):
        n = 0
        for a in self.db.q("SELECT id FROM artists WHERE COALESCE(followed, 1) = 1"):
            n += self.reapply_rules(a["id"])
        return n

    def set_artist_rules(self, artist_id, **fields):
        allowed = {"release_types", "include_secondary", "auto_download", "monitor"}
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))
        if not artist:
            raise KeyError("artist not found")
        sets = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "release_types":
                if v is None or v == "" or v == []:
                    v = None
                else:
                    parts = v if isinstance(v, list) else str(v).split(",")
                    v = ",".join(p.strip().lower() for p in parts if p.strip()) or None
            elif k in ("include_secondary", "auto_download"):
                v = None if v is None or v == "" else (1 if v in (True, 1, "1", "true", "yes", "on") else 0)
            elif k == "monitor":
                if v not in ("all", "new"):
                    raise ValueError("monitor must be 'all' or 'new'")
                if v == "new" and artist.get("monitor") != "new":
                    sets["monitor_since"] = _utcnow().date().isoformat()
            sets[k] = v
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            self.db.x(f"UPDATE artists SET {cols} WHERE id = ?", (*sets.values(), artist_id))
        return self.reapply_rules(artist_id)

    def follow_artist(self, artist_id, username=None, monitor="all", auto_download=None):
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))
        if not artist:
            raise KeyError("artist not found")
        if artist.get("followed") in (None, 1):
            return artist, False
        self.db.x("UPDATE artists SET followed = 1, monitored = 1, monitor = ?, monitor_since = ?, "
                  "added_by = COALESCE(added_by, ?), auto_download = ?, last_synced = NULL WHERE id = ?",
                  (monitor, _utcnow().date().isoformat() if monitor == "new" else None, username,
                   None if auto_download is None else (1 if auto_download else 0), artist_id))
        self.db.event(f"Now following {artist['name']}" + (f" (by {username})" if username else ""),
                      artist_id=artist_id, username=username)
        threading.Thread(target=self.sync_artist, args=(artist_id,), daemon=True).start()
        return self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,)), True

    def request_album(self, rg_mbid, username=None):
        """Fetch one album without following its artist. Returns the album row."""
        row = self.db.one("SELECT id FROM albums WHERE mbid = ?", (rg_mbid,))
        if row:
            return self.album_action(row["id"], "download", username), False
        g = self.mb.release_group(rg_mbid)
        if not g.get("artist_mbid"):
            raise ValueError("That release has no artist on MusicBrainz")
        artist = self.db.one("SELECT * FROM artists WHERE mbid = ?", (g["artist_mbid"],))
        if not artist:
            info = self.mb.get_artist(g["artist_mbid"])
            aid = self.db.x(
                "INSERT INTO artists (mbid, name, sort_name, disambiguation, country, added_at, sync_status, "
                "added_by, followed) VALUES (?,?,?,?,?,?, 'ok', ?, 0)",
                (info["mbid"], info["name"], info["sort_name"], info["disambiguation"], info["country"],
                 now_iso(), username))
            threading.Thread(target=self._artist_image, args=(info["mbid"],), daemon=True).start()
            artist = self.db.one("SELECT * FROM artists WHERE id = ?", (aid,))
        album_id = self.db.x(
            "INSERT INTO albums (mbid, artist_id, title, primary_type, secondary_types, release_date, year, "
            "status, status_detail, updated_at) VALUES (?,?,?,?,?,?,?, 'missing', NULL, ?)",
            (g["mbid"], artist["id"], g["title"], g["primary_type"], json.dumps(g["secondary_types"]),
             g["release_date"], g["year"], now_iso()))
        self.db.event(f"Requested {artist['name']} — {g['title']}" + (f" (by {username})" if username else ""),
                      artist_id=artist["id"], album_id=album_id, username=username)
        return self.album_action(album_id, "download", username), True

    def _artist_image(self, mbid):
        try:
            self.artist_info(mbid)
        except Exception:
            pass

    def sync_artist(self, artist_id):
        with self._sync_lock:
            artist = self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))
            if not artist:
                return
            self.db.x("UPDATE artists SET sync_status = 'syncing' WHERE id = ?", (artist_id,))
            if not artist.get("image"):
                try:
                    self.artist_info(artist["mbid"])
                except Exception:
                    pass
            try:
                groups = self.mb.release_groups(artist["mbid"])
                rules = self.artist_rules(artist)
                auto = rules["auto"]
                followed = artist.get("followed") is None or bool(artist.get("followed"))
                if time.monotonic() - self.last_scan > 60:
                    self.library.scan()
                    self.last_scan = time.monotonic()
                new = have = 0
                for g in groups:
                    row = self.db.one("SELECT id FROM albums WHERE mbid = ?", (g["mbid"],))
                    if not row and not followed:
                        continue  # request-only artist: just refresh the albums you asked for
                    if row:
                        self.db.update_album(row["id"], title=g["title"], release_date=g["release_date"],
                                             year=g["year"], primary_type=g["primary_type"],
                                             secondary_types=json.dumps(g["secondary_types"]))
                        continue
                    path = self.find_in_library(artist["name"], g["title"], artist.get("local_name"))
                    if path:
                        status, detail, reason = "have", "Already in your library", None
                        have += 1
                    else:
                        status, detail, reason = self.classify(rules, g)
                    self.db.x(
                        "INSERT INTO albums (mbid, artist_id, title, primary_type, secondary_types, release_date, "
                        "year, status, status_detail, path, skip_reason, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (g["mbid"], artist_id, g["title"], g["primary_type"], json.dumps(g["secondary_types"]),
                         g["release_date"], g["year"], status, detail, path, reason, now_iso()),
                    )
                    new += 1
                self.db.x("UPDATE artists SET last_synced = ?, sync_status = 'ok' WHERE id = ?",
                          (now_iso(), artist_id))
                wanted = self.db.one("SELECT COUNT(*) n FROM albums WHERE artist_id = ? AND status = 'wanted'",
                                     (artist_id,))["n"]
                missing = self.db.one("SELECT COUNT(*) n FROM albums WHERE artist_id = ? AND status = 'missing'",
                                      (artist_id,))["n"]
                if new:
                    tail = f"{wanted} queued to download" if auto else f"{missing} missing (not set to download)"
                    self.db.event(
                        f"{artist['name']}: found {new} new release(s); {have} already in library, {tail}",
                        artist_id=artist_id)
                self.wake.set()
            except Exception as e:
                self.db.x("UPDATE artists SET sync_status = ? WHERE id = ?", (f"error: {e}", artist_id))
                self.db.event(f"Couldn't fetch discography for {artist['name']}: {e}", "error", artist_id=artist_id)

    # ---------------------------------------------------------------- albums
    def album_action(self, album_id, action, username=None):
        a = self.album(album_id)
        if not a:
            raise KeyError("album not found")
        if action in ("download", "retry", "want"):
            self._cancel_active(a)
            path = self.find_in_library(a["artist_name"], a["title"], a.get("local_name"))
            if path and action != "retry":
                self.db.update_album(album_id, status="have", status_detail="Already in your library", path=path)
            else:
                self.db.update_album(album_id, status="wanted", status_detail="Waiting to search", query_index=0,
                                     skip_reason=None,
                                     candidates=None, candidate_index=0, tried_users="[]", attempts=0,
                                     next_retry=None, search_id=None,
                                     requested_by=username, requested_at=now_iso() if username else None)
        elif action == "reimport":
            if not a.get("dl_files"):
                raise ValueError("Nothing downloaded to import; use Search again")
            self.db.update_album(album_id, status="importing", status_detail="Looking for the downloaded files",
                                 dl_last_change=now_iso())
            self._import(self.slskd(), album_id)
        elif action == "skip":
            self._cancel_active(a)
            self.db.update_album(album_id, status="skipped", status_detail="Skipped by you", skip_reason="user")
        elif action == "next_source":
            if a["status"] == "downloading":
                self._abandon(self.slskd(), a, "skipped by you")
        elif action == "recheck":
            self.library.scan()
            path = self.find_in_library(a["artist_name"], a["title"], a.get("local_name"))
            if path:
                self.db.update_album(album_id, status="have", status_detail="Already in your library", path=path)
        else:
            raise ValueError(f"unknown action {action}")
        self.wake.set()
        return self.album(album_id)

    def _cancel_active(self, a):
        if not a or not self.configured():
            return
        sl = self.slskd()
        try:
            if a["status"] == "searching" and a["search_id"]:
                sl.delete_search(a["search_id"])
            if a["status"] == "downloading" and a["dl_username"]:
                names = {f["filename"] for f in _loads(a["dl_files"], [])}
                for t in sl.user_downloads(a["dl_username"]):
                    if t.get("filename") in names and "Succeeded" not in (t.get("state") or ""):
                        sl.cancel_download(a["dl_username"], t["id"])
        except SlskdError:
            pass

    # ---------------------------------------------------------------- worker
    def run_worker(self):
        while not self.stop.is_set():
            try:
                self.tick()
                self.worker_error = None
            except Exception as e:  # keep the loop alive no matter what
                self.worker_error = str(e)
                traceback.print_exc()
            self.last_tick = now_iso()
            self.wake.wait(float(os.environ.get("CRATE_TICK_SECONDS", "5")))
            self.wake.clear()

    def tick(self):
        try:
            self.updater.periodic()
        except Exception:
            pass
        self._periodic_sync()
        if not self.configured():
            return
        sl = self.slskd()
        # Never search or download unless slskd is actually logged in to Soulseek:
        # searches sent while disconnected fail instantly (HTTP 400).
        st = self.check_slskd()
        if not st["ok"]:
            # Pause stall timers so downloads aren't abandoned because of the outage.
            self.db.x("UPDATE albums SET dl_last_change = ? WHERE status = 'downloading'", (now_iso(),))
            return
        try:
            self._poll_downloads(sl)
            self._poll_imports(sl)
            self._poll_searches(sl)
            self._start_queued(sl)
            self._start_searches(sl)
        except SlskdError as e:
            self.slskd_status = {"ok": False, "message": str(e)}
            raise

    def _periodic_sync(self):
        if self._sync_lock.locked():
            return
        hours = max(self.settings.int("sync_hours"), 1)
        cutoff = (_utcnow() - timedelta(hours=hours)).replace(microsecond=0).isoformat()
        stale = self.db.one(
            "SELECT id FROM artists WHERE monitored = 1 AND COALESCE(followed, 1) = 1 AND sync_status != 'syncing' "
            "AND (last_synced IS NULL OR last_synced < ?) ORDER BY last_synced LIMIT 1", (cutoff,))
        if stale:
            threading.Thread(target=self.sync_artist, args=(stale["id"],), daemon=True).start()

    def _count(self, *statuses):
        marks = ",".join("?" * len(statuses))
        return self.db.one(f"SELECT COUNT(*) n FROM albums WHERE status IN ({marks})", statuses)["n"]

    # -- searching
    def _start_searches(self, sl):
        max_s = max(self.settings.int("max_active_searches"), 1)
        max_d = max(self.settings.int("max_active_downloads"), 1)
        slots = max_s - self._count("searching")
        if slots <= 0 or self._count("queued", "downloading") >= max_d * 2:
            return
        mono = time.monotonic()
        if mono < self.search_pause_until:
            return
        # Space searches out so Soulseek never sees a burst from us.
        if mono - self.last_search_start < SEARCH_SPACING:
            return
        slots = 1  # at most one new search per tick
        now = now_iso()
        today = _utcnow().date().isoformat()
        rows = self.db.q(
            "SELECT al.*, ar.name AS artist_name, ar.local_name FROM albums al JOIN artists ar ON ar.id = al.artist_id "
            "WHERE ar.monitored = 1 "
            "AND ((al.status = 'wanted' AND (al.next_retry IS NULL OR al.next_retry <= ?1)) "
            "     OR (al.status = 'not_found' AND al.next_retry <= ?1)) "
            "AND (al.release_date IS NULL OR al.release_date = '' OR al.release_date <= ?2) "
            "ORDER BY al.status = 'not_found', al.next_retry IS NOT NULL, ar.added_at, al.release_date DESC "
            "LIMIT ?3",
            (now, today, slots))
        if rows and time.monotonic() - self.last_scan > 3600:
            self.library.scan()
            self.last_scan = time.monotonic()
        for a in rows:
            path = self.find_in_library(a["artist_name"], a["title"], a.get("local_name"))
            if path:
                self.db.update_album(a["id"], status="have", status_detail="Already in your library", path=path)
                continue
            if a["tracks"] is None:
                try:
                    count, titles = self.mb.tracklist(a["mbid"])
                    self.db.update_album(a["id"], track_count=count, tracks=json.dumps(titles))
                    a["track_count"], a["tracks"] = count, json.dumps(titles)
                except Exception as e:
                    self.db.event(f"Couldn't fetch tracklist for {a['title']}: {e}", "warn", album_id=a["id"])
            queries = search_queries(a["artist_name"], a["title"])
            qi = a["query_index"] or 0
            if qi >= len(queries):
                qi = 0
            self.last_search_start = time.monotonic()
            try:
                sid = sl.start_search(queries[qi], self.settings.int("search_timeout"))
            except SlskdError as e:
                msg = str(e)
                if "HTTP 400" in msg:
                    msg = "slskd rejected the search (HTTP 400) — usually it isn't logged in to Soulseek"
                self._search_failed(a, msg)
                return
            self.search_failures = 0
            self.db.update_album(a["id"], status="searching", search_id=sid, search_started=now_iso(),
                                 query_index=qi, candidates=None, candidate_index=0, next_retry=None,
                                 status_detail=f'Searching Soulseek for "{queries[qi]}"')

    def _search_failed(self, a, reason):
        """A search couldn't run (not a 'no results'). Retry this album later and pause
        all new searches for a while, doubling the pause on repeated failures."""
        self.search_failures += 1
        pause = min(5 * 2 ** (self.search_failures - 1), 30)
        self.search_pause_until = time.monotonic() + pause * 60
        retry = (_utcnow() + timedelta(minutes=max(15, pause))).replace(microsecond=0).isoformat()
        self.db.update_album(a["id"], status="wanted", search_id=None, next_retry=retry,
                             status_detail=f"{reason} — will retry in {max(15, pause)} min")
        self.db.event(f"Search for {a['artist_name']} – {a['title']} failed ({reason}). "
                      f"Pausing new searches for {pause} min.", "warn", album_id=a["id"], artist_id=a["artist_id"])

    def _poll_searches(self, sl):
        timeout = self.settings.int("search_timeout")
        for a in self.db.q(
                "SELECT al.*, ar.name AS artist_name FROM albums al JOIN artists ar ON ar.id = al.artist_id "
                "WHERE al.status = 'searching'"):
            st = sl.search_state(a["search_id"]) if a["search_id"] else None
            if st is None:
                retry = (_utcnow() + timedelta(minutes=2)).replace(microsecond=0).isoformat()
                self.db.update_album(a["id"], status="wanted", search_id=None, next_retry=retry,
                                     status_detail="Search disappeared from slskd; retrying in 2 min")
                continue
            started = parse_iso(a["search_started"]) or _utcnow()
            elapsed = (_utcnow() - started).total_seconds()
            done = st.get("isComplete") or str(st.get("state", "")).startswith("Completed") or elapsed > timeout + 90
            if not done:
                self.db.update_album(a["id"], status_detail=(
                    f"Searching… {st.get('responseCount', 0)} user{'' if st.get('responseCount') == 1 else 's'} responded"))
                continue
            state = str(st.get("state", ""))
            if "Errored" in state and not st.get("responseCount"):
                sl.delete_search(a["search_id"])
                self._search_failed(a, f"search errored in slskd ({state.replace('Completed, ', '')})")
                continue
            responses = sl.search_responses(a["search_id"])
            sl.delete_search(a["search_id"])
            tried = _loads(a["tried_users"], [])
            stats = {}
            cands = rank_candidates(
                responses, a["artist_name"], a["title"],
                track_count=a["track_count"], track_titles=_loads(a["tracks"], []),
                preferred_formats=self.settings.list("preferred_formats"),
                min_bitrate=self.settings.int("min_bitrate"),
                primary_type=a["primary_type"], exclude_users=tried, stats=stats)
            queries = search_queries(a["artist_name"], a["title"])
            qi = a["query_index"] or 0
            notes = _loads(a.get("search_notes"), []) if qi else []
            notes.append({"q": queries[qi] if qi < len(queries) else "", "users": len(responses),
                          "folders": stats.get("folders", 0), "why": explain(stats)})
            self.db.update_album(a["id"], search_notes=json.dumps(notes[-5:]))
            if cands:
                best = cands[0]
                self.db.update_album(a["id"], status="queued", search_id=None, candidates=json.dumps(cands),
                                     candidate_index=0,
                                     status_detail=f"{len(cands)} source(s) found — best: {best['format'].upper()} "
                                                   f"from {best['username']}")
                continue
            qi += 1
            if qi < len(queries):
                self.db.update_album(a["id"], status="wanted", search_id=None, query_index=qi,
                                     status_detail=f'Nothing usable for "{notes[-1]["q"]}"; trying "{queries[qi]}"')
            else:
                self._not_found(a, "No suitable source. " + "; ".join(_note(n) for n in notes))

    def _not_found(self, a, reason):
        attempts = (a["attempts"] or 0) + 1
        hours = max(self.settings.int("retry_hours"), 1) * min(attempts, 7)
        retry = (_utcnow() + timedelta(hours=hours)).replace(microsecond=0).isoformat()
        self.db.update_album(a["id"], status="not_found", search_id=None, query_index=0, attempts=attempts,
                             next_retry=retry, tried_users="[]", candidates=None,
                             dl_username=None, dl_files=None,
                             status_detail=reason)
        self.db.event(f"{a['artist_name']} – {a['title']}: {reason[:1].lower() + reason[1:]} — retrying in {hours}h",
                      "warn", album_id=a["id"], artist_id=a["artist_id"])

    # -- downloading
    def _start_queued(self, sl):
        max_d = max(self.settings.int("max_active_downloads"), 1)
        slots = max_d - self._count("downloading")
        if slots <= 0:
            return
        for a in self.db.q(
                "SELECT al.*, ar.name AS artist_name FROM albums al JOIN artists ar ON ar.id = al.artist_id "
                "WHERE al.status = 'queued' ORDER BY al.updated_at LIMIT ?", (slots,)):
            self._begin_download(sl, a)

    def _begin_download(self, sl, a):
        cands = _loads(a["candidates"], [])
        tried = set(_loads(a["tried_users"], []))
        idx = a["candidate_index"] or 0
        while idx < len(cands):
            c = cands[idx]
            if c["username"] in tried:
                idx += 1
                continue
            try:
                sl.enqueue(c["username"], [{"filename": f["filename"], "size": f["size"]} for f in c["files"]])
            except SlskdError as e:
                self.db.event(f"Couldn't queue from {c['username']}: {e}", "warn", album_id=a["id"])
                tried.add(c["username"])
                idx += 1
                continue
            self.db.update_album(
                a["id"], status="downloading", candidate_index=idx, tried_users=json.dumps(sorted(tried)),
                dl_username=c["username"], dl_directory=c["directory"], dl_files=json.dumps(c["files"]),
                dl_started=now_iso(), dl_last_change=now_iso(), dl_bytes=0, dl_size=c["size"], dl_retries=0,
                status_detail=f"Queued {c['track_files']} {c['format'].upper()} files from {c['username']}")
            self.db.event(
                f"Downloading {a['artist_name']} – {a['title']} ({c['format'].upper()}, {c['track_files']} tracks) "
                f"from {c['username']}", album_id=a["id"], artist_id=a["artist_id"])
            return True
        self._not_found(a, "Every source found failed or was unavailable")
        return False

    def _poll_downloads(self, sl):
        rows = self.db.q(
            "SELECT al.*, ar.name AS artist_name FROM albums al JOIN artists ar ON ar.id = al.artist_id "
            "WHERE al.status = 'downloading'")
        by_user = {}
        for a in rows:
            by_user.setdefault(a["dl_username"], []).append(a)
        for user, albums in by_user.items():
            try:
                transfers = sl.user_downloads(user)
            except SlskdError:
                continue
            tmap = {}
            for t in transfers:
                prev = tmap.get(t.get("filename"))
                if prev is None or (t.get("requestedAt") or "") >= (prev.get("requestedAt") or ""):
                    tmap[t.get("filename")] = t
            for a in albums:
                self._check_download(sl, a, tmap)

    def _check_download(self, sl, a, tmap):
        files = _loads(a["dl_files"], [])
        done, failed, missing, active_states = 0, [], [], []
        got = 0
        for f in files:
            t = tmap.get(f["filename"])
            if not t:
                missing.append(f)
                continue
            st = t.get("state") or ""
            if "Succeeded" in st:
                done += 1
                got += f["size"]
            elif st.startswith("Completed"):
                failed.append((f, t))
            else:
                got += int(t.get("bytesTransferred") or 0)
                active_states.append(st)
        now = _utcnow()
        if got != (a["dl_bytes"] or 0):
            self.db.update_album(a["id"], dl_bytes=got, dl_last_change=now_iso())
            a["dl_last_change"] = now_iso()

        if files and done == len(files):
            self.db.update_album(a["id"], status="importing", status_detail="Download complete — importing",
                                 dl_last_change=now_iso())
            self._import(sl, a["id"])
            return

        started = parse_iso(a["dl_started"]) or now
        lost = missing and (now - started).total_seconds() > 180
        if failed or lost:
            if failed and not missing and (a["dl_retries"] or 0) < 1:
                for f, t in failed:
                    sl.cancel_download(a["dl_username"], t["id"])
                try:
                    sl.enqueue(a["dl_username"], [{"filename": f["filename"], "size": f["size"]} for f, _ in failed])
                    self.db.update_album(a["id"], dl_retries=(a["dl_retries"] or 0) + 1, dl_last_change=now_iso(),
                                         status_detail=f"Retrying {len(failed)} failed file(s) from {a['dl_username']}")
                    return
                except SlskdError:
                    pass
            why = (failed[0][1].get("state") or "error").replace("Completed, ", "") if failed else "transfers disappeared"
            self._abandon(sl, a, f"{len(failed) or len(missing)} file(s) failed ({why})", tmap)
            return

        last = parse_iso(a["dl_last_change"]) or started
        stall = max(self.settings.int("stall_minutes"), 5) * 60
        if (now - last).total_seconds() > stall:
            queued = active_states and all("Queued" in s for s in active_states)
            self._abandon(sl, a, "stuck in their upload queue" if queued and done == 0 else "download stalled", tmap)
            return

        size = a["dl_size"] or 1
        if active_states and done == 0 and got == 0 and all("Queued" in s for s in active_states):
            detail = f"Waiting in {a['dl_username']}'s upload queue"
        else:
            detail = f"{done}/{len(files)} files · {min(100, int(got * 100 / size))}% from {a['dl_username']}"
        self.db.update_album(a["id"], status_detail=detail)

    def _abandon(self, sl, a, reason, tmap=None):
        user = a["dl_username"]
        names = {f["filename"] for f in _loads(a["dl_files"], [])}
        try:
            transfers = list(tmap.values()) if tmap is not None else sl.user_downloads(user)
            for t in transfers:
                if t.get("filename") in names and "Succeeded" not in (t.get("state") or ""):
                    sl.cancel_download(user, t["id"])
        except SlskdError:
            pass
        tried = set(_loads(a["tried_users"], []))
        tried.add(user)
        self.db.event(f"{a['artist_name']} – {a['title']}: source {user} dropped ({reason}); trying next source",
                      "warn", album_id=a["id"], artist_id=a["artist_id"])
        self.db.update_album(a["id"], status="queued", candidate_index=(a["candidate_index"] or 0) + 1,
                             tried_users=json.dumps(sorted(tried)), dl_username=None, dl_files=None,
                             dl_bytes=0, status_detail=f"Previous source failed ({reason}); trying next")

    # -- importing
    def _poll_imports(self, sl):
        for a in self.db.q("SELECT id FROM albums WHERE status = 'importing'"):
            self._import(sl, a["id"])

    def _import_wait(self, a, waited, limit, why, missing=()):
        """Keep looking for the files for `limit` seconds, then give up with the reason."""
        why = why.rstrip(". ") + "."
        if waited < limit:
            self.db.update_album(a["id"], status_detail=f"Looking for the downloaded files: {why}")
            return
        names = ", ".join(split_remote(f["filename"])[-1] for f, _ in list(missing)[:5])
        more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
        self.db.update_album(a["id"], status="failed", status_detail=f"Couldn't import: {why} Fix it, then click Import again.")
        self.db.event(f"Import failed for {a['artist_name']} – {a['title']}: {why}"
                      + (f" Missing: {names}{more}" if names else ""), "error", album_id=a["id"], artist_id=a["artist_id"])

    def render_path(self, artist, album, year, primary_type):
        tpl = self.settings.folder_template or "{artist}/{album} ({year})"
        vals = {"artist": artist, "album": album, "year": year or "", "type": primary_type or ""}
        parts = []
        for piece in re.split(r"[\\/]", tpl):
            try:
                s = piece.format(**vals)
            except (KeyError, IndexError, ValueError):
                s = piece
            s = re.sub(r"\s*[\(\[]\s*[\)\]]", "", s).strip(" -")
            if s:
                parts.append(_safe(s))
        return os.path.join(*parts) if parts else _safe(f"{artist} - {album}")

    def _import(self, sl, album_id):
        a = self.album(album_id)
        if not a or a["status"] != "importing":
            return
        files = _loads(a["dl_files"], [])
        root = os.path.abspath(os.path.expanduser(self.settings.slskd_downloads_dir))
        last = parse_iso(a["dl_last_change"]) or _utcnow()
        waited = (_utcnow() - last).total_seconds()
        if not os.path.isdir(root):
            return self._import_wait(a, waited, 120, f"Crate can't see the downloads folder {root}. "
                                     "Check the 'slskd downloads folder' setting and that it's mounted in Crate's container.")
        finder = Finder(root)
        saved, located, missing, taken = None, [], [], set()
        for f in files:
            p, why = finder.find(f, a["dl_username"], None, taken)
            if not p:
                if saved is None:
                    try:
                        saved = sl.completed_files() or {}
                    except SlskdError:
                        saved = {}
                p, why = finder.find(f, a["dl_username"], saved, taken)
            if p and not finder.readable(p):
                p, why = None, f"Crate isn't allowed to read {p} (permission denied)"
            if p:
                taken.add(p)
                located.append((f, p))
            else:
                missing.append((f, why))
        if missing:
            perm = finder.unreadable or [w for _, w in missing if w and "permission" in w]
            name = split_remote(missing[0][0]["filename"])[-1]
            if finder.unreadable:
                why = (f"Crate isn't allowed to open {finder.unreadable[0]} (permission denied). slskd saves files "
                       "as a different user: make Crate's PUID/PGID match slskd's, or let others read them.")
            elif perm:
                why = perm[0] + ". Make Crate's PUID/PGID match slskd's."
            else:
                why = next((w for _, w in missing if w), None) or f'"{name}" isn\'t under {root}.'
            found = f"Found {len(located)} of {len(files)} files. " if located else ""
            return self._import_wait(a, waited, 120 if perm else 600, found + why, missing)

        mode = (self.settings.import_mode or "move").lower()
        kept = []
        if mode == "off":
            dest = os.path.dirname(located[0][1])
        else:
            lib = os.path.abspath(os.path.expanduser(self.settings.library_dir))
            dest = os.path.join(lib, self.render_path(a["artist_name"], a["title"], a["year"], a["primary_type"]))
            try:
                for f, src in located:
                    tdir = os.path.join(dest, _safe(f["sub"])) if f.get("sub") else dest
                    os.makedirs(tdir, exist_ok=True)
                    target = os.path.join(tdir, os.path.basename(src))
                    if os.path.exists(target):
                        if os.path.getsize(target) == os.path.getsize(src):
                            if mode == "move":
                                try:
                                    os.remove(src)
                                except OSError:
                                    kept.append(src)
                            continue
                        stem, ext = os.path.splitext(target)
                        target = f"{stem} (1){ext}"
                    if mode == "copy":
                        shutil.copy2(src, target)
                    elif not _move(src, target):
                        kept.append(src)
            except OSError as e:
                self.db.update_album(album_id, status="failed", status_detail=f"Couldn't write to library: {e}")
                self.db.event(f"Import failed for {a['title']}: {e}", "error", album_id=album_id)
                return
            if mode == "move":
                for _, src in located:
                    d = os.path.dirname(src)
                    while os.path.abspath(d).startswith(root) and os.path.abspath(d) != root:
                        try:
                            os.rmdir(d)
                        except OSError:
                            break
                        d = os.path.dirname(d)
        # Tidy slskd's transfer list.
        try:
            names = {f["filename"] for f in files}
            for t in sl.user_downloads(a["dl_username"]):
                if t.get("filename") in names and "Succeeded" in (t.get("state") or ""):
                    sl.cancel_download(a["dl_username"], t["id"], remove=True)
        except SlskdError:
            pass
        self.library.add(a["artist_name"], a["title"], dest)
        if kept:
            self.db.event(f"{a['artist_name']} – {a['title']}: copied {len(kept)} file(s) into your library but couldn't "
                          f"delete them from the downloads folder (permission denied). Make Crate's PUID/PGID match "
                          f"slskd's so it can move files.", "warn", album_id=album_id, artist_id=a["artist_id"])
        cand = (_loads(a["candidates"], []) or [{}])
        cand = cand[a["candidate_index"] or 0] if (a["candidate_index"] or 0) < len(cand) else {}
        fmt = (cand.get("format") or "").upper()
        self.db.update_album(album_id, status="have", path=dest, candidates=None,
                             status_detail=f"Downloaded {fmt} from {a['dl_username']}".replace("  ", " "))
        self.db.event(f"Added {a['artist_name']} – {a['title']} to your library", "success",
                      album_id=album_id, artist_id=a["artist_id"])
