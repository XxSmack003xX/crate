"""HTTP API + static UI (stdlib http.server)."""
import json
import os
import re
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .auth import COOKIE, PERM_LABEL, SESSION_SECONDS, AuthError
from .core import ACTIVE, App
from .db import DEFAULTS, SECRET_KEYS, now_iso
from .dupes import DupeError
from . import __version__
from .slskd import Slskd, SlskdError
from .updater import UpdateError

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
ROUTES = []


ACCESS = ("public", "user", "artists", "downloads", "admin")


def route(method, pattern, access="admin"):
    """Register an API route. Routes are admin-only unless they say otherwise:
    public (no login), user (any signed-in account), artists / downloads (users
    with that permission), admin."""
    assert access in ACCESS

    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn, access))
        return fn
    return deco


class HTTPError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _artist_rows(app, where="", args=()):
    return app.db.q(f"""
        SELECT ar.*,
          (SELECT COUNT(*) FROM albums WHERE artist_id = ar.id AND status != 'skipped') AS total,
          (SELECT COUNT(*) FROM albums WHERE artist_id = ar.id AND status = 'have') AS have,
          (SELECT COUNT(*) FROM albums WHERE artist_id = ar.id AND status IN ('searching','queued','downloading','importing')) AS active,
          (SELECT COUNT(*) FROM albums WHERE artist_id = ar.id AND status IN ('wanted','not_found','failed','missing')) AS missing,
          (SELECT mbid FROM albums WHERE artist_id = ar.id AND primary_type = 'Album' AND status != 'skipped'
              ORDER BY (status = 'have') DESC, release_date DESC LIMIT 1) AS cover_mbid
        FROM artists ar {where} ORDER BY COALESCE(ar.sort_name, ar.name) COLLATE NOCASE""", args)


def _album_out(a):
    a = dict(a)
    for k in ("secondary_types", "tracks", "candidates", "tried_users", "dl_files"):
        if k in a and a[k]:
            try:
                a[k] = json.loads(a[k])
            except ValueError:
                pass
    if a.get("candidates"):
        a["candidates"] = [{k: v for k, v in c.items() if k != "files"} for c in a["candidates"]]
    a.pop("dl_files", None)
    a["progress"] = None
    if a.get("status") == "downloading" and a.get("dl_size"):
        a["progress"] = min(100, round((a.get("dl_bytes") or 0) * 100 / a["dl_size"], 1))
    return a


# ------------------------------------------------------------------ routes
@route("GET", r"/api/status", access="user")
def status(app, h, m, body):
    counts = {r["status"]: r["n"] for r in app.db.q("SELECT status, COUNT(*) n FROM albums GROUP BY status")}
    # "missing" only counts albums that are actually out (matches the Missing albums page)
    counts["missing"] = app.db.one("SELECT COUNT(*) n FROM albums al WHERE al.status = 'missing' AND " + RELEASED,
                                   (_today(),))["n"]
    return {
        "configured": app.configured(),
        "slskd": app.slskd_status,
        "worker_error": app.worker_error,
        "last_tick": app.last_tick,
        "counts": counts,
        "artists": app.db.one("SELECT COUNT(*) n FROM artists")["n"],
        "version": __version__,
        "update": app.updater.banner() if h.me and h.me["role"] == "admin" else None,
    }


@route("GET", r"/api/settings")
def get_settings(app, h, m, body):
    s = app.settings.raw()
    out = {k: s[k] for k in DEFAULTS}
    for k in SECRET_KEYS:
        out[k] = "••••••••" if s.get(k) else ""
    return out


@route("PUT", r"/api/settings")
def put_settings(app, h, m, body):
    vals = {k: v for k, v in (body or {}).items() if k in DEFAULTS}
    for k in SECRET_KEYS:
        if vals.get(k, "").startswith("•"):
            vals.pop(k)
    app.db.save_settings(vals)
    app.check_slskd()
    app.wake.set()
    return get_settings(app, h, m, body)


