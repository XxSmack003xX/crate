"""A small in-memory Docker Engine API on a Unix socket, for testing the updater.

Supports what Crate's updater uses: version, container inspect/list/create/start/
stop/rename/delete/logs, exec, image inspect/pull, network connect. Images whose
tag is in `unhealthy_tags` start but report an unhealthy health check, so the
rollback path can be tested.
"""
import json
import os
import re
import socketserver
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler

API = "1.45"


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.containers = {}   # id -> dict
        self.images = {}       # "repo:tag" -> {"Id", "Config"}
        self.pullable = {}     # "repo:tag" -> config for images that can be pulled
        self.unhealthy_tags = set()
        self.drop_mounts_tags = set()
        self.calls = []
        self.logs = {}         # id -> text

    # ---- helpers
    def find(self, ref):
        ref = urllib.parse.unquote(ref)
        with self.lock:
            if ref in self.containers:
                return self.containers[ref]
            for c in self.containers.values():
                if c["Name"].lstrip("/") == ref.lstrip("/") or c["Id"].startswith(ref):
                    return c
        return None

    def add_image(self, ref, config):
        self.images[ref] = {"Id": "sha256:" + uuid.uuid4().hex, "RepoTags": [ref], "Config": config}

    def add_container(self, name, image, config=None, host_config=None, networks=None, running=True):
        img = self.images[image]
        cid = uuid.uuid4().hex + uuid.uuid4().hex
        cfg = dict(img["Config"])
        cfg.update(config or {})
        cfg["Image"] = image
        c = {"Id": cid, "Name": "/" + name, "Image": img["Id"], "Config": cfg, "HostConfig": host_config or {},
             "NetworkSettings": {"Networks": {n: {"Aliases": [name]} for n in (networks or ["bridge"])}},
             "State": {"Running": running, "Status": "running" if running else "created"}}
        c["Mounts"] = self._mounts(c["HostConfig"])
        self._set_health(c)
        self.containers[cid] = c
        return c

    @staticmethod
    def _mounts(hc):
        out = []
        for b in hc.get("Binds") or []:
            parts = b.split(":")
            src, dst = parts[0], parts[1]
            typ = "bind" if src.startswith("/") else "volume"
            m = {"Type": typ, "Destination": dst, "RW": not (len(parts) > 2 and parts[2] == "ro")}
            m["Source" if typ == "bind" else "Name"] = src
            out.append(m)
        return out

    def _set_health(self, c):
        tag = c["Config"]["Image"].rsplit(":", 1)[-1]
        if not c["State"].get("Running"):
            c["State"].pop("Health", None)
        elif c["Config"].get("Healthcheck"):
            c["State"]["Health"] = {"Status": "unhealthy" if tag in self.unhealthy_tags else "healthy"}


