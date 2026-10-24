"""One-shot Crate self-updater (a Python port of MEDIARR's update-helper.js and
docker-recreate.js).

Crate launches this as a short-lived container from the *current* image, with the
Docker socket mounted, because a process can't safely replace the container it is
running inside. Safety model:

  1. The running Crate container is RENAMED and stopped, never deleted up front.
  2. The replacement is created under the original name with the same settings,
     mounts, ports and networks, on the new image.
  3. It must start, become healthy, keep every original mount, and still see its
     database.
  4. Only then is the original deleted (containers only; volumes are never removed).
  5. On any failure the replacement is removed and the ORIGINAL container is renamed
     back and restarted, unchanged.

The last log line is "[crate-updater] RESULT {json}"; Crate reads it afterwards to
show the outcome in Settings → Updates.
"""
import json
import os
import sys
import time

from .docker_api import Docker

HOST_CONFIG_KEYS = [
    "Binds", "PortBindings", "RestartPolicy", "NetworkMode", "ExtraHosts", "GroupAdd", "Devices", "DeviceRequests",
    "CapAdd", "CapDrop", "SecurityOpt", "Privileged", "ReadonlyRootfs", "Tmpfs", "ShmSize", "Init", "Runtime", "Dns",
    "DnsOptions", "DnsSearch", "Sysctls", "IpcMode", "PidMode", "UTSMode", "UsernsMode", "LogConfig", "Links",
    "VolumeDriver", "VolumesFrom", "PublishAllPorts", "CgroupnsMode", "Cgroup", "OomScoreAdj", "StorageOpt",
    "CpuShares", "Memory", "NanoCpus", "CgroupParent", "BlkioWeight", "BlkioWeightDevice", "BlkioDeviceReadBps",
    "BlkioDeviceWriteBps", "BlkioDeviceReadIOps", "BlkioDeviceWriteIOps", "CpuPeriod", "CpuQuota",
    "CpuRealtimePeriod", "CpuRealtimeRuntime", "CpusetCpus", "CpusetMems", "DeviceCgroupRules",
    "MemoryReservation", "MemorySwap", "MemorySwappiness", "OomKillDisable", "PidsLimit", "Ulimits",
    "CpuCount", "CpuPercent", "IOMaximumIOps", "IOMaximumBandwidth", "MaskedPaths", "ReadonlyPaths",
]

# Settings an image provides by default. If the container still has the OLD image's
# value, leave it out so the NEW image's value applies (e.g. a new entrypoint or
# healthcheck). If the user changed it (compose `command:`, `healthcheck:`...), keep it.
IMAGE_DEFAULT_KEYS = ["Cmd", "Entrypoint", "Healthcheck", "WorkingDir", "User", "StopSignal", "Volumes"]


def log(msg):
    print("[crate-updater] " + msg, flush=True)


def recreate_host_config(inspect):
    h = (inspect or {}).get("HostConfig") or {}
    out = {k: h[k] for k in HOST_CONFIG_KEYS if h.get(k) is not None}
    binds = list(out.get("Binds") or [])
    dests = set()
    for b in binds:
        parts = str(b or "").split(":")
        if len(parts) >= 2:
            dests.add(parts[1])
    # Named/anonymous volumes and --mount binds only appear in Mounts: fold them in.
    for m in (inspect or {}).get("Mounts") or []:
        d = m.get("Destination")
        if not d or d in dests:
            continue
        ro = ":ro" if m.get("RW") is False else ""
        if m.get("Type") == "volume" and m.get("Name"):
            binds.append(f"{m['Name']}:{d}{ro}")
        elif m.get("Type") == "bind" and m.get("Source"):
            binds.append(f"{m['Source']}:{d}{ro}")
        dests.add(d)
    if binds:
        out["Binds"] = binds
    out["AutoRemove"] = False
    return out


