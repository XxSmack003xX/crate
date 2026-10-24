"""Update system tests: version compare, GitHub release check, container replace with
rollback (against a fake Docker engine), and the full in-app update flow.

Run:  python3 tests/test_updater.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
tmp = tempfile.mkdtemp(prefix="crate-upd-")
SOCK = os.path.join(tmp, "docker.sock")

# --- fake GitHub
RELEASE = {"tag_name": "v9.9.9", "name": "Crate 9.9.9", "body": "Big update", "published_at": "2026-10-24T00:00:00Z",
           "html_url": "https://github.com/XxSmack003xX/crate/releases/tag/v9.9.9"}
GH_CALLS = []


class GH(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        GH_CALLS.append(self.path)
        if self.path == "/repos/XxSmack003xX/crate/releases/latest":
            data = json.dumps(RELEASE).encode()
            self.send_response(200)
        else:
            data = b'{"message":"Not Found"}'
            self.send_response(404)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


gh = ThreadingHTTPServer(("127.0.0.1", 0), GH)
threading.Thread(target=gh.serve_forever, daemon=True).start()
os.environ.update({"CRATE_GITHUB_API": f"http://127.0.0.1:{gh.server_address[1]}", "CRATE_DOCKER_SOCKET": SOCK,
                   "CRATE_TICK_SECONDS": "3600", "CRATE_UPDATE_REPO": "XxSmack003xX/crate",
                   "CRATE_PROC_ROOT": os.path.join(tmp, "proc"),
                   "CRATE_UPDATE_CHECKS": "true"})  # CI turns checks off for the other suites

import fake_docker  # noqa: E402
from crate import __version__, updater as upd  # noqa: E402
from crate import update_helper  # noqa: E402
from crate.docker_api import Docker  # noqa: E402

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


print("== versions")
cv = upd.compare_versions
check(cv("1.2.1", "1.3.0") < 0 and cv("1.10.0", "1.9.9") > 0 and cv("v1.3.0", "1.3.0") == 0, "semantic version compare")
check(cv("1.3.0-beta.1", "1.3.0") < 0 and cv("1.3.0", "1.3.0-rc.1") > 0, "pre-releases sort before releases")
check(upd.normalize_repo("https://github.com/XxSmack003xX/crate.git") == "XxSmack003xX/crate", "repo URL normalised")

srv, eng = fake_docker.start(SOCK)
OLD_IMG, NEW_IMG, BAD_IMG = ("ghcr.io/xxsmack003xx/crate:1.2.1", "ghcr.io/xxsmack003xx/crate:9.9.9",
                             "ghcr.io/xxsmack003xx/crate:6.6.6")
OLD_CFG = {"Env": ["PATH=/usr/local/bin:/usr/bin", "CRATE_DB=/data/crate.db", "PYTHON_VERSION=3.12.7"],
           "Cmd": ["python", "-m", "crate", "--port", "9325"], "Entrypoint": None,
           "Healthcheck": {"Test": ["CMD", "python", "-c", "status"]}, "WorkingDir": "/app",
           "Labels": {"org.opencontainers.image.version": "1.2.1"}}
NEW_CFG = {"Env": ["PATH=/usr/local/bin:/usr/bin", "CRATE_DB=/data/crate.db", "PYTHON_VERSION=3.12.8",
                   "CRATE_RUNTIME=docker"],
           "Cmd": ["python", "-m", "crate", "--port", "9325"], "Entrypoint": ["python", "-m", "crate.entrypoint"],
           "Healthcheck": {"Test": ["CMD", "python", "-c", "version"]}, "WorkingDir": "/app",
           "Labels": {"org.opencontainers.image.version": "9.9.9"}}
eng.add_image(OLD_IMG, OLD_CFG)
eng.pullable[NEW_IMG] = NEW_CFG
eng.pullable[BAD_IMG] = NEW_CFG
eng.unhealthy_tags.add("6.6.6")


def make_crate():
    for c in list(eng.containers.values()):
        if c["Name"] == "/crate":
            del eng.containers[c["Id"]]
    return eng.add_container("crate", OLD_IMG, config={
        "Env": OLD_CFG["Env"] + ["CRATE_SLSKD_URL=http://slskd:5030", "PUID=1000"],
        "Labels": {"org.opencontainers.image.version": "1.2.1", "com.docker.compose.project": "crate"},
    }, host_config={"Binds": ["/srv/crate/crate-data:/data", "/mnt/music:/music", "/var/run/docker.sock:" + SOCK],
                    "PortBindings": {"9325/tcp": [{"HostPort": "9325"}]}, "RestartPolicy": {"Name": "unless-stopped"},
                    "NetworkMode": "crate_default", "ExtraHosts": ["host.docker.internal:host-gateway"]},
        networks=["crate_default", "media_net"])


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **k)
    return r, buf.getvalue()


fast = dict(poll=0.05, stable=0.2)

print("== replace: success")
eng.add_image(NEW_IMG, NEW_CFG)
old = make_crate()
r, out = quiet(update_helper.Replacer(Docker(SOCK), **fast).replace, old["Id"], NEW_IMG, ready_seconds=5,
               data_check=["sh", "-c", "test -f /data/crate.db"])
check(r["ok"], f"update succeeded ({r.get('reason')})")
new = next((c for c in eng.containers.values() if c["Name"] == "/crate"), None)
check(new and new["Id"] != old["Id"] and new["Config"]["Image"] == NEW_IMG, "container 'crate' now runs the new image")
check(old["Id"] not in eng.containers and not any("rollback" in c["Name"] for c in eng.containers.values()),
      "original container removed after success")
body = new["CreateBody"]
check(sorted(body["HostConfig"]["Binds"]) == sorted(old["HostConfig"]["Binds"]), "all mounts kept")
check(body["HostConfig"]["PortBindings"] == old["HostConfig"]["PortBindings"] and
      body["HostConfig"]["RestartPolicy"] == {"Name": "unless-stopped"} and
      body["HostConfig"]["ExtraHosts"] == ["host.docker.internal:host-gateway"], "ports, restart policy, extra hosts kept")
check(set(new["NetworkSettings"]["Networks"]) == {"crate_default", "media_net"}, "both networks reconnected")
check("CRATE_SLSKD_URL=http://slskd:5030" in body["Env"] and "PUID=1000" in body["Env"], "your environment kept")
check(not any(e.startswith("PYTHON_VERSION") for e in body["Env"]), "old image's built-in env dropped (new image's applies)")
check("Healthcheck" not in body and "Entrypoint" not in body and "Cmd" not in body,
      "old image's command/healthcheck not copied (new entrypoint + healthcheck apply)")
check(new["Config"]["Entrypoint"] == ["python", "-m", "crate.entrypoint"], "new image's entrypoint in effect")
check(body["Labels"].get("com.docker.compose.project") == "crate" and
      "org.opencontainers.image.version" not in body["Labels"], "compose labels kept, old image labels dropped")

print("== replace: user-customised command is kept")
eng.add_image(NEW_IMG, NEW_CFG)
old = make_crate()
old["Config"]["Cmd"] = ["python", "-m", "crate", "--port", "8080"]
r, _ = quiet(update_helper.Replacer(Docker(SOCK), **fast).replace, old["Id"], NEW_IMG, ready_seconds=5)
new = next(c for c in eng.containers.values() if c["Name"] == "/crate")
check(r["ok"] and new["CreateBody"].get("Cmd") == ["python", "-m", "crate", "--port", "8080"], "custom command carried over")

print("== replace: new version unhealthy → rollback")
eng.add_image(BAD_IMG, NEW_CFG)
old = make_crate()
r, out = quiet(update_helper.Replacer(Docker(SOCK), **fast).replace, old["Id"], BAD_IMG, ready_seconds=3)
check(not r["ok"] and r["rolled_back"] and "unhealthy" in r["reason"], f"rolled back ({r.get('reason')})")
cur = next(c for c in eng.containers.values() if c["Name"] == "/crate")
check(cur["Id"] == old["Id"] and cur["State"]["Running"] and cur["Config"]["Image"] == OLD_IMG,
      "original container restored, running the old version")
check(len([c for c in eng.containers.values() if not c["Name"].startswith("/crate-updater")]) == 1,
      "failed replacement removed")

print("== replace: a mount would be lost → rollback")
eng.drop_mounts_tags.add("9.9.9")
old = make_crate()
r, _ = quiet(update_helper.Replacer(Docker(SOCK), **fast).replace, old["Id"], NEW_IMG, ready_seconds=5)
check(not r["ok"] and "missing mounts" in r["reason"], f"refused to lose data ({r.get('reason')})")
check(next(c for c in eng.containers.values() if c["Name"] == "/crate")["Id"] == old["Id"], "original kept")
eng.drop_mounts_tags.clear()

print("== status outside Docker")
from crate.core import App  # noqa: E402

os.environ["CRATE_RUNTIME"] = "python"
app = App(os.path.join(tmp, "data", "crate.db"))
st = app.updater.status(force=True)
check(st["latest"]["version"] == "9.9.9" and st["update_available"], f"new release detected (you have {__version__})")
check(st["latest"]["image"] == NEW_IMG, "image for the release: ghcr.io/<repo>:<version>")
check(not st["can_auto_update"] and "git pull" in st["reason"], "outside Docker: explains how to update by hand")
check(st["repo_locked"], "update repo baked into the image is locked")
n = len(GH_CALLS)
app.updater.status()
check(len(GH_CALLS) == n, "release info cached (no GitHub call every page load)")
check(app.updater.banner() == {"version": "9.9.9", "url": RELEASE["html_url"]}, "banner for admins")

print("== status in Docker without the socket")
os.environ["CRATE_RUNTIME"] = "docker"
upd.DOCKER_SOCKET = os.path.join(tmp, "missing.sock")
app.updater._self = None
st = app.updater.status()
check(not st["can_auto_update"] and "socket isn't mounted" in st["reason"] and "docker compose pull" in st["reason"],
      "no socket: explains what to mount, plus the manual commands")
upd.DOCKER_SOCKET = SOCK

print("== full in-app update")
eng.add_image(NEW_IMG, NEW_CFG)  # reset
eng.images.pop(NEW_IMG)          # must be pulled
me = make_crate()
os.environ["CRATE_CONTAINER_NAME"] = "crate"
app.updater._self = None
st = app.updater.status(force=True)
check(st["can_auto_update"] and st["container"] == "crate", f"found its own container ({st['reason']})")
res = app.updater.apply({"username": "boss"})
check(res["ok"] and res["target_version"] == "9.9.9", "update started")
check(NEW_IMG in eng.images, "new image pulled first")
bk = os.path.join(tmp, "data", "backups")
check(os.path.isdir(bk) and any(f.startswith("crate-pre-update-9.9.9") for f in os.listdir(bk)), "database backed up")
helper = next(c for c in eng.containers.values() if c["Name"].startswith("/crate-updater"))
hb = helper["CreateBody"]
check(hb["Labels"] == {"crate.updater": "1"} and hb["Entrypoint"] == ["python", "-m", "crate.update_helper"]
      and hb["HostConfig"]["Binds"] == ["/var/run/docker.sock:" + SOCK] and hb["Image"] == OLD_IMG,
      "helper container: current image, socket mounted, labelled")
check(app.updater.status()["applying"]["to"] == "9.9.9", "status shows the update in progress")

# Run the helper the way its container would, then let Crate collect the result.
env = dict(e.split("=", 1) for e in hb["Env"])
saved = dict(os.environ)
os.environ.update(env)
os.environ["CRATE_UPDATE_DELAY"] = "0"
os.environ["CRATE_UPDATE_READY_SECONDS"] = "5"
orig_init = update_helper.Replacer.__init__
update_helper.Replacer.__init__ = lambda self, d, logf=update_helper.log, poll=0.05, stable=0.2: orig_init(self, d, logf, poll, stable)
code, out = quiet(update_helper.main)
update_helper.Replacer.__init__ = orig_init
os.environ.clear()
os.environ.update(saved)
check(code == 0 and "RESULT" in out, "helper replaced the container")
eng.logs[helper["Id"]] = out
helper["State"] = {"Running": False, "Status": "exited", "ExitCode": 0}
app.updater.collect_helpers()
lr = app.updater.last_result()
check(lr and lr["ok"] and lr["from"] == __version__ and lr["to"] == "9.9.9", "result recorded for Settings → Updates")
check(helper["Id"] not in eng.containers, "finished helper container cleaned up")
check(app.updater.applying is None, "'update in progress' cleared")
ev = app.db.q("SELECT message FROM events ORDER BY id DESC LIMIT 5")
check(any("installed" in e["message"] for e in ev), "update logged in Activity")

print("== image not published yet")
RELEASE["tag_name"] = "v9.9.10"
app.updater._self = None
make_crate()
try:
    app.updater.apply({"username": "boss"})
    check(False, "missing image refused")
except upd.UpdateError as e:
    check("still be building" in str(e), f"clear message when the image isn't on GHCR yet")

print()
print("FAILED:" if fails else "ALL PASSED", len(fails) or "")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
