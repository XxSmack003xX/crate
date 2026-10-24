"""Signed-in HTTP opener for tests: runs first-run setup (or logs in) and keeps the
session cookie; adds the X-Crate header Crate requires on changes."""
import http.cookiejar
import json
import urllib.request


def login_opener(base, username="admin", password="admin-pass-123"):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("X-Crate", "1")]
    with opener.open(base + "/api/setup-needed", timeout=10) as r:
        needed = json.loads(r.read())["needed"]
    path = "/api/setup" if needed else "/api/login"
    req = urllib.request.Request(base + path, data=json.dumps({"username": username, "password": password}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=30) as r:
        r.read()
    return opener