def recreate_networking(inspect):
    nets = ((inspect or {}).get("NetworkSettings") or {}).get("Networks") or {}
    cid = str((inspect or {}).get("Id") or "")
    ep = {}
    for name, n in nets.items():
        if not name or not n:
            continue
        aliases = [a for a in (n.get("Aliases") or []) if a and a != cid and not cid.startswith(str(a))]
        cfg = {}
        if aliases:
            cfg["Aliases"] = aliases
        for k in ("DriverOpts", "IPAMConfig"):
            if n.get(k):
                cfg[k] = n[k]
        if n.get("Links"):
            cfg["Links"] = n["Links"]
        ep[name] = cfg
    return {"EndpointsConfig": ep} if ep else None


def recreate_body(inspect, image, old_image_config=None, drop_env=()):
    c = (inspect or {}).get("Config") or {}
    base = old_image_config or {}
    env = c.get("Env")
    if env is not None:
        baked = set(base.get("Env") or [])
        drop = set(drop_env)
        env = [e for e in env if e not in baked and e.split("=", 1)[0] not in drop]
    labels = c.get("Labels")
    if labels:
        img_labels = base.get("Labels") or {}
        labels = {k: v for k, v in labels.items() if img_labels.get(k) != v}
    body = {
        "Image": image,
        "Hostname": c.get("Hostname") or None,
        "Domainname": c.get("Domainname") or None,
        "AttachStdin": c.get("AttachStdin"),
        "AttachStdout": c.get("AttachStdout"),
        "AttachStderr": c.get("AttachStderr"),
        "ExposedPorts": c.get("ExposedPorts") or None,
        "Tty": c.get("Tty"),
        "OpenStdin": c.get("OpenStdin"),
        "StdinOnce": c.get("StdinOnce"),
        "Env": env,
        "MacAddress": c.get("MacAddress") or None,
        "Labels": labels or None,
        "StopTimeout": c.get("StopTimeout"),
        "Shell": c.get("Shell") or None,
        "HostConfig": recreate_host_config(inspect),
    }
    for k in IMAGE_DEFAULT_KEYS:
        v = c.get(k)
        if v in (None, "", [], {}):
            continue
        if old_image_config is not None and base.get(k) == v:
            continue  # unchanged from the old image: let the new image decide
        body[k] = v
    net = recreate_networking(inspect)
    if net:
        body["NetworkingConfig"] = net
    return {k: v for k, v in body.items() if v is not None}


def split_networking(body):
    """Docker < 25 rejects more than one network at create time: attach the rest after."""
    ep = ((body or {}).get("NetworkingConfig") or {}).get("EndpointsConfig") or {}
    names = list(ep)
    if len(names) <= 1:
        return body, []
    mode = (body.get("HostConfig") or {}).get("NetworkMode")
    primary = mode if mode in names else names[0]
    create = dict(body, NetworkingConfig={"EndpointsConfig": {primary: ep[primary]}})
    return create, [(n, ep[n]) for n in names if n != primary]


def mounts_match(old, new):
    def key(m):
        t = m.get("Type")
        if t == "volume":
            return "volume:" + str(m.get("Name"))
        if t == "bind":
            return "bind:" + str(m.get("Source"))
        return f"{t}:{m.get('Name') or m.get('Source') or ''}"
    have = {m.get("Destination"): key(m) for m in (new or {}).get("Mounts") or []}
    missing = [m["Destination"] for m in (old or {}).get("Mounts") or []
               if m.get("Destination") and m.get("Type") != "tmpfs" and have.get(m["Destination"]) != key(m)]
    return not missing, missing


