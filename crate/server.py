"""HTTP API + static UI (stdlib http.server)."""
import json
import os
import re
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import ACTIVE, App
from .db import DEFAULTS, SECRET_KEYS
from .dupes import DupeError
from .slskd import Slskd, SlskdError

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
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
@route("GET", r"/api/status")
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


@route("GET", r"/api/mb/artists")
def mb_search(app, h, m, body):
    q = (h.query.get("q") or [""])[0].strip()
    if not q:
        return []
    results = app.mb.search_artists(q)
    have = {r["mbid"] for r in app.db.q("SELECT mbid FROM artists")}
    for r in results:
        r["added"] = r["mbid"] in have
    return results


@route("GET", r"/api/mb/artists/([0-9a-fA-F-]{36}|[\w-]+)/info")
def mb_artist_info(app, h, m, body):
    return app.artist_info(m.group(1))


@route("GET", r"/api/artists")
def list_artists(app, h, m, body):
    return _artist_rows(app)


@route("POST", r"/api/artists")
def add_artist(app, h, m, body):
    mbid = (body or {}).get("mbid")
    if not mbid:
        raise HTTPError(400, "mbid required")
    artist, created = app.add_artist(mbid)
    return {"artist": artist, "created": created}


@route("GET", r"/api/artists/(\d+)")
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


@route("POST", r"/api/artists/(\d+)/sync")
def sync_artist(app, h, m, body):
    threading.Thread(target=app.sync_artist, args=(int(m.group(1)),), daemon=True).start()
    return {"ok": True}


@route("POST", r"/api/artists/(\d+)/download-all")
def download_all(app, h, m, body):
    ids = app.db.q("SELECT id FROM albums WHERE artist_id = ? AND status IN ('missing','not_found','failed')",
                   (int(m.group(1)),))
    for r in ids:
        app.album_action(r["id"], "download")
    return {"ok": True, "count": len(ids)}


@route("POST", r"/api/albums/(\d+)/(download|retry|want|skip|next_source|recheck)")
def album_action(app, h, m, body):
    try:
        return _album_out(app.album_action(int(m.group(1)), m.group(2)))
    except KeyError:
        raise HTTPError(404, "album not found")


@route("GET", r"/api/queue")
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


@route("GET", r"/api/activity")
def activity(app, h, m, body):
    limit = int((h.query.get("limit") or ["100"])[0])
    return app.db.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 1000),))


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


@route("GET", r"/api/missing")
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


@route("POST", r"/api/missing/download")
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
    for i in ids:
        app.album_action(i, "download")
    if ids:
        app.db.event(f"Queued {len(ids)} missing album(s) to download")
    return {"queued": len(ids)}


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


# ----------------------------------------------------------------- handler
def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Crate/1.1"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
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
            for meth, rx, fn in ROUTES:
                if meth != method:
                    continue
                m = rx.match(path)
                if m:
                    try:
                        return self._send(200, fn(app, self, m, body))
                    except HTTPError as e:
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
