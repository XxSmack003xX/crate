"""SQLite storage + settings for Crate.

One shared connection guarded by a re-entrant lock; good enough for a
single-user self-hosted app with a handful of threads.
"""
import json
import os
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
CREATE TABLE IF NOT EXISTS artist_info (
    mbid       TEXT PRIMARY KEY,
    data       TEXT,
    fetched_at TEXT
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
    "folder_template": "{artist}/{album} ({year})",
    "sync_hours": "24",
    "auto_download": "true",
    "mb_contact": "crate-self-hosted",
    "mb_url": "https://musicbrainz.org/ws/2",
}

SECRET_KEYS = {"slskd_api_key"}


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
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(artists)")}
            if "image" not in cols:  # added in 1.1
                self.conn.execute("ALTER TABLE artists ADD COLUMN image TEXT")

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
            return cur.lastrowid

    def update_album(self, album_id, **fields):
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in fields.values()]
        self.x(f"UPDATE albums SET {cols} WHERE id = ?", (*vals, album_id))

    def event(self, message, level="info", artist_id=None, album_id=None):
        self.x(
            "INSERT INTO events (ts, level, artist_id, album_id, message) VALUES (?,?,?,?,?)",
            (now_iso(), level, artist_id, album_id, message),
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