@route("POST", r"/api/settings/test")
def test_settings(app, h, m, body):
    body = body or {}
    url = body.get("slskd_url") or app.settings.slskd_url
    key = body.get("slskd_api_key") or ""
    if not key or key.startswith("•"):
        key = app.settings.slskd_api_key
    out = {}
    try:
        st = Slskd(url, key, timeout=10).server_state()
        out["slskd"] = {"ok": True, "state": st.get("state"), "username": st.get("username"),
                        "logged_in": st.get("isLoggedIn")}
    except SlskdError as e:
        out["slskd"] = {"ok": False, "error": str(e)}
    for k in ("slskd_downloads_dir", "library_dir"):
        p = os.path.abspath(os.path.expanduser(body.get(k) or app.settings.raw()[k]))
        out[k] = {"path": p, "exists": os.path.isdir(p), "writable": os.access(p, os.W_OK) if os.path.isdir(p) else False}
    return out


@route("GET", r"/api/mb/artists", access="user")
def mb_search(app, h, m, body):
    q = (h.query.get("q") or [""])[0].strip()
    if not q:
        return []
    results = app.mb.search_artists(q)
    have = {r["mbid"] for r in app.db.q("SELECT mbid FROM artists")}
    for r in results:
        r["added"] = r["mbid"] in have
    return results


@route("GET", r"/api/mb/artists/([0-9a-fA-F-]{36}|[\w-]+)/info", access="user")
def mb_artist_info(app, h, m, body):
    return app.artist_info(m.group(1))


@route("GET", r"/api/artists", access="user")
def list_artists(app, h, m, body):
    return _artist_rows(app)


@route("POST", r"/api/artists", access="artists")
def add_artist(app, h, m, body):
    mbid = (body or {}).get("mbid")
    if not mbid:
        raise HTTPError(400, "mbid required")
    me = h.me
    auto = None
    # Users with a daily limit (or no download permission) don't get whole discographies
    # queued automatically: missing albums wait for them to pick, counted against the limit.
    if me["role"] != "admin" and (not app.auth.can(me, "downloads") or (me.get("daily_limit") or 0) > 0):
        auto = False
    artist, created = app.add_artist(mbid, added_by=me["username"], auto_download=auto)
    return {"artist": artist, "created": created}


@route("GET", r"/api/artists/(\d+)", access="user")
def get_artist(app, h, m, body):
    rows = _artist_rows(app, "WHERE ar.id = ?", (int(m.group(1)),))
    if not rows:
        raise HTTPError(404, "artist not found")
    albums = app.db.q("SELECT * FROM albums WHERE artist_id = ? ORDER BY release_date DESC, title",
                      (int(m.group(1)),))
    return {"artist": rows[0], "albums": [_album_out(a) for a in albums]}


@route("DELETE", r"/api/artists/(\d+)")
def delete_artist(app, h, m, body):
    app.remove_artist(int(m.group(1)))
    return {"ok": True}


@route("POST", r"/api/artists/(\d+)/sync", access="artists")
def sync_artist(app, h, m, body):
    threading.Thread(target=app.sync_artist, args=(int(m.group(1)),), daemon=True).start()
    return {"ok": True}


@route("POST", r"/api/artists/(\d+)/download-all", access="downloads")
def download_all(app, h, m, body):
    ids = [r["id"] for r in app.db.q(
        "SELECT id FROM albums WHERE artist_id = ? AND status IN ('missing','not_found','failed') "
        "ORDER BY release_date DESC", (int(m.group(1)),))]
    res = _queue(app, h.me, ids)
    return {"ok": True, "count": res["queued"], **res}


@route("POST", r"/api/albums/(\d+)/(download|retry|want|skip|next_source|recheck)", access="downloads")
def album_action(app, h, m, body):
    action, me = m.group(2), h.me
    if action == "skip" and me["role"] != "admin":
        raise HTTPError(403, "Only admins can skip releases.")
    try:
        if action in ("download", "retry", "want"):
            a = app.album(int(m.group(1)))
            if not a:
                raise KeyError
            if a["status"] not in ("wanted", "searching", "queued", "downloading", "importing"):
                res = _queue(app, me, [a["id"]], action)
                if res["limited"]:
                    raise HTTPError(429, res["message"])
            return _album_out(app.album(int(m.group(1))))
        return _album_out(app.album_action(int(m.group(1)), action, username=me["username"]))
    except KeyError:
        raise HTTPError(404, "album not found")


