"""Accounts, sessions and permissions (modelled on MEDIARR).

Roles
  admin  everything, including Settings, Users, Updates, Duplicates and Import.
  user   browse, plus what their permissions allow:
           perms.artists   follow new artists                 (default on)
           perms.downloads start album downloads              (default on)
           daily_limit     albums they can queue per day      (default 10, 0 = no limit)
         Like MEDIARR, a permission is allowed unless it is set to false.

Passwords are hashed with scrypt. Sessions are random tokens in an HttpOnly cookie;
only a SHA-256 of each token is stored, so they survive restarts and updates but the
database alone can't be used to sign in. Failed logins lock out per IP and per
username: 5 failures, then 1, 5, 15, 30 and 60 minutes.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from .db import now_iso

PERMS = ("artists", "downloads")
PERM_LABEL = {"artists": "follow new artists", "downloads": "download albums"}
COOKIE = "crate_session"
SESSION_SECONDS = float(os.environ.get("CRATE_SESSION_HOURS", "24")) * 3600
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,40}$")

LOGIN_MAX = 5
LOGIN_WINDOW = 15 * 60
LOCK_STEPS = [60, 300, 900, 1800, 3600]


class AuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.scrypt(str(password).encode("utf-8"), salt=salt.encode(), n=16384, r=8, p=1, dklen=64,
                       maxmem=64 * 1024 * 1024)
    return salt, h.hex()


_DUMMY_SALT = secrets.token_hex(16)


def verify_password(password, salt, stored):
    if not salt or not stored:
        hash_password(password, _DUMMY_SALT)  # same timing for unknown users
        return False
    _, h = hash_password(password, salt)
    return hmac.compare_digest(h, str(stored))


def _sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.fails = {}  # key -> [count, first, until]
        self._touched = {}
        self.db.x("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        self._bootstrap_from_env()

    # ------------------------------------------------------------------ users
    def count(self):
        return self.db.one("SELECT COUNT(*) n FROM users")["n"]

    def get(self, username):
        return self.db.one("SELECT * FROM users WHERE username = ?", (str(username or "").strip(),))

    def get_id(self, uid):
        return self.db.one("SELECT * FROM users WHERE id = ?", (uid,))

    def perms(self, u):
        if not u:
            return {k: False for k in PERMS}
        stored = {}
        try:
            stored = json.loads(u.get("perms") or "{}")
        except ValueError:
            pass
        return {k: (u["role"] == "admin" or stored.get(k) is not False) for k in PERMS}

    def can(self, u, perm):
        return bool(u) and (u["role"] == "admin" or self.perms(u).get(perm, False))

    def public(self, u):
        if not u:
            return None
        return {"id": u["id"], "username": u["username"], "role": u["role"], "perms": self.perms(u),
                "daily_limit": 0 if u["role"] == "admin" else (u.get("daily_limit") or 0),
                "created_at": u.get("created_at"), "created_by": u.get("created_by"),
                "last_login": u.get("last_login"), "requested_today": self.requested_today(u)}

    def list(self):
        return [self.public(u) for u in self.db.q("SELECT * FROM users ORDER BY role != 'admin', username COLLATE NOCASE")]

    @staticmethod
    def _check_new(username, password):
        username = str(username or "").strip()
        if not USERNAME_RE.match(username):
            raise AuthError(400, "Username must be 2–40 characters: letters, numbers, dots, dashes or underscores.")
        if len(str(password or "")) < 6:
            raise AuthError(400, "Password must be at least 6 characters.")
        if len(str(password)) > 256:
            raise AuthError(400, "Password is too long.")
        return username

    def create(self, username, password, role="user", perms=None, daily_limit=10, created_by=None):
        username = self._check_new(username, password)
        if self.get(username):
            raise AuthError(409, "That username already exists.")
        salt, h = hash_password(password)
        role = "admin" if role == "admin" else "user"
        clean = {k: bool(v) for k, v in (perms or {}).items() if k in PERMS}
        try:
            limit = max(0, int(daily_limit))
        except (TypeError, ValueError):
            limit = 10
        self.db.x("INSERT INTO users (username, role, salt, hash, perms, daily_limit, created_at, created_by) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (username, role, salt, h, json.dumps(clean), limit, now_iso(), created_by))
        return self.get(username)

    def update(self, uid, actor, role=None, perms=None, daily_limit=None, password=None):
        u = self.get_id(uid)
        if not u:
            raise AuthError(404, "User not found.")
        if role in ("admin", "user") and role != u["role"]:
            if u["role"] == "admin" and role == "user" and self._admins() <= 1:
                raise AuthError(400, "Can't demote the last admin.")
            self.db.x("UPDATE users SET role = ? WHERE id = ?", (role, uid))
        if perms is not None:
            cur = {}
            try:
                cur = json.loads(u.get("perms") or "{}")
            except ValueError:
                pass
            cur.update({k: bool(v) for k, v in perms.items() if k in PERMS})
            self.db.x("UPDATE users SET perms = ? WHERE id = ?", (json.dumps(cur), uid))
        if daily_limit is not None:
            try:
                self.db.x("UPDATE users SET daily_limit = ? WHERE id = ?", (max(0, int(daily_limit)), uid))
            except (TypeError, ValueError):
                raise AuthError(400, "Daily limit must be a number (0 = no limit).")
        if password:
            self.set_password(uid, password)
            if actor and actor["id"] != uid:
                self.db.x("DELETE FROM sessions WHERE user_id = ?", (uid,))  # sign them out everywhere
        return self.get_id(uid)

    def delete(self, uid, actor):
        u = self.get_id(uid)
        if not u:
            raise AuthError(404, "User not found.")
        if actor and actor["id"] == uid:
            raise AuthError(400, "You can't remove yourself.")
        if u["role"] == "admin" and self._admins() <= 1:
            raise AuthError(400, "Can't remove the last admin.")
        self.db.x("DELETE FROM sessions WHERE user_id = ?", (uid,))
        self.db.x("DELETE FROM users WHERE id = ?", (uid,))

    def set_password(self, uid, password):
        if len(str(password or "")) < 6:
            raise AuthError(400, "Password must be at least 6 characters.")
        salt, h = hash_password(password)
        self.db.x("UPDATE users SET salt = ?, hash = ? WHERE id = ?", (salt, h, uid))

    def _admins(self):
        return self.db.one("SELECT COUNT(*) n FROM users WHERE role = 'admin'")["n"]

    def _bootstrap_from_env(self):
        """CRATE_ADMIN_USER + CRATE_ADMIN_PASSWORD pre-create the first admin (no setup screen)."""
        user, pw = os.environ.get("CRATE_ADMIN_USER"), os.environ.get("CRATE_ADMIN_PASSWORD")
        if user and pw and self.count() == 0:
            try:
                self.create(user, pw, role="admin", created_by="environment")
                self.db.event(f"Created admin account '{user}' from CRATE_ADMIN_USER")
            except AuthError as e:
                print(f"[auth] CRATE_ADMIN_USER ignored: {e}", flush=True)

    # ------------------------------------------------------------------ daily limit
    def requested_today(self, u):
        if not u:
            return 0
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return self.db.one("SELECT COUNT(*) n FROM albums WHERE requested_by = ? AND requested_at >= ?",
                           (u["username"], start))["n"]

    def remaining_today(self, u):
        """How many more albums this user may queue today (None = unlimited)."""
        if u["role"] == "admin" or not (u.get("daily_limit") or 0):
            return None
        return max(0, u["daily_limit"] - self.requested_today(u))

    # ------------------------------------------------------------------ lockout
    def _keys(self, ip, username):
        return ["ip:" + str(ip), "user:" + str(username or "").lower()]

    def blocked(self, ip, username):
        now = time.time()
        with self.lock:
            for k in self._keys(ip, username):
                r = self.fails.get(k)
                if r and r[2] > now:
                    return int(r[2] - now) + 1
        return 0

    def failed(self, ip, username):
        now, lock = time.time(), 0
        with self.lock:
            for k in self._keys(ip, username):
                r = self.fails.get(k) or [0, now, 0]
                if now - r[1] > LOGIN_WINDOW and r[2] < now:
                    r = [0, now, 0]
                r[0] += 1
                if r[0] >= LOGIN_MAX:
                    step = LOCK_STEPS[min(len(LOCK_STEPS) - 1, r[0] - LOGIN_MAX)]
                    r[2] = now + step
                    lock = max(lock, step)
                self.fails[k] = r
            if len(self.fails) > 5000:
                for k in [k for k, r in self.fails.items() if r[2] < now and now - r[1] > LOGIN_WINDOW]:
                    self.fails.pop(k, None)
        return lock

    def succeeded(self, ip, username):
        with self.lock:
            for k in self._keys(ip, username):
                self.fails.pop(k, None)

    # ------------------------------------------------------------------ sessions
    def login(self, username, password, ip):
        wait = self.blocked(ip, username)
        if wait:
            raise AuthError(429, f"Too many failed attempts. Try again in {_human(wait)}.")
        u = self.get(username)
        if not verify_password(password, u and u["salt"], u and u["hash"]):
            locked = self.failed(ip, username)
            self.db.event(f"Failed sign-in for '{str(username)[:40]}' from {ip}"
                          + (f"; locked for {_human(locked)}" if locked else ""), "warn")
            raise AuthError(429 if locked else 401, f"Too many failed attempts. Locked for {_human(locked)}."
                            if locked else "Wrong username or password.")
        self.succeeded(ip, username)
        token = self.new_session(u["id"], ip)
        self.db.x("UPDATE users SET last_login = ? WHERE id = ?", (now_iso(), u["id"]))
        return u, token

    def new_session(self, uid, ip=None):
        token = secrets.token_hex(32)
        self.db.x("INSERT INTO sessions (token_hash, user_id, expires, created_at, ip) VALUES (?,?,?,?,?)",
                  (_sha(token), uid, time.time() + SESSION_SECONDS, now_iso(), ip))
        return token

    def from_token(self, token):
        if not token:
            return None
        h = _sha(token)
        s = self.db.one("SELECT * FROM sessions WHERE token_hash = ?", (h,))
        if not s:
            return None
        now = time.time()
        if s["expires"] < now:
            self.db.x("DELETE FROM sessions WHERE token_hash = ?", (h,))
            return None
        # Sliding expiry, saved at most once a minute (the UI polls every few seconds).
        if now - self._touched.get(h, 0) > 60:
            self._touched[h] = now
            self.db.x("UPDATE sessions SET expires = ? WHERE token_hash = ?", (now + SESSION_SECONDS, h))
        return self.get_id(s["user_id"])

    def logout(self, token):
        if token:
            self.db.x("DELETE FROM sessions WHERE token_hash = ?", (_sha(token),))


def _human(seconds):
    seconds = int(seconds)
    if seconds >= 120:
        return f"{-(-seconds // 60)} minutes"
    if seconds >= 60:
        return "1 minute"
    return f"{seconds} seconds"
