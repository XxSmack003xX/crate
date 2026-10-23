"""Fake MusicBrainz + slskd on one port, for end-to-end testing without the network."""
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = "test-key"
ARTIST = {"id": "a-1", "name": "Test Band", "sort-name": "Test Band", "disambiguation": "", "country": "GB",
          "type": "Group", "score": 100, "life-span": {"begin": "1999"}}
# A second, same-named artist with no Wikidata photo (UI should fall back to album art).
ARTIST2 = {"id": "a-2", "name": "Test Band", "sort-name": "Test Band", "disambiguation": "jazz trio from Ohio",
           "country": "US", "type": "Group", "score": 90, "life-span": {"begin": "2015"}}

# A third artist, used by the library-import tests.
ARTIST3 = {"id": "a-3", "name": "Night Owls", "sort-name": "Night Owls", "disambiguation": "", "country": "CA",
           "type": "Group", "score": 100, "life-span": {"begin": "2009"}}
OWL_GROUPS = [
    ("rg-owl1", "Midnight Hours", "Album", [], "2011-04-01", ["Dusk", "Moonrise", "Witching"]),
    ("rg-owl2", "Dawn Chorus", "Album", [], "2013-09-01", ["First Bird", "Grey Light"]),
    ("rg-owl3", "Sunrise", "EP", [], "2015", ["Sunrise"]),
]
JAZZ_GROUPS = [("rg-jazz", "Late Night Standards", "Album", [], "2017-02-02", ["Blue", "Green"])]

# (rg id, title, primary, secondary, date, tracks)
GROUPS = [
    ("rg-first", "First Light", "Album", [], "2001-03-01", ["Opening", "Daybreak", "Closing Time"]),
    ("rg-second", "Second Wind (Deluxe Edition)", "Album", [], "2005-06-01", ["Gust", "Breeze", "Gale", "Calm"]),
    ("rg-small", "Small Things", "EP", [], "2003", ["Tiny", "Little"]),
    ("rg-live", "Live at Home", "Album", ["Live"], "2008", ["Opening (Live)"]),
    ("rg-hit", "Hit", "Single", [], "2004", ["Hit"]),
    ("rg-owned", "Owned Already", "Album", [], "2010", ["One", "Two", "Three"]),
    ("rg-rare", "Rare Gem", "Album", [], "2012", ["Shine", "Glimmer", "Glow"]),
    ("rg-flaky", "Flaky Connection", "Album", [], "2014", ["Drop", "Lag", "Ping"]),
    ("rg-future", "Not Out Yet", "Album", [], "2099-01-01", ["Soon"]),
]


def f(path, size, ext=None, br=None, locked=False):
    return {"filename": path, "size": size, "extension": ext or path.rsplit(".", 1)[-1],
            "bitRate": br, "isLocked": locked}


def responses_for(text):
    t = text.lower()
    out = []
    if "first light" in t:
        d = "@@music\\Test Band\\2001 - First Light [FLAC]"
        out.append({"username": "alice", "hasFreeUploadSlot": True, "uploadSpeed": 2_000_000, "queueLength": 0,
                    "files": [f(d + "\\01 - Opening.flac", 3000), f(d + "\\02 - Daybreak.flac", 3100),
                              f(d + "\\03 - Closing Time.flac", 3200), f(d + "\\cover.jpg", 500),
                              f(d + "\\log.txt", 50)]})
        d2 = "share\\mp3\\Test Band - First Light"
        out.append({"username": "bob", "hasFreeUploadSlot": True, "uploadSpeed": 5_000_000, "queueLength": 0,
                    "files": [f(d2 + f"\\0{i} - {n}.mp3", 1000, br=320) for i, n in
                              enumerate(["Opening", "Daybreak", "Closing Time"], 1)]})
        out.append({"username": "carol", "hasFreeUploadSlot": True, "uploadSpeed": 9_000_000, "queueLength": 0,
                    "files": [f("x\\Test Band\\First Light\\01 - Opening.flac", 3000)]})  # incomplete
        # Wrong album with the right track count — must be rejected by track titles.
        out.append({"username": "dave", "hasFreeUploadSlot": True, "uploadSpeed": 9_000_000, "queueLength": 0,
                    "files": [f(f"y\\Test Band\\First Light\\0{i} - Random {i}.flac", 3000) for i in range(1, 4)]})
    if "second wind" in t:
        d = "Music\\Test Band - Second Wind (2005)"
        out.append({"username": "alice", "hasFreeUploadSlot": True, "uploadSpeed": 2_000_000, "queueLength": 0,
                    "files": [f(d + "\\CD1\\01 Gust.flac", 2000), f(d + "\\CD1\\02 Breeze.flac", 2000),
                              f(d + "\\CD2\\01 Gale.flac", 2000), f(d + "\\CD2\\02 Calm.flac", 2000)]})
    if "small things" in t:
        out.append({"username": "erin", "hasFreeUploadSlot": True, "uploadSpeed": 1_000_000, "queueLength": 0,
                    "files": [f("m\\Test Band\\Small Things EP\\1 Tiny.mp3", 900, br=192),
                              f("m\\Test Band\\Small Things EP\\2 Little.mp3", 900, br=192)]})
        out.append({"username": "frank", "hasFreeUploadSlot": False, "uploadSpeed": 1_000_000, "queueLength": 3,
                    "files": [f("m\\Test Band\\Small Things\\1 Tiny.mp3", 1900, br=320),
                              f("m\\Test Band\\Small Things\\2 Little.mp3", 1900, br=320)]})
    if "flaky" in t:
        out.append({"username": "flaky", "hasFreeUploadSlot": True, "uploadSpeed": 9_000_000, "queueLength": 0,
                    "files": [f(f"z\\Test Band\\Flaky Connection\\0{i} {n}.flac", 3000) for i, n in
                              enumerate(["Drop", "Lag", "Ping"], 1)]})
        out.append({"username": "bob", "hasFreeUploadSlot": True, "uploadSpeed": 1_000_000, "queueLength": 0,
                    "files": [f(f"share\\Test Band\\Flaky Connection\\0{i} {n}.mp3", 1000, br=320) for i, n in
                              enumerate(["Drop", "Lag", "Ping"], 1)]})
    return out