class Replacer:
    def __init__(self, docker, logf=log, poll=1.5, stable=6.0):
        self.d = docker
        self.log = logf
        self.poll = poll
        self.stable = stable

    def _ok(self, r, codes=(200, 201, 204, 304)):
        return r[0] in codes

    def _must(self, method, path, body=None, codes=(200, 201, 204, 304), timeout=30, what=""):
        r = self.d.call(method, path, body, timeout)
        if not self._ok(r, codes):
            msg = (r[1] or {}).get("message") if isinstance(r[1], dict) else None
            raise RuntimeError(f"{what + ': ' if what else ''}{msg or 'HTTP ' + str(r[0])}")
        return r

    def inspect(self, cid):
        st, data, _ = self.d.call("GET", f"/containers/{cid}/json", timeout=15)
        if st == 404:
            return None
        if st != 200:
            raise RuntimeError(f"inspect: HTTP {st}")
        return data or {}

    def image_config(self, image_id):
        try:
            st, data, _ = self.d.call("GET", f"/images/{image_id}/json", timeout=15)
            return (data or {}).get("Config") or {} if st == 200 else None
        except Exception:
            return None

    def rename(self, cid, name):
        from urllib.parse import quote
        self._must("POST", f"/containers/{cid}/rename?name={quote(name)}", codes=(200, 204), what="rename")

    def stop(self, cid):
        self._must("POST", f"/containers/{cid}/stop?t=10", codes=(204, 304), timeout=45, what="stop")

    def start(self, cid):
        self._must("POST", f"/containers/{cid}/start", codes=(204, 304), what="start")

    def remove(self, cid):
        """Container only: `v` is never set, so volumes survive."""
        if not cid:
            return True
        try:
            self.d.call("POST", f"/containers/{cid}/stop?t=10", timeout=45)
        except Exception:
            pass
        for _ in range(3):
            try:
                self.d.call("DELETE", f"/containers/{cid}?force=1", timeout=30)
            except Exception:
                pass
            try:
                if not self.inspect(cid):
                    return True
            except Exception:
                pass
            time.sleep(self.poll)
        return False

    def create(self, name, body):
        from urllib.parse import quote
        create_body, extra = split_networking(body)
        r = self._must("POST", f"/containers/create?name={quote(name)}", create_body, codes=(201,), what="create")
        cid = (r[1] or {}).get("Id")
        if not cid:
            raise RuntimeError("create: Docker did not return a container ID")
        try:
            for net, cfg in extra:
                self._must("POST", f"/networks/{quote(net)}/connect", {"Container": cid, "EndpointConfig": cfg or {}},
                           codes=(200, 204), what=f"connect network {net}")
        except Exception:
            self.remove(cid)
            raise
        return cid

    def wait_ready(self, cid, seconds):
        until = time.time() + seconds
        stable_since = 0
        while time.time() < until:
            time.sleep(self.poll)
            c = self.inspect(cid)
            if not c:
                return False, "container disappeared"
            s = c.get("State") or {}
            if s.get("Dead") or s.get("Status") == "exited" or s.get("OOMKilled"):
                return False, "exited: " + (s.get("Error") or f"code {s.get('ExitCode')}")
            health = (s.get("Health") or {}).get("Status")
            if health == "healthy":
                return True, ""
            if health == "unhealthy":
                return False, "became unhealthy"
            if not health and s.get("Running") and not s.get("Restarting"):
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= self.stable:
                    return True, ""
            elif not health:
                stable_since = 0
        return False, f"did not become ready within {seconds}s"

    def exec_code(self, cid, cmd, seconds=15):
        try:
            st, data, _ = self.d.call("POST", f"/containers/{cid}/exec",
                                      {"Cmd": cmd, "AttachStdout": False, "AttachStderr": False}, 15)
            if st != 201 or not data or not data.get("Id"):
                return None
            ex = data["Id"]
            st, _, _ = self.d.call("POST", f"/exec/{ex}/start", {"Detach": True}, 15)
            if st not in (200, 204):
                return None
            until = time.time() + seconds
            while time.time() < until:
                st, j, _ = self.d.call("GET", f"/exec/{ex}/json", timeout=10)
                if st == 200 and j and j.get("Running") is False:
                    return j.get("ExitCode") if isinstance(j.get("ExitCode"), int) else None
                time.sleep(0.4)
        except Exception:
            pass
        return None

    def replace(self, cid, image, ready_seconds=120, data_check=None, drop_env=()):
        old = self.inspect(cid)
        if not old:
            raise RuntimeError("The container to update no longer exists")
        name = str(old.get("Name") or "").lstrip("/")
        if not name:
            raise RuntimeError("Could not determine the container name")
        was_running = bool((old.get("State") or {}).get("Running"))
        body = recreate_body(old, image, self.image_config(old.get("Image")), drop_env)

        had_data = None
        if data_check and was_running:
            code = self.exec_code(old["Id"], data_check)
            had_data = None if code is None else code == 0
            self.log("data check on current container: " +
                     ("unavailable" if had_data is None else "data present" if had_data else "no data yet"))

        parked = f"{name}-rollback-{int(time.time())}"[:120]
        recovery = f"docker rename {parked} {name} && docker start {name}"
        try:
            self.rename(old["Id"], parked)
        except Exception as e:
            return {"ok": False, "rolled_back": True, "untouched": True,
                    "reason": f"update aborted before any change ({e})"}
        self.log(f"parked original as {parked} (manual recovery if interrupted: {recovery})")

        new_id = ""
        try:
            if was_running:
                self.stop(old["Id"])
                self.log("stopped original")
            new_id = self.create(name, body)
            self.log("created replacement " + new_id[:12])
            if was_running:
                self.start(new_id)
                ok, why = self.wait_ready(new_id, ready_seconds)
                if not ok:
                    raise RuntimeError("replacement " + why)
            now = self.inspect(new_id)
            ok, missing = mounts_match(old, now)
            if not ok:
                raise RuntimeError("replacement is missing mounts: " + ", ".join(missing))
            if data_check and was_running and had_data is True:
                code = self.exec_code(new_id, data_check)
                if code is not None and code != 0:
                    raise RuntimeError("replacement cannot see its database")
        except Exception as e:
            self.log(f"update failed: {e} — restoring the original container")
            gone = self.remove(new_id)
            try:
                if not gone:
                    raise RuntimeError("could not remove the failed replacement")
                self.rename(old["Id"], name)
                if was_running:
                    self.start(old["Id"])
                    ok, why = self.wait_ready(old["Id"], ready_seconds)
                    if not ok:
                        self.log("original restored but not ready yet: " + why)
                self.log("rollback complete: original container restored unchanged")
                return {"ok": False, "rolled_back": True, "reason": str(e)}
            except Exception as re_:
                self.log(f"ROLLBACK FAILED: {re_} — recover with: {recovery}")
                return {"ok": False, "rolled_back": False, "reason": f"{e} · rollback: {re_}", "recovery": recovery}

        removed = self.remove(old["Id"])
        self.log("removed original container" if removed else
                 f"update succeeded but the old container {parked} could not be removed")
        return {"ok": True, "id": new_id,
                "warning": "" if removed else f"Old container {parked} was left behind; you can remove it."}


