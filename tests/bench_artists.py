"""Benchmark the Artists page with a large library.

Run:  python3 tests/bench_artists.py [artists] [albums_per_artist]
"""
import json
import os
import random
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.update({"CRATE_TICK_SECONDS": "3600", "CRATE_UPDATE_CHECKS": "false"})
N = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
PER = int(sys.argv[2]) if len(sys.argv) > 2 else 20

from http.server import ThreadingHTTPServer  # noqa: E402
from crate.core import App  # noqa: E402
from crate.server import make_handler, _artist_rows, _grid_cache  # noqa: E402
from client import login_opener  # noqa: E402

tmp = tempfile.mkdtemp(prefix="crate-bench-")
app = App(os.path.join(tmp, "crate.db"))
random.seed(1)
statuses = ["have"] * 6 + ["skipped"] * 5 + ["missing"] * 3 + ["wanted", "not_found", "downloading", "queued"]
types = ["Album"] * 5 + ["EP", "Single", "Single"]
with app.db.lock:
    c = app.db.conn
    c.execute("BEGIN")
    for i in range(N):
        aid = c.execute("INSERT INTO artists (mbid, name, sort_name, added_at, sync_status) VALUES (?,?,?,?, 'ok')",
                        (f"a-{i}", f"Artist {i:04}", f"Artist {i:04}", "2026-01-01")).lastrowid
        c.executemany("INSERT INTO albums (mbid, artist_id, title, primary_type, release_date, status) VALUES (?,?,?,?,?,?)",
                      [(f"rg-{i}-{j}", aid, f"Album {j}", random.choice(types), f"{1990 + j}-01-01",
                        random.choice(statuses)) for j in range(PER)])
    c.execute("COMMIT")

httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"
op = login_opener(base)


def timeit(fn, n=10):
    fn()
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n * 1000


q_ms = timeit(lambda: _artist_rows(app, grid=True) and _grid_cache.update(key=None))
size = 0


def http():
    global size
    with op.open(base + "/api/artists", timeout=60) as r:
        size = len(r.read())


def http_changed():
    app.db.versions["albums"] = app.db.versions.get("albums", 0) + 1  # as if a download moved on
    http()


h_ms = timeit(http_changed)
hc_ms = timeit(http)
print(json.dumps({"artists": N, "albums": N * PER, "query_ms": round(q_ms, 1),
                  "changed_refresh_ms": round(h_ms, 1), "shared_cache_refresh_ms": round(hc_ms, 1),
                  "response_kb": round(size / 1024)}))

# Conditional request (only meaningful once ETags exist)
req = urllib.request.Request(base + "/api/artists")
with op.open(req, timeout=60) as r:
    etag = r.headers.get("ETag")
if etag:
    def cond():
        rq = urllib.request.Request(base + "/api/artists", headers={"If-None-Match": etag})
        try:
            op.open(rq, timeout=60).read()
            return 200
        except urllib.error.HTTPError as e:
            return e.code
    code = cond()
    c_ms = timeit(cond)
    print(json.dumps({"unchanged_refresh_status": code, "unchanged_refresh_ms": round(c_ms, 2)}))
