"""Minimal Docker Engine API client over the Unix socket (stdlib only)."""
import http.client
import json
import socket


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


class DockerError(Exception):
    pass


class Docker:
    def __init__(self, socket_path="/var/run/docker.sock"):
        self.socket_path = socket_path
        self._prefix = None

    def raw(self, method, path, body=None, timeout=30):
        """Returns (status, parsed_json_or_None, raw_bytes). Never raises on HTTP errors."""
        conn = _UnixConnection(self.socket_path, timeout)
        headers = {"Host": "localhost", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            raw = r.read()
            status = r.status
        finally:
            conn.close()
        parsed = None
        text = raw.decode("utf-8", "replace").strip()
        if text:
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        return status, parsed, raw

    def call(self, method, path, body=None, timeout=30):
        if self._prefix is None:
            st, data, _ = self.raw("GET", "/version", timeout=15)
            if st != 200 or not isinstance(data, dict) or not data.get("ApiVersion"):
                raise DockerError("Docker API unavailable")
            self._prefix = "/v" + data["ApiVersion"]
        return self.raw(method, self._prefix + path, body, timeout)


def demux_logs(raw):
    """Docker log stream (non-TTY) -> text. Frames: 1 byte stream, 3 pad, 4 size, payload."""
    out, i = [], 0
    if raw[:1] in (b"\x00", b"\x01", b"\x02") and len(raw) >= 8:
        while i + 8 <= len(raw):
            size = int.from_bytes(raw[i + 4:i + 8], "big")
            out.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        return b"".join(out).decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")
