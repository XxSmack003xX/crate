"""SQLite storage + settings for Crate.

One shared connection guarded by a re-entrant lock; good enough for a
single-user self-hosted app with a handful of threads.
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS artists (
    id             INTEGER PRIMARY KEY,
    mbid           TEXT UNIQUE NOT NULL,
    name           TEXT NOT NULL,
    sort_name      TEXT,
    disambiguation TEXT,
    country        TEXT,
    added_at       TEXT,
    last_synced    TEXT,
    sync_status    TEXT,
    monitored      INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS albums (
    id              INTEGER PRIMARY KEY,
    mbid            TEXT UNIQUE NOT NULL,
    artist_id       INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    primary_type    TEXT,
    secondary_types TEXT,
    release_date    TEXT,
    year            TEXT,
    track_count     INTEGER,
    tracks          TEXT,
    status          TEXT DEFAULT 'wanted',
    status_detail   TEXT,
    query_index     INTEGER DEFAULT 0,
    search_id       TEXT,
    search_started  TEXT,
    candidates      TEXT,
    candidate_index INTEGER DEFAULT 0,
    tried_users     TEXT DEFAULT '[]',
    dl_username     TEXT,
    dl_directory    TEXT,
    dl_files        TEXT,
    dl_started      TEXT,
    dl_last_change  TEXT,
    dl_bytes        INTEGER DEFAULT 0,
    dl_size         INTEGER DEFAULT 0,
    dl_retries      INTEGER DEFAULT 0,
    attempts        INTEGER DEFAULT 0,
    next_retry      TEXT,
    path            TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_albums_status ON albums(status);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist_id);
-- Similar artists per followed artist (ListenBrainz / Last.fm), for recommendations.
CREATE TABLE IF NOT EXISTS similar (
    artist_mbid TEXT PRIMARY KEY,
    data        TEXT,
    fetched_at  TEXT
);
CREATE TABLE IF NOT EXISTS rec_dismissed (
    mbid         TEXT PRIMARY KEY,
    username     TEXT,
    dismissed_at TEXT
);
CREATE TABLE IF NOT EXISTS artist_info (
    mbid       TEXT PRIMARY KEY,
    data       TEXT,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL COLLATE NOCASE,
    role        TEXT NOT NULL DEFAULT 'user',
    salt        TEXT NOT NULL,
    hash        TEXT NOT NULL,
    perms       TEXT DEFAULT '{}',
    daily_limit INTEGER DEFAULT 10,
    created_at  TEXT,
    created_by  TEXT,
    last_login  TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires    REAL NOT NULL,
    created_at TEXT,
    ip         TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    ts        TEXT,
    level     TEXT,
    artist_id INTEGER,
    album_id  INTEGER,
    message   TEXT
);
"""

# Every setting the app understands, with its default. Env vars named
# CRATE_<KEY> override the default; values saved from the UI override both.
DEFAULTS = {
    "slskd_url": "http://localhost:5030",
    "slskd_api_key": "",
    # Where slskd's finished downloads appear *as seen by Crate*.
    "slskd_downloads_dir": "./downloads",
    "library_dir": "./music",
    "release_types": "album,ep",
    "exclude_secondary": "compilation,live,remix,dj-mix,mixtape/street,demo,interview,audiobook,spokenword,soundtrack,audio drama,field recording",
    "preferred_formats": "flac,mp3",
    "min_bitrate": "256",
    "search_timeout": "45",
    "max_active_searches": "2",
    "max_active_downloads": "3",
    "stall_minutes": "30",
    "retry_hours": "24",
    "import_mode": "move",
    # Duplicate cleanup: "trash" (move to <library>/.crate-trash) or "delete".
    "dupes_action": "trash",
    # Updates: check GitHub Releases for new versions.
    "update_checks": "true",
    "update_repo": "XxSmack003xX/crate",
    # Internal bookkeeping (not shown in the settings form).
    "update_last_result": "",
    "update_notified": "",
    "folder_template": "{artist}/{album} ({year})",
    "sync_hours": "24",
    "auto_download": "true",
    # Discover page: similar artists from ListenBrainz (+ Last.fm if you add a key).
    "recommendations": "true",
    "lastfm_api_key": "",
    "mb_contact": "crate-self-hosted",
    "mb_url": "https://musicbrainz.org/ws/2",
}