class Handler(BaseHTTPRequestHandler):
    engine = None

    def log_message(self, *a):
        pass

    def address_string(self):
        return "unix"

    def send(self, code, obj=None, raw=None):
        data = raw if raw is not None else (b"" if obj is None else json.dumps(obj).encode())
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def route(self, method):
        e = self.engine
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        p = re.sub(r"^/v[\d.]+", "", u.path)
        e.calls.append((method, p, dict(qs)))
        b = self.body() if method in ("POST", "PUT") else None
        with e.lock:
            if p == "/version":
                return self.send(200, {"ApiVersion": API, "Version": "27.0.0"})
            if p == "/containers/json":
                flt = json.loads(qs.get("filters", ["{}"])[0])
                want = [l.split("=", 1) for l in flt.get("label", [])]
                out = []
                for c in e.containers.values():
                    labels = c["Config"].get("Labels") or {}
                    if all(labels.get(k) == v for k, v in want):
                        out.append({"Id": c["Id"], "Names": [c["Name"]],
                                    "State": "running" if c["State"]["Running"] else "exited"})
                return self.send(200, out)
            if p == "/containers/create" and method == "POST":
                name = qs["name"][0]
                if any(c["Name"] == "/" + name for c in e.containers.values()):
                    return self.send(409, {"message": f"Conflict. The container name \"/{name}\" is already in use"})
                image = b["Image"]
                if image not in e.images:
                    return self.send(404, {"message": f"No such image: {image}"})
                img = e.images[image]
                cfg = dict(img["Config"])
                for k, v in b.items():
                    if k not in ("HostConfig", "NetworkingConfig", "Image"):
                        cfg[k] = v
                cfg["Image"] = image
                hc = dict(b.get("HostConfig") or {})
                if image.rsplit(":", 1)[-1] in e.drop_mounts_tags:
                    hc["Binds"] = (hc.get("Binds") or [])[:1]
                nets = list(((b.get("NetworkingConfig") or {}).get("EndpointsConfig") or {"bridge": {}}).keys())
                if len(nets) > 1:
                    return self.send(400, {"message": "Container cannot be connected to network endpoints"})
                cid = uuid.uuid4().hex + uuid.uuid4().hex
                c = {"Id": cid, "Name": "/" + name, "Image": img["Id"], "Config": cfg, "HostConfig": hc,
                     "NetworkSettings": {"Networks": {n: {} for n in nets}},
                     "State": {"Running": False, "Status": "created"}, "CreateBody": b}
                c["Mounts"] = Engine._mounts(hc)
                e.containers[cid] = c
                return self.send(201, {"Id": cid, "Warnings": []})
            m = re.match(r"^/containers/([^/]+)/(json|start|stop|rename|exec|logs)$", p)
            if m:
                c = e.find(m.group(1))
                if not c:
                    return self.send(404, {"message": "No such container"})
                act = m.group(2)
                if act == "json":
                    return self.send(200, c)
                if act == "start":
                    c["State"] = {"Running": True, "Status": "running"}
                    e._set_health(c)
                    return self.send(204)
                if act == "stop":
                    if not c["State"]["Running"]:
                        return self.send(304)
                    c["State"] = {"Running": False, "Status": "exited", "ExitCode": 0}
                    return self.send(204)
                if act == "rename":
                    c["Name"] = "/" + qs["name"][0]
                    return self.send(204)
                if act == "exec":
                    return self.send(201, {"Id": uuid.uuid4().hex})
                if act == "logs":
                    text = e.logs.get(c["Id"], "")
                    raw = b""
                    for line in text.splitlines(True):
                        data = line.encode()
                        raw += b"\x01\x00\x00\x00" + len(data).to_bytes(4, "big") + data
                    return self.send(200, raw=raw)
            m = re.match(r"^/exec/([^/]+)/(start|json)$", p)
            if m:
                if m.group(2) == "start":
                    return self.send(200, raw=b"")
                return self.send(200, {"Running": False, "ExitCode": 0})
            m = re.match(r"^/containers/([^/]+)$", p)
            if m and method == "DELETE":
                c = e.find(m.group(1))
                if not c:
                    return self.send(404, {"message": "No such container"})
                del e.containers[c["Id"]]
                return self.send(204)
            m = re.match(r"^/images/(.+)/json$", p)
            if m:
                ref = urllib.parse.unquote(m.group(1))
                img = e.images.get(ref) or next((i for i in e.images.values() if i["Id"] == ref), None)
                return self.send(200, img) if img else self.send(404, {"message": "No such image"})
            if p == "/images/create" and method == "POST":
                ref = qs["fromImage"][0] + ":" + qs["tag"][0]
                if ref in e.pullable:
                    e.add_image(ref, e.pullable[ref])
                    return self.send(200, raw=b'{"status":"Pulling"}\n{"status":"Downloaded newer image"}\n')
                return self.send(404, {"message": f"manifest unknown: {ref} not found"})
            m = re.match(r"^/networks/([^/]+)/connect$", p)
            if m:
                c = e.find(b["Container"])
                c["NetworkSettings"]["Networks"][urllib.parse.unquote(m.group(1))] = b.get("EndpointConfig") or {}
                return self.send(200)
        return self.send(404, {"message": f"fake engine: no route {method} {p}"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def start(path):
    if os.path.exists(path):
        os.remove(path)
    engine = Engine()
    handler = type("H", (Handler,), {"engine": engine})
    srv = _Server(path, handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, engine