def main():
    sock = os.environ.get("CRATE_DOCKER_SOCKET", "/var/run/docker.sock")
    self_id = os.environ.get("CRATE_UPDATE_SELF_ID", "")
    target = os.environ.get("CRATE_UPDATE_TARGET_IMAGE", "")
    old_v = os.environ.get("CRATE_UPDATE_OLD_VERSION", "?")
    new_v = os.environ.get("CRATE_UPDATE_TARGET_VERSION", target)
    result = {"from": old_v, "to": new_v, "image": target, "at": time.time()}
    try:
        if not self_id or not target:
            raise RuntimeError("Missing update parameters")
        if not os.path.exists(sock):
            raise RuntimeError("Docker socket is missing")
        log(f"preparing {old_v} -> {new_v}")
        time.sleep(float(os.environ.get("CRATE_UPDATE_DELAY", "1.5")))  # let the API response reach the browser
        r = Replacer(Docker(sock)).replace(
            self_id, target, ready_seconds=int(os.environ.get("CRATE_UPDATE_READY_SECONDS", "120")),
            data_check=["sh", "-c", 'test -f "${CRATE_DB:-/data/crate.db}"'])
        result.update(r)
        if r["ok"]:
            log("update completed successfully" + (f" ({r['warning']})" if r.get("warning") else ""))
        else:
            log(("no changes were made: " if r.get("untouched") else
                 "update was rolled back: " if r.get("rolled_back") else
                 "update failed and could not roll back automatically: ") + r.get("reason", ""))
    except Exception as e:
        result.update(ok=False, rolled_back=True, untouched=True, reason=str(e))
        log(f"fatal: {e}")
    log("RESULT " + json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