class State:
    def __init__(self, downloads_dir):
        self.downloads = downloads_dir
        self.searches = {}
        self.transfers = {}  # user -> list
        self.lock = threading.Lock()
        self.next_id = 1
        self.enqueue_log = []
        self.search_log = []
        self.wikidata_calls = []
        self.logged_in = True
        self.reject_searches = False   # simulate slskd answering 400
        self.search_posts = 0

    def advance(self):
        now = time.time()
        with self.lock:
            for user, ts in self.transfers.items():
                for t in ts:
                    if t["state"].startswith("Completed"):
                        continue
                    age = now - t["_created"]
                    if user == "flaky" and age > 1:
                        t["state"] = "Completed, Errored"
                    elif age > 2.5:
                        t["state"] = "Completed, Succeeded"
                        t["bytesTransferred"] = t["size"]
                        parts = re.split(r"[\\/]", t["filename"])
                        dest = os.path.join(self.downloads, parts[-2], parts[-1])
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with open(dest, "wb") as fh:
                            fh.write(b"\0" * t["size"])
                    elif age > 1:
                        t["state"] = "InProgress"
                        t["bytesTransferred"] = t["size"] // 2


def make(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, obj=None):
            data = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else None

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            p = u.path
            if p.startswith("/ws/2/"):
                return self.mb(p[6:], qs)
            if p == "/w/api.php":  # Wikidata wbgetclaims
                state.wikidata_calls.append((qs["entity"][0], qs["property"][0]))
                if qs["entity"][0] == "Q42" and qs["property"][0] == "P18":
                    return self.send(200, {"claims": {"P18": [{"rank": "normal", "mainsnak": {
                        "datavalue": {"value": "Test Band live at Brixton 2003.jpg"}}}]}})
                return self.send(200, {"claims": {}})
            if not self.auth():
                return
            state.advance()
            if p == "/api/v0/server":
                if not state.logged_in:
                    return self.send(200, {"state": "Disconnected", "isConnected": False, "isLoggedIn": False})
                return self.send(200, {"state": "Connected, LoggedIn", "isConnected": True, "isLoggedIn": True,
                                       "username": "tester"})
            if p == "/api/v0/searches":
                now = time.time()
                return self.send(200, [{"id": s["id"], "searchText": s["text"],
                                        "isComplete": s.get("failed") or now - s["created"] > 1,
                                        "state": "Completed, Errored" if s.get("failed") else
                                        ("Completed, TimedOut" if now - s["created"] > 1 else "InProgress")}
                                       for s in state.searches.values()])
            m = re.match(r"^/api/v0/searches/([^/]+)(/responses)?$", p)
            if m:
                s = state.searches.get(m.group(1))
                if not s:
                    return self.send(404)
                done = time.time() - s["created"] > 1
                if m.group(2):
                    return self.send(200, responses_for(s["text"]))
                return self.send(200, {"id": s["id"], "searchText": s["text"], "isComplete": done,
                                       "state": "Completed, TimedOut" if done else "InProgress",
                                       "responseCount": len(responses_for(s["text"]))})
            m = re.match(r"^/api/v0/transfers/downloads/([^/]+)$", p)
            if m:
                user = urllib.parse.unquote(m.group(1))
                ts = state.transfers.get(user)
                if not ts:
                    return self.send(404)
                clean = [{k: v for k, v in t.items() if not k.startswith("_")} for t in ts]
                return self.send(200, {"username": user, "directories": [{"directory": "x", "files": clean}]})
            self.send(404)

        def do_POST(self):
            if not self.auth():
                return
            p = urllib.parse.urlparse(self.path).path
            b = self.body()
            if p == "/api/v0/searches":
                state.search_posts += 1
                if not state.logged_in or state.reject_searches:
                    # Real slskd records the failed search, then answers 400.
                    state.searches[b["id"]] = {"id": b["id"], "text": b["searchText"], "created": 0, "failed": True}
                    return self.send(400, {"error": "The server connection must be connected and logged in"})
                state.searches[b["id"]] = {"id": b["id"], "text": b["searchText"], "created": time.time()}
                state.search_log.append(b["searchText"])
                return self.send(200, {"id": b["id"]})
            m = re.match(r"^/api/v0/transfers/downloads/([^/]+)$", p)
            if m:
                user = urllib.parse.unquote(m.group(1))
                state.enqueue_log.append((user, [x["filename"] for x in b]))
                with state.lock:
                    lst = state.transfers.setdefault(user, [])
                    for x in b:
                        state.next_id += 1
                        lst.append({"id": f"t{state.next_id}", "username": user, "filename": x["filename"],
                                    "size": x["size"], "state": "Queued, Remotely", "bytesTransferred": 0,
                                    "requestedAt": time.strftime("%Y-%m-%dT%H:%M:%S") + f".{state.next_id:06d}",
                                    "_created": time.time()})
                return self.send(201)
            self.send(404)

        def do_DELETE(self):
            if not self.auth():
                return
            p = urllib.parse.urlparse(self.path).path
            m = re.match(r"^/api/v0/searches/([^/]+)$", p)
            if m:
                state.searches.pop(m.group(1), None)
                return self.send(204)
            m = re.match(r"^/api/v0/transfers/downloads/([^/]+)/([^/]+)$", p)
            if m:
                user = urllib.parse.unquote(m.group(1))
                with state.lock:
                    state.transfers[user] = [t for t in state.transfers.get(user, []) if t["id"] != m.group(2)]
                return self.send(204)
            self.send(404)

        def auth(self):
            if self.headers.get("X-API-Key") != API_KEY:
                self.send(401)
                return False
            return True

        # ---- MusicBrainz
        def mb(self, path, qs):
            if path == "artist":
                q = qs.get("query", [""])[0].lower()
                hits = [a for a in (ARTIST, ARTIST2) if "test" in q or "band" in q]
                if "owl" in q:
                    hits.append(ARTIST3)
                return self.send(200, {"artists": hits})
            if path.startswith("artist/") and path.endswith("a-3"):
                out = dict(ARTIST3)
                if "url-rels" in qs.get("inc", [""])[0]:
                    out["relations"] = []
                    out["release-groups"] = [{"id": g[0], "title": g[1], "primary-type": g[2], "secondary-types": g[3],
                                              "first-release-date": g[4]} for g in OWL_GROUPS]
                return self.send(200, out)
            if path.startswith("artist/"):
                art = ARTIST2 if path.endswith("a-2") else ARTIST
                out = dict(art)
                if "url-rels" in qs.get("inc", [""])[0]:
                    if art is ARTIST:
                        out["relations"] = [
                            {"type": "official homepage", "url": {"resource": "https://testband.example"}},
                            {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q42"}}]
                        out["release-groups"] = [{"id": g[0], "title": g[1], "primary-type": g[2],
                                                  "secondary-types": g[3], "first-release-date": g[4]} for g in GROUPS]
                    else:
                        out["relations"] = []
                        out["release-groups"] = [{"id": "rg-jazz", "title": "Late Night Standards",
                                                  "primary-type": "Album", "secondary-types": [],
                                                  "first-release-date": "2017-02-02"}]
                return self.send(200, out)
            if path == "release-group":
                src = {"a-2": JAZZ_GROUPS, "a-3": OWL_GROUPS}.get(qs.get("artist", [""])[0], GROUPS)
                rgs = [{"id": g[0], "title": g[1], "primary-type": g[2], "secondary-types": g[3],
                        "first-release-date": g[4]} for g in src]
                off = int(qs.get("offset", ["0"])[0])
                return self.send(200, {"release-group-count": len(rgs), "release-groups": rgs[off:off + 5]})
            if path == "release":
                rg = qs["release-group"][0]
                g = next(g for g in GROUPS + OWL_GROUPS + JAZZ_GROUPS if g[0] == rg)
                rels = [{"id": "rel-" + rg, "status": "Official", "date": g[4],
                         "media": [{"track-count": len(g[5])}]},
                        {"id": "rel2-" + rg, "status": "Official", "date": "2020",
                         "media": [{"track-count": len(g[5]) + 3}]}]
                return self.send(200, {"releases": rels})
            if path.startswith("release/"):
                rg = path.split("rel-", 1)[1]
                g = next(g for g in GROUPS + OWL_GROUPS + JAZZ_GROUPS if g[0] == rg)
                return self.send(200, {"media": [{"tracks": [{"title": t} for t in g[5]]}]})
            self.send(404)

    return H


def start(downloads_dir, port=0):
    state = State(downloads_dir)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state