@route("GET", r"/api/queue", access="user")
def queue(app, h, m, body):
    marks = ",".join("?" * len(ACTIVE))
    active = app.db.q(
        f"SELECT al.*, ar.name AS artist_name FROM albums al JOIN artists ar ON ar.id = al.artist_id "
        f"WHERE al.status IN ({marks}) ORDER BY CASE al.status WHEN 'importing' THEN 0 WHEN 'downloading' THEN 1 "
        f"WHEN 'queued' THEN 2 ELSE 3 END, al.updated_at", ACTIVE)
    waiting = app.db.q(
        "SELECT al.*, ar.name AS artist_name FROM albums al JOIN artists ar ON ar.id = al.artist_id "
        "WHERE al.status IN ('wanted','not_found','failed') ORDER BY al.status, al.updated_at DESC LIMIT 200")
    return {"active": [_album_out(a) for a in active], "waiting": [_album_out(a) for a in waiting]}


@route("GET", r"/api/activity", access="user")
def activity(app, h, m, body):
    limit = min(int((h.query.get("limit") or ["100"])[0]), 1000)
    if h.me["role"] == "admin":
        return app.db.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    # Non-admins see what they did themselves (like MEDIARR).
    return app.db.q("SELECT * FROM events WHERE username = ? ORDER BY id DESC LIMIT ?", (h.me["username"], limit))


@route("POST", r"/api/slskd/clear-searches")
def clear_searches(app, h, m, body):
    if not app.configured():
        raise HTTPError(400, "Connect slskd in Settings first")
    return app.clear_searches()


@route("GET", r"/api/import")
def import_state(app, h, m, body):
    return app.importer.snapshot()


@route("POST", r"/api/import/scan")
def import_scan(app, h, m, body):
    app.importer.start()
    return app.importer.snapshot()


@route("POST", r"/api/import/add")
def import_add(app, h, m, body):
    body = body or {}
    result = app.importer.add(body.get("items") or [], auto_download=bool(body.get("auto_download")))
    return {"result": result, **app.importer.snapshot()}


RELEASED = "(al.release_date IS NULL OR al.release_date = '' OR al.release_date <= ?)"


def _today():
    import datetime
    return datetime.date.today().isoformat()


@route("GET", r"/api/missing", access="user")
def missing(app, h, m, body):
    rows = app.db.q(
        "SELECT al.*, ar.name AS artist_name FROM albums al JOIN artists ar ON ar.id = al.artist_id "
        "WHERE al.status = 'missing' AND " + RELEASED +
        " ORDER BY COALESCE(ar.sort_name, ar.name) COLLATE NOCASE, al.release_date DESC", (_today(),))
    groups, order = {}, []
    for r in rows:
        if r["artist_id"] not in groups:
            groups[r["artist_id"]] = {"artist_id": r["artist_id"], "artist_name": r["artist_name"], "albums": []}
            order.append(r["artist_id"])
        groups[r["artist_id"]]["albums"].append(_album_out(r))
    return {"total": len(rows), "artists": [groups[i] for i in order]}


@route("POST", r"/api/missing/download", access="downloads")
def missing_download(app, h, m, body):
    """Queue missing albums: all of them, one artist's ({artist_id}), or a list ({album_ids})."""
    body = body or {}
    sql, args = "SELECT al.id FROM albums al WHERE al.status = 'missing' AND " + RELEASED, [_today()]
    if body.get("artist_id"):
        sql += " AND al.artist_id = ?"
        args.append(int(body["artist_id"]))
    ids = [r["id"] for r in app.db.q(sql, args)]
    if body.get("album_ids"):
        want = {int(i) for i in body["album_ids"]}
        ids = [i for i in ids if i in want]
    res = _queue(app, h.me, ids)
    if res["queued"]:
        app.db.event(f"{h.me['username']} queued {res['queued']} missing album(s) to download",
                     username=h.me["username"])
    return res


def _queue(app, me, ids, action="download"):
    """Queue albums for download on behalf of `me`, respecting their daily limit."""
    remaining = app.auth.remaining_today(me)
    queued = limited = 0
    for i in ids:
        if remaining is not None and queued >= remaining:
            limited += 1
            continue
        a = app.album_action(i, action, username=me["username"])
        if a and a["status"] == "wanted":
            queued += 1
    msg = ""
    if limited:
        msg = (f"Daily limit reached: you can queue {me['daily_limit']} album(s) a day. "
               f"{limited} album(s) weren't queued. Ask an admin to raise your limit.")
    return {"queued": queued, "limited": limited, "message": msg, "remaining": app.auth.remaining_today(me)}


