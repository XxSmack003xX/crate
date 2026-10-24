"""Update checks (GitHub Releases) and one-click Docker self-update (modelled on MEDIARR).

* Releases are checked every 6 hours (and on demand) at
  https://api.github.com/repos/<repo>/releases/latest. Official images bake the repo
  into CRATE_UPDATE_REPO; other installs can set it in Settings → Updates.
* In Docker with the socket mounted, "Update now" backs up the database, pulls
  ghcr.io/<repo>:<version>, and starts a short-lived helper container that swaps
  the Crate container over with rollback (see update_helper.py).
* Otherwise Crate says why it can't update itself and how to update by hand.
"""
import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from . import __version__
from .docker_api import Docker, demux_logs

GITHUB_API = os.environ.get("CRATE_GITHUB_API", "https://api.github.com")
DOCKER_SOCKET = os.environ.get("CRATE_DOCKER_SOCKET", "/var/run/docker.sock")
PROC_ROOT = os.environ.get("CRATE_PROC_ROOT", "/proc")
CHECK_EVERY = float(os.environ.get("CRATE_UPDATE_CHECK_HOURS", "6")) * 3600
HELPER_LABEL = "crate.updater"
MANUAL = "Until then, update with: docker compose pull && docker compose up -d"


class UpdateError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def clean_version(v):
    return str(v or "").strip().lstrip("vV").split("+")[0]


def version_parts(v):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$", clean_version(v))
    return (int(m[1]), int(m[2]), int(m[3]), m[4] or "") if m else None


def compare_versions(a, b):
    A, B = version_parts(a), version_parts(b)
    if not A or not B:
        return (str(a) > str(b)) - (str(a) < str(b))
    if A[:3] != B[:3]:
        return -1 if A[:3] < B[:3] else 1
    if A[3] == B[3]:
        return 0
    if not A[3]:
        return 1   # 1.2.0 > 1.2.0-beta
    if not B[3]:
        return -1
    return -1 if A[3] < B[3] else 1


def normalize_repo(raw):
    x = re.sub(r"^https?://github\.com/", "", str(raw or "").strip(), flags=re.I)
    x = re.sub(r"\.git$", "", x).strip("/")
    return x if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", x) else ""


