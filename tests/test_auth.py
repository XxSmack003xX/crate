"""Login system tests: first-run setup, sign-in, lockout, sessions, roles,
per-user permissions, daily limits, CSRF header and the password-reset command.

Run:  python3 tests/test_auth.py
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.update({"CRATE_TICK_SECONDS": "3600", "CRATE_MB_INTERVAL": "0", "CRATE_UPDATE_CHECKS": "false"})

import mock_services  # noqa: E402

tmp = tempfile.mkdtemp(prefix="crate-auth-")
srv, state = mock_services.start(os.path.join(tmp, "dl"))
mu = f"http://127.0.0.1:{srv.server_address[1]}"
os.environ.update({"CRATE_MB_URL": mu + "/ws/2", "CRATE_WIKIDATA_URL": mu + "/w/api.php",
                   "CRATE_LIBRARY_DIR": os.path.join(tmp, "music")})
os.makedirs(os.path.join(tmp, "music"))

from http.server import ThreadingHTTPServer  # noqa: E402
from crate import auth as auth_mod  # noqa: E402
from crate.core import App  # noqa: E402
from crate.server import make_handler  # noqa: E402

DB = os.path.join(tmp, "crate.db")
app = App(DB)
httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"
fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


class Browser:
    def __init__(self, csrf=True):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = csrf

    def api(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.csrf:
            headers["X-Crate"] = "1"
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with self.op.open(req, timeout=30) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw or b"null")
                except ValueError:
                    return r.status, raw
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None

    def cookie(self):
        return next((c.value for c in self.jar if c.name == "crate_session"), None)


print("== first-run setup")
anon = Browser()
st, d = anon.api("GET", "/api/setup-needed")
check(st == 200 and d["needed"], "setup needed on a fresh install")
st, d = anon.api("GET", "/api/artists")
check(st == 401 and d.get("login"), "API requires sign-in")
st, _ = anon.api("GET", "/")
check(st == 200, "the page itself loads (shows the sign-in screen)")
st, d = anon.api("GET", "/api/version")
check(st == 200 and d["version"], "version endpoint is public (Docker health check)")
st, d = anon.api("POST", "/api/setup", {"username": "x", "password": "short"})
check(st == 400, "setup validates username/password")
admin = Browser()
st, d = admin.api("POST", "/api/setup", {"username": "boss", "password": "boss-password-1"})
check(st == 200 and d["role"] == "admin" and admin.cookie(), "first account is an admin and is signed in")
st, _ = Browser().api("POST", "/api/setup", {"username": "intruder", "password": "intruder-pw-1"})
check(st == 403, "setup can't run twice")
row = app.db.one("SELECT salt, hash FROM users WHERE username = 'boss'")
check(row and "boss-password-1" not in json.dumps(row) and len(row["hash"]) == 128, "password stored as scrypt hash")
tok = admin.cookie()
check(not app.db.one("SELECT 1 FROM sessions WHERE token_hash = ?", (tok,)), "only a hash of the session token is stored")

print("== CSRF")
nocsrf = Browser(csrf=False)
nocsrf.jar = admin.jar
nocsrf.op = admin.op
st, d = nocsrf.api("POST", "/api/artists", {"mbid": "a-1"})
check(st == 403 and "X-Crate" in d["error"], "changes without the X-Crate header are refused")

print("== users and permissions")
st, d = admin.api("POST", "/api/users", {"username": "kid", "password": "kid-password-1", "role": "user",
                                         "perms": {"artists": True, "downloads": True}, "daily_limit": 2})
check(st == 200 and d["role"] == "user" and d["daily_limit"] == 2, "admin creates a user with a daily limit")
kid_id = d["id"]
st, d = admin.api("POST", "/api/users", {"username": "viewer", "password": "viewer-pass-1",
                                         "perms": {"artists": False, "downloads": False}})
check(st == 200 and d["perms"] == {"artists": False, "downloads": False}, "user with no add/download permission")
viewer_id = d["id"]
st, _ = admin.api("POST", "/api/users", {"username": "KID", "password": "another-pass1"})
check(st == 409, "usernames are unique (case-insensitive)")

kid = Browser()
st, d = kid.api("POST", "/api/login", {"username": "kid", "password": "kid-password-1"})
check(st == 200 and d["role"] == "user", "user signs in")
for path in ("/api/settings", "/api/users", "/api/update", "/api/dupes", "/api/import"):
    st, d = kid.api("GET", path)
    check(st == 403, f"user blocked from admin-only {path}")
st, _ = kid.api("POST", "/api/slskd/clear-searches")
check(st == 403, "user can't clear slskd searches")
st, d = kid.api("GET", "/api/artists")
check(st == 200, "user can browse artists")

print("== user adds an artist")
st, d = kid.api("POST", "/api/artists", {"mbid": "a-1"})
check(st == 200 and d["created"], "user with permission follows an artist")
aid = d["artist"]["id"]
a = app.db.one("SELECT added_by, auto_download FROM artists WHERE id = ?", (aid,))
check(a["added_by"] == "kid" and a["auto_download"] == 0, "artists added by limited users don't auto-download")
app.sync_artist(aid)
albums = {x["title"]: x for x in kid.api("GET", f"/api/artists/{aid}")[1]["albums"]}
check(not any(x["status"] == "wanted" for x in albums.values()), "nothing queued without asking")
st, _ = kid.api("DELETE", f"/api/artists/{aid}")
check(st == 403, "user can't remove artists")
st, _ = kid.api("POST", f"/api/albums/{albums['Rare Gem']['id']}/skip")
check(st == 403, "user can't skip releases")

print("== daily limit")
ids = [albums[t]["id"] for t in ("Rare Gem", "First Light", "Small Things")]
st, d = kid.api("POST", f"/api/albums/{ids[0]}/download")
check(st == 200 and d["status"] == "wanted" and d["requested_by"] == "kid", "download #1 queued, recorded as kid's")
st, d = kid.api("POST", "/api/missing/download", {"album_ids": ids[1:]})
check(st == 200 and d["queued"] == 1 and d["limited"] == 1 and "Daily limit" in d["message"],
      f"limit of 2/day: second queued, third refused ({d.get('queued')}, {d.get('limited')})")
st, d = kid.api("POST", f"/api/albums/{albums['Flaky Connection']['id']}/download")
check(st == 429 and "Daily limit" in d["error"], "single download past the limit → 429 with explanation")
st, d = admin.api("POST", f"/api/albums/{albums['Flaky Connection']['id']}/download")
check(st == 200 and d["status"] == "wanted", "admins have no daily limit")
st, d = kid.api("GET", "/api/me")
check(d["requested_today"] == 2, "user sees how many they've requested today")

print("== no-permission user")
viewer = Browser()
viewer.api("POST", "/api/login", {"username": "viewer", "password": "viewer-pass-1"})
st, d = viewer.api("POST", "/api/artists", {"mbid": "a-3"})
check(st == 403 and "follow new artists" in d["error"], "can't add artists without permission")
st, d = viewer.api("POST", f"/api/albums/{albums['Small Things']['id']}/download")
check(st == 403 and "download albums" in d["error"], "can't download without permission")
st, d = viewer.api("GET", "/api/queue")
check(st == 200, "can still see the queue")

print("== activity")
st, ev = kid.api("GET", "/api/activity")
check(st == 200 and ev and all(e["username"] == "kid" for e in ev), "users only see their own activity")
st, ev = admin.api("GET", "/api/activity")
check(any(e["username"] != "kid" for e in ev), "admins see everything")

print("== admin rules")
boss_id = app.auth.get("boss")["id"]
st, d = admin.api("PATCH", f"/api/users/{boss_id}", {"role": "user"})
check(st == 400 and "last admin" in d["error"], "can't demote the last admin")
st, d = admin.api("DELETE", f"/api/users/{boss_id}")
check(st == 400, "can't remove yourself / last admin")
st, d = admin.api("PATCH", f"/api/users/{viewer_id}", {"perms": {"downloads": True}, "daily_limit": 0})
check(d["perms"]["downloads"] and not d["perms"]["artists"] and d["daily_limit"] == 0, "admin changes permissions")
st, d = viewer.api("POST", f"/api/albums/{albums['Small Things']['id']}/download")
check(st == 200, "permission change applies immediately")
st, d = admin.api("PATCH", f"/api/users/{kid_id}", {"password": "kid-new-pass-2"})
st, _ = kid.api("GET", "/api/me")
check(st == 401, "admin password reset signs the user out")
st, _ = kid.api("POST", "/api/login", {"username": "kid", "password": "kid-new-pass-2"})
check(st == 200, "user signs in with the new password")
st, d = admin.api("PATCH", f"/api/users/{viewer_id}", {"role": "admin"})
st, d = admin.api("DELETE", f"/api/users/{viewer_id}")
check(st == 200, "a second admin can be removed")

print("== own password + logout")
st, d = kid.api("POST", "/api/me/password", {"current": "wrong", "new": "whatever123"})
check(st == 403, "changing password needs the current one")
st, d = kid.api("POST", "/api/me/password", {"current": "kid-new-pass-2", "new": "kid-third-pass"})
check(st == 200 and kid.api("GET", "/api/me")[0] == 200, "user changes own password and stays signed in")
st, _ = kid.api("POST", "/api/logout")
check(kid.api("GET", "/api/me")[0] == 401, "sign out ends the session")

print("== lockout")
auth_mod.LOCK_STEPS[0] = 2  # shorten the first lockout for the test
b = Browser()
codes = [b.api("POST", "/api/login", {"username": "boss", "password": f"nope{i}"})[0] for i in range(5)]
check(codes[:4] == [401] * 4 and codes[4] == 429, f"5th wrong password locks the account ({codes})")
st, d = b.api("POST", "/api/login", {"username": "boss", "password": "boss-password-1"})
check(st == 429 and "Try again" in d["error"], "even the right password is refused while locked")
st, _ = Browser().api("POST", "/api/login", {"username": "nobody", "password": "x"})
check(st == 429, "lockout also applies per IP address")
time.sleep(2.2)
st, _ = b.api("POST", "/api/login", {"username": "boss", "password": "boss-password-1"})
check(st == 200, "lock expires; right password works")
st, ev = admin.api("GET", "/api/activity")
check(any("Failed sign-in" in e["message"] for e in ev), "failed sign-ins logged for admins")

print("== sessions survive a restart")
app2 = App(DB)
check(app2.auth.from_token(admin.cookie()) is not None, "existing session still valid after restart")

print("== command-line password reset")
r = subprocess.run([sys.executable, "-m", "crate", "--db", DB, "reset-password", "boss", "recovered-pass-1"],
                   cwd=ROOT, capture_output=True, text=True,
                   env=dict(os.environ, CRATE_UPDATE_CHECKS="false"))
check(r.returncode == 0 and "updated" in r.stdout, f"reset-password command ({r.stdout.strip() or r.stderr.strip()})")
st, _ = Browser().api("POST", "/api/login", {"username": "boss", "password": "recovered-pass-1"})
check(st == 200, "admin signs in with the reset password")

print("== CRATE_ADMIN_USER bootstrap")
os.environ.update({"CRATE_ADMIN_USER": "envadmin", "CRATE_ADMIN_PASSWORD": "env-admin-pass1"})
app3 = App(os.path.join(tmp, "fresh.db"))
check(app3.auth.get("envadmin")["role"] == "admin", "admin pre-created from environment on a fresh install")

print()
print("FAILED:" if fails else "ALL PASSED", len(fails) or "")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