# ------------------------------------------------------------------ accounts
def _client_ip(h):
    if os.environ.get("CRATE_TRUST_PROXY") == "1":
        fwd = (h.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if fwd:
            return fwd
    return h.client_address[0]


def _secure(h):
    if os.environ.get("CRATE_SECURE_COOKIES") == "1":
        return True
    return (h.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower() == "https"


def _set_session(h, token):
    flags = "; Secure" if _secure(h) else ""
    h.extra_headers.append(("Set-Cookie", f"{COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; "
                                          f"Max-Age={int(SESSION_SECONDS)}{flags}"))


@route("GET", r"/api/version", access="public")
def version(app, h, m, body):
    return {"app": "Crate", "version": __version__}


@route("GET", r"/api/setup-needed", access="public")
def setup_needed(app, h, m, body):
    return {"needed": app.auth.count() == 0, "version": __version__}


@route("POST", r"/api/setup", access="public")
def setup(app, h, m, body):
    if app.auth.count():
        raise HTTPError(403, "Setup is already complete. Sign in instead.")
    body = body or {}
    u = app.auth.create(body.get("username"), body.get("password"), role="admin", created_by="setup")
    _set_session(h, app.auth.new_session(u["id"], _client_ip(h)))
    app.db.x("UPDATE users SET last_login = ? WHERE id = ?", (now_iso(), u["id"]))
    app.db.event(f"Created admin account '{u['username']}' (first-run setup)", username=u["username"])
    return app.auth.public(u)


@route("POST", r"/api/login", access="public")
def login(app, h, m, body):
    body = body or {}
    u, token = app.auth.login(body.get("username"), body.get("password"), _client_ip(h))
    _set_session(h, token)
    return app.auth.public(u)


@route("POST", r"/api/logout", access="public")
def logout(app, h, m, body):
    app.auth.logout(h.token)
    h.extra_headers.append(("Set-Cookie", f"{COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"))
    return {"ok": True}


@route("GET", r"/api/me", access="user")
def me(app, h, m, body):
    return app.auth.public(h.me)


@route("POST", r"/api/me/password", access="user")
def change_password(app, h, m, body):
    from .auth import verify_password
    body = body or {}
    if not verify_password(body.get("current") or "", h.me["salt"], h.me["hash"]):
        raise HTTPError(403, "Current password is incorrect.")
    app.auth.set_password(h.me["id"], body.get("new") or "")
    # Sign out other sessions, keep this one.
    app.db.x("DELETE FROM sessions WHERE user_id = ?", (h.me["id"],))
    _set_session(h, app.auth.new_session(h.me["id"], _client_ip(h)))
    return {"ok": True}


@route("GET", r"/api/users")
def users_list(app, h, m, body):
    return {"users": app.auth.list(), "perm_labels": PERM_LABEL}


@route("POST", r"/api/users")
def users_create(app, h, m, body):
    body = body or {}
    u = app.auth.create(body.get("username"), body.get("password"), role=body.get("role"),
                        perms=body.get("perms"), daily_limit=body.get("daily_limit", 10),
                        created_by=h.me["username"])
    app.db.event(f"{h.me['username']} created {u['role']} account '{u['username']}'", username=h.me["username"])
    return app.auth.public(u)


@route("PATCH", r"/api/users/(\d+)")
def users_update(app, h, m, body):
    body = body or {}
    u = app.auth.update(int(m.group(1)), h.me, role=body.get("role"), perms=body.get("perms"),
                        daily_limit=body.get("daily_limit"), password=body.get("password") or None)
    return app.auth.public(u)


@route("DELETE", r"/api/users/(\d+)")
def users_delete(app, h, m, body):
    u = app.auth.get_id(int(m.group(1)))
    app.auth.delete(int(m.group(1)), h.me)
    app.db.event(f"{h.me['username']} removed account '{u['username'] if u else '?'}'", username=h.me["username"])
    return {"ok": True}


# ------------------------------------------------------------------ updates
@route("GET", r"/api/update")
def update_status(app, h, m, body):
    return app.updater.status(force=(h.query.get("fresh") or ["0"])[0] == "1")


@route("POST", r"/api/update/config")
def update_config(app, h, m, body):
    body = body or {}
    vals = {}
    if "enabled" in body:
        vals["update_checks"] = "true" if body["enabled"] else "false"
    if "repo" in body and not app.updater.repo_locked():
        vals["update_repo"] = str(body["repo"] or "").strip()
    app.db.save_settings(vals)
    return app.updater.status(force=True)


@route("POST", r"/api/update/apply")
def update_apply(app, h, m, body):
    return app.updater.apply(h.me, force=bool((body or {}).get("force")))


@route("GET", r"/api/dupes")
def dupes(app, h, m, body):
    return app.dupes.snapshot()


@route("POST", r"/api/dupes/scan")
def dupes_scan(app, h, m, body):
    app.dupes.start_scan()
    return app.dupes.snapshot()


@route("POST", r"/api/dupes/resolve")
def dupes_resolve(app, h, m, body):
    try:
        result = app.dupes.resolve(body or {})
    except DupeError as e:
        raise HTTPError(400, str(e))
    return {"result": result, **app.dupes.snapshot()}


@route("POST", r"/api/dupes/trash/empty")
def dupes_empty(app, h, m, body):
    return {"emptied": app.dupes.empty_trash(), **app.dupes.snapshot()}


@route("POST", r"/api/library/rescan")
def rescan(app, h, m, body):
    folders, matched = app.rescan_library()
    app.db.event(f"Library rescan: {folders} album folders found, {matched} wanted album(s) were already there")
    return {"folders": folders, "matched": matched}


def _cookie(header, name):
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return urllib.parse.unquote(v)
    return None


# ----------------------------------------------------------------- handler
def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Crate/1.3"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in getattr(self, "extra_headers", []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            parsed = urllib.parse.urlparse(self.path)
            self.query = urllib.parse.parse_qs(parsed.query)
            path = parsed.path
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            body = None
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
            self.extra_headers = []
            self.token = None
            self.me = None
            for meth, rx, fn, access in ROUTES:
                if meth != method:
                    continue
                m = rx.match(path)
                if m:
                    # Cross-site requests can't add custom headers, so this blocks CSRF.
                    if method != "GET" and self.headers.get("X-Crate") != "1":
                        return self._send(403, {"error": "Missing X-Crate header"})
                    self.token = _cookie(self.headers.get("Cookie"), COOKIE)
                    self.me = app.auth.from_token(self.token)
                    if access != "public":
                        if not self.me:
                            return self._send(401, {"error": "Sign in to continue.", "login": True})
                        if access == "admin" and self.me["role"] != "admin":
                            return self._send(403, {"error": "Only admins can do that."})
                        if access in PERM_LABEL and not app.auth.can(self.me, access):
                            return self._send(403, {"error": f"Your account isn't allowed to {PERM_LABEL[access]}. "
                                                             "Ask an admin if you need this."})
                    try:
                        return self._send(200, fn(app, self, m, body))
                    except HTTPError as e:
                        return self._send(e.code, {"error": str(e)})
                    except AuthError as e:
                        return self._send(e.code, {"error": str(e)})
                    except UpdateError as e:
                        return self._send(e.code, {"error": str(e)})
                    except SlskdError as e:
                        return self._send(502, {"error": str(e)})
                    except Exception as e:
                        traceback.print_exc()
                        return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return self._send(404, {"error": "not found"})

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            full = os.path.abspath(os.path.join(WEB_DIR, path.lstrip("/")))
            if not full.startswith(os.path.abspath(WEB_DIR)) or not os.path.isfile(full):
                full = os.path.join(WEB_DIR, "index.html")
            ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript", "css": "text/css",
                     "svg": "image/svg+xml", "png": "image/png", "ico": "image/x-icon"}.get(
                full.rsplit(".", 1)[-1], "application/octet-stream")
            with open(full, "rb") as fh:
                self._send(200, fh.read(), ctype)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def do_PATCH(self):
            self._dispatch("PATCH")

    return Handler


def serve(db_path, host, port):
    app = App(db_path)
    app.check_slskd()
    threading.Thread(target=app.run_worker, daemon=True, name="worker").start()
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"Crate is running at http://{'localhost' if host in ('0.0.0.0', '') else host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        app.wake.set()
        httpd.server_close()
    return app