class Updater:
    def __init__(self, app, db_path):
        self.app = app
        self.db_path = os.path.abspath(db_path)
        self.lock = threading.Lock()
        self._cache = {"key": "", "at": 0, "value": None, "error": None}
        self._last_check = 0
        self._last_collect = 0
        self._self = None
        self._self_error = ""
        self.applying = None

    # ------------------------------------------------------------------ config
    def repo_locked(self):
        return bool(normalize_repo(os.environ.get("CRATE_UPDATE_REPO")))

    def repo(self):
        return normalize_repo(os.environ.get("CRATE_UPDATE_REPO") or self.app.settings.raw().get("update_repo"))

    def enabled(self):
        return self.app.settings.bool("update_checks")

    # ------------------------------------------------------------------ GitHub
    def latest(self, force=False):
        repo = self.repo()
        if not repo:
            raise UpdateError(400, "Set the GitHub repository (owner/repository) to check for updates")
        c = self._cache
        if not force and c["key"] == repo.lower() and c["value"] and time.time() - c["at"] < 300:
            return c["value"]
        req = urllib.request.Request(f"{GITHUB_API}/repos/{repo}/releases/latest", headers={
            "Accept": "application/vnd.github+json", "User-Agent": f"Crate/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise UpdateError(502, f"No published GitHub release was found for {repo}")
            if e.code in (403, 429):
                raise UpdateError(502, "GitHub's rate limit was reached; Crate will check again later")
            raise UpdateError(502, f"GitHub returned HTTP {e.code}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise UpdateError(502, f"Couldn't reach GitHub: {getattr(e, 'reason', e)}")
        version = clean_version(d.get("tag_name") or d.get("name"))
        if not version_parts(version):
            raise UpdateError(502, "The latest release doesn't use a version tag like v1.3.0")
        value = {"repo": repo, "version": version, "tag": d.get("tag_name") or "v" + version,
                 "name": d.get("name") or d.get("tag_name") or version,
                 "notes": str(d.get("body") or "")[:20000], "published_at": d.get("published_at") or "",
                 "url": d.get("html_url") or f"https://github.com/{repo}/releases/latest",
                 "image": f"ghcr.io/{repo.lower()}:{version}"}
        self._cache = {"key": repo.lower(), "at": time.time(), "value": value, "error": None}
        return value

    # ------------------------------------------------------------------ runtime
    def in_container(self):
        rt = os.environ.get("CRATE_RUNTIME", "").lower()
        if rt in ("docker", "podman"):
            return True
        if rt == "python":
            return False
        if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
            return True
        try:
            with open(f"{PROC_ROOT}/1/cgroup") as fh:
                return bool(re.search(r"docker|kubepods|containerd|libpod", fh.read()))
        except OSError:
            return False

    @staticmethod
    def _ids_from_proc():
        ids = []
        try:
            with open(f"{PROC_ROOT}/self/mountinfo") as fh:
                mi = fh.read(512 * 1024)
            ids += re.findall(r"/([0-9a-f]{64})/(?:userdata/)?(?:hostname|hosts|resolv\.conf)(?=\s)", mi)
        except OSError:
            pass
        for f in ("self/cgroup", "1/cgroup"):
            try:
                with open(f"{PROC_ROOT}/{f}") as fh:
                    ids += re.findall(r"(?:docker[-/]|libpod-|crio-|cri-containerd-)([0-9a-f]{64})", fh.read())
            except OSError:
                pass
        return list(dict.fromkeys(ids))

    def docker(self):
        return Docker(DOCKER_SOCKET)

    def self_inspect(self):
        """Find our own container through the socket (cached once found)."""
        if self._self:
            return self._self
        if not os.path.exists(DOCKER_SOCKET):
            self._self_error = "no-socket"
            return None
        self._self_error = ""
        d = self.docker()
        ids = self._ids_from_proc()
        host = socket.gethostname()
        named = os.environ.get("CRATE_CONTAINER_NAME", "").strip()
        tries = list(dict.fromkeys(ids + ([named] if named else []) + ([host] if host else [])))
        for ref in tries:
            try:
                st, c, _ = d.call("GET", f"/containers/{urllib.parse.quote(ref)}/json", timeout=10)
            except PermissionError:
                self._self_error = "permission"
                return None
            except OSError:
                self._self_error = "unreachable"
                return None
            except Exception:
                self._self_error = "unreachable"
                return None
            if st != 200 or not c:
                continue
            cid = str(c.get("Id") or "")
            me = (cid in ids) if ids else (cid.startswith(host) or (c.get("Config") or {}).get("Hostname") == host)
            if me or (ref == named and not ids):
                self._self = c
                return c
        self._self_error = self._self_error or "not-found"
        return None

    def _self_reason(self):
        e = self._self_error
        if e == "no-socket":
            return (f"Crate is running in Docker, but the Docker socket isn't mounted, so it can't replace its own "
                    f"container. Mount {DOCKER_SOCKET} as in the included docker-compose.yml to enable one-click "
                    f"updates. {MANUAL}")
        if e == "permission":
            return ("Crate is running in Docker, but it isn't allowed to use the Docker socket. Remove any `user:` "
                    f"line from Crate's compose service (use PUID/PGID instead) and recreate it. {MANUAL}")
        if e == "unreachable":
            return f"The Docker API didn't answer just now. Select Check now to try again. {MANUAL}"
        return ("Crate couldn't identify its own container through the Docker socket. Set CRATE_CONTAINER_NAME to "
                f"the container's name (for example crate) and recreate it. {MANUAL}")

    @staticmethod
    def _socket_source(inspect):
        for m in (inspect or {}).get("Mounts") or []:
            if str(m.get("Destination")) == DOCKER_SOCKET and m.get("Source"):
                return str(m["Source"])
        return ""

    # ------------------------------------------------------------------ status
    def status(self, force=False):
        in_docker = self.in_container()
        me = self.self_inspect() if in_docker else None
        out = {
            "current_version": __version__, "repo": self.repo(), "repo_locked": self.repo_locked(),
            "enabled": self.enabled(), "update_available": False, "docker": in_docker,
            "container": (me or {}).get("Name", "").lstrip("/") if me else "",
            "current_image": ((me or {}).get("Config") or {}).get("Image", "") if me else "",
            "can_auto_update": False, "reason": "", "latest": None, "message": "",
            "last_result": self.last_result(), "applying": self.applying,
        }
        if not in_docker:
            out["reason"] = ("Crate is running directly with Python, so it can't update itself. Update it with "
                             "`git pull` (or download the new release) and restart it.")
        elif not me:
            out["reason"] = self._self_reason()
        elif not self._socket_source(me):
            out["reason"] = f"The Docker socket isn't a host bind mount Crate can pass to the updater. {MANUAL}"
        else:
            out["can_auto_update"] = True
            self.collect_helpers()
            out["last_result"] = self.last_result()
        if not out["enabled"]:
            out["message"] = "Update checks are turned off"
            return out
        try:
            latest = self.latest(force)
            out["latest"] = latest
            out["update_available"] = compare_versions(__version__, latest["version"]) < 0
        except UpdateError as e:
            out["message"] = str(e)
            self._cache["error"] = str(e)
        return out

    def banner(self):
        """Cheap summary for the header (no network)."""
        v = self._cache.get("value")
        if not self.enabled() or not v:
            return None
        if compare_versions(__version__, v["version"]) < 0:
            return {"version": v["version"], "url": v["url"]}
        return None

    def periodic(self):
        """Called from the worker loop: background release check + helper cleanup."""
        now = time.time()
        if self.enabled() and self.repo() and now - self._last_check > CHECK_EVERY:
            self._last_check = now
            try:
                v = self.latest(force=True)
                if compare_versions(__version__, v["version"]) < 0 and \
                        self.app.settings.raw().get("update_notified") != v["version"]:
                    self.app.db.save_settings({"update_notified": v["version"]})
                    self.app.db.event(f"Crate {v['version']} is available (you have {__version__}). "
                                      "Admins can update from Settings → Updates.", "success")
            except UpdateError:
                pass
        if now - self._last_collect > 60 and os.path.exists(DOCKER_SOCKET):
            self._last_collect = now
            try:
                self.collect_helpers()
            except Exception:
                pass

    # ------------------------------------------------------------------ results
    def last_result(self):
        raw = self.app.settings.raw().get("update_last_result")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def collect_helpers(self):
        """Read the RESULT line of finished helper containers, then remove them."""
        d = self.docker()
        filters = urllib.parse.quote(json.dumps({"label": [f"{HELPER_LABEL}=1"]}))
        st, items, _ = d.call("GET", f"/containers/json?all=1&filters={filters}", timeout=10)
        if st != 200 or not isinstance(items, list):
            return
        collected = False
        for it in items:
            if it.get("State") == "running":
                continue
            collected = True
            cid = it.get("Id")
            st, _, raw = d.call("GET", f"/containers/{cid}/logs?stdout=1&stderr=1", timeout=15)
            text = demux_logs(raw or b"")
            result = None
            for line in text.splitlines():
                if "[crate-updater] RESULT " in line:
                    try:
                        result = json.loads(line.split("RESULT ", 1)[1])
                    except ValueError:
                        pass
            if result:
                result["log"] = [l for l in text.splitlines() if "RESULT " not in l][-40:]
                self.app.db.save_settings({"update_last_result": json.dumps(result)})
                ok = result.get("ok")
                self.app.db.event(
                    f"Update {result.get('from')} → {result.get('to')}: " +
                    ("installed" if ok else ("rolled back: " if result.get("rolled_back") else "FAILED: ") +
                     str(result.get("reason", ""))),
                    "success" if ok else "error")
            d.call("DELETE", f"/containers/{cid}?force=1", timeout=15)
        if collected:
            self.applying = None

    # ------------------------------------------------------------------ apply
    def backup(self, tag):
        folder = os.path.join(os.path.dirname(self.db_path), "backups")
        os.makedirs(folder, exist_ok=True)
        name = f"crate-{tag}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        dest = os.path.join(folder, name)
        src = sqlite3.connect(self.db_path)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        # keep the 5 newest pre-update backups
        olds = sorted(f for f in os.listdir(folder) if f.startswith("crate-pre-update-"))
        for f in olds[:-5]:
            try:
                os.remove(os.path.join(folder, f))
            except OSError:
                pass
        return dest

    def pull(self, image):
        i = str(image or "")
        at = i.rfind(":")
        if not i.lower().startswith("ghcr.io/") or at <= len("ghcr.io/"):
            raise UpdateError(400, "Refusing to pull a non-GHCR update image")
        frm, tag = i[:at], i[at + 1:]
        st, _, raw = self.docker().call(
            "POST", f"/images/create?fromImage={urllib.parse.quote(frm)}&tag={urllib.parse.quote(tag)}",
            timeout=600)
        err = None
        for line in (raw or b"").decode("utf-8", "replace").splitlines():
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if j.get("error") or j.get("errorDetail"):
                err = j.get("error") or (j.get("errorDetail") or {}).get("message")
            elif st >= 300 and j.get("message"):
                err = j["message"]
        if st >= 300 or err:
            msg = err or f"HTTP {st}"
            if re.search(r"manifest unknown|not found", msg, re.I):
                msg += (". The image for this release may still be building on GitHub (about 3 minutes after "
                        "a release), or the package isn't public yet.")
            elif re.search(r"denied|unauthorized", msg, re.I):
                msg += ". The image package on GitHub must be public so Crate can download it."
            raise UpdateError(502, "Couldn't download the new image: " + msg)

    def apply(self, actor, force=False):
        with self.lock:
            st = self.status(force=True)
            if not st["enabled"]:
                raise UpdateError(409, "Update checks are turned off")
            if not st["latest"]:
                raise UpdateError(502, st["message"] or "Couldn't find the latest release")
            if not st["can_auto_update"]:
                raise UpdateError(409, st["reason"] or "Automatic update isn't available")
            if not st["update_available"] and not force:
                raise UpdateError(409, "Crate is already up to date")
            latest = st["latest"]
            backup = self.backup("pre-update-" + latest["version"])
            self.pull(latest["image"])
            helper = self._launch_helper(latest)
            self.applying = {"to": latest["version"], "from": __version__, "at": time.time()}
            who = actor["username"] if actor else "?"
            self.app.db.event(f"{who} started the update {__version__} → {latest['version']} "
                              f"(database backed up to {os.path.basename(backup)})", username=who)
            return {"ok": True, "target_version": latest["version"], "target_image": latest["image"],
                    "backup": os.path.basename(backup), "helper": helper,
                    "message": "Updating. Crate will restart in a moment."}

    def _launch_helper(self, latest):
        me = self.self_inspect()
        if not me:
            raise UpdateError(409, self._self_reason())
        sock = self._socket_source(me)
        if not sock:
            raise UpdateError(409, "Couldn't find the host Docker socket mount")
        image = (me.get("Config") or {}).get("Image") or ""
        if not image:
            raise UpdateError(500, "Couldn't determine the current Crate image")
        body = {
            "Image": image,
            "Entrypoint": ["python", "-m", "crate.update_helper"],
            "Cmd": [],
            "User": "0",
            "Labels": {HELPER_LABEL: "1"},
            "Env": [f"CRATE_DOCKER_SOCKET={DOCKER_SOCKET}", f"CRATE_UPDATE_SELF_ID={me['Id']}",
                    f"CRATE_UPDATE_TARGET_IMAGE={latest['image']}",
                    f"CRATE_UPDATE_TARGET_VERSION={latest['version']}", f"CRATE_UPDATE_OLD_VERSION={__version__}"],
            "HostConfig": {"AutoRemove": False, "Binds": [f"{sock}:{DOCKER_SOCKET}"], "RestartPolicy": {"Name": "no"}},
        }
        d = self.docker()
        name = f"crate-updater-{int(time.time())}"
        st, data, _ = d.call("POST", f"/containers/create?name={name}", body, timeout=15)
        if st not in (200, 201) or not (data or {}).get("Id"):
            raise UpdateError(502, (data or {}).get("message") or "Couldn't create the update helper")
        st, data2, _ = d.call("POST", f"/containers/{data['Id']}/start", timeout=15)
        if st not in (204, 304):
            raise UpdateError(502, (data2 or {}).get("message") or "Couldn't start the update helper")
        return data["Id"][:12]