SECRET_KEYS = {"slskd_api_key", "lastfm_api_key"}
_WRITE_RE = re.compile(r"^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", re.I)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class DB:
    def __init__(self, path):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self.versions = {}
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(artists)")}
            if "image" not in cols:  # added in 1.1
                self.conn.execute("ALTER TABLE artists ADD COLUMN image TEXT")
            if "local_name" not in cols:  # 1.1: artist's folder name in your library, if different
                self.conn.execute("ALTER TABLE artists ADD COLUMN local_name TEXT")
            if "auto_download" not in cols:  # 1.1: per-artist override (NULL = use the global setting)
                self.conn.execute("ALTER TABLE artists ADD COLUMN auto_download INTEGER")
            if "added_by" not in cols:  # 1.3: who followed this artist
                self.conn.execute("ALTER TABLE artists ADD COLUMN added_by TEXT")
            # 1.4: per-artist rules. NULL = use the global setting.
            for col, typ in (("followed", "INTEGER DEFAULT 1"),       # 0 = only one-off album requests
                             ("monitor", "TEXT DEFAULT 'all'"),         # 'all' | 'new' (released after monitor_since)
                             ("monitor_since", "TEXT"),
                             ("release_types", "TEXT"),                 # e.g. "album,ep,single"
                             ("include_secondary", "INTEGER")):         # 1 = also live, compilations, remixes…
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE artists ADD COLUMN {col} {typ}")
            acols = {r[1] for r in self.conn.execute("PRAGMA table_info(albums)")}
            if "requested_by" not in acols:  # 1.3: who asked for this download (daily limits)
                self.conn.execute("ALTER TABLE albums ADD COLUMN requested_by TEXT")
                self.conn.execute("ALTER TABLE albums ADD COLUMN requested_at TEXT")
            if "search_notes" not in acols:  # 1.4.3: what each search found, for "not found" details
                self.conn.execute("ALTER TABLE albums ADD COLUMN search_notes TEXT")
            if "skip_reason" not in acols:  # 1.4: why it's skipped: type | before_follow | user
                self.conn.execute("ALTER TABLE albums ADD COLUMN skip_reason TEXT")
                self.conn.execute("UPDATE albums SET skip_reason = 'type' WHERE status = 'skipped' "
                                  "AND status_detail LIKE '%not a release type you follow%'")
                self.conn.execute("UPDATE albums SET skip_reason = 'user' WHERE status = 'skipped' "
                                  "AND skip_reason IS NULL")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_albums_artist_status ON albums(artist_id, status)")
            # 1.4.1: 1.4.0 created request-only artists with monitored = 0, which stopped their
            # albums from ever being searched. Nothing else sets it to 0.
            self.conn.execute("UPDATE artists SET monitored = 1 WHERE monitored = 0")
            ecols = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
            if "username" not in ecols:  # 1.3: who did it
                self.conn.execute("ALTER TABLE events ADD COLUMN username TEXT")

    # ---- generic helpers -------------------------------------------------
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            m = _WRITE_RE.match(sql)
            if m:
                t = m.group(1).lower()
                self.versions[t] = self.versions.get(t, 0) + 1
            return cur.lastrowid

    def version(self, *tables):
        """Changes whenever any of these tables is written (for HTTP ETags)."""
        return "-".join(str(self.versions.get(t, 0)) for t in tables)

    def update_album(self, album_id, **fields):
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in fields.values()]
        self.x(f"UPDATE albums SET {cols} WHERE id = ?", (*vals, album_id))

    def event(self, message, level="info", artist_id=None, album_id=None, username=None):
        self.x(
            "INSERT INTO events (ts, level, artist_id, album_id, message, username) VALUES (?,?,?,?,?,?)",
            (now_iso(), level, artist_id, album_id, message, username),
        )
        # keep the log bounded
        self.x("DELETE FROM events WHERE id < (SELECT MAX(id) - 5000 FROM events)")
        print(f"[{level}] {message}", flush=True)

    # ---- settings --------------------------------------------------------
    def settings(self):
        out = {}
        for k, v in DEFAULTS.items():
            out[k] = os.environ.get("CRATE_" + k.upper(), v)
        for row in self.q("SELECT key, value FROM settings"):
            if row["key"] in DEFAULTS:
                out[row["key"]] = row["value"]
        return out

    def save_settings(self, values):
        for k, v in values.items():
            if k not in DEFAULTS:
                continue
            self.x(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, str(v)),
            )


class Settings:
    """Typed, read-on-demand view over DB settings."""

    def __init__(self, db):
        self.db = db

    def raw(self):
        return self.db.settings()

    def __getattr__(self, name):
        return self.db.settings()[name]

    def int(self, name):
        try:
            return int(float(self.db.settings()[name]))
        except (TypeError, ValueError):
            return int(DEFAULTS[name])

    def bool(self, name):
        return str(self.db.settings()[name]).strip().lower() in ("1", "true", "yes", "on")

    def list(self, name):
        return [p.strip().lower() for p in str(self.db.settings()[name]).split(",") if p.strip()]
