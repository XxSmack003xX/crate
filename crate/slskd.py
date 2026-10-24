"""Thin client for the slskd REST API (v0)."""
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid


class SlskdError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# slskd's /server endpoint is Administrator-only; ReadWrite/ReadOnly keys get 403
# there but can read the same login state from /application. Remember per URL.
_USE_APPLICATION_STATE = set()


class Slskd:
    def __init__(self, url, api_key, timeout=30):
        self.base = url.rstrip("/") + "/api/v0"
        self.api_key = api_key
        self.timeout = timeout

    def _req(self, method, path, body=None, params=None, allow_404=False):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"Accept": "application/json", "X-API-Key": self.api_key}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if allow_404 and e.code == 404:
                return None
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code == 401:
                raise SlskdError("slskd rejected the API key (401): it's wrong, or not allowed from this "
                                 "IP address (the key's cidr setting). Check the key in Settings.", 401) from e
            if e.code == 403:
                raise SlskdError(f"slskd refused {method} {path} (403): the API key's role isn't allowed to do "
                                 "this. Give the key the ReadWrite or Administrator role in slskd.", 403) from e
            raise SlskdError(f"slskd {method} {path} -> HTTP {e.code} {detail}", e.code) from e
        except urllib.error.URLError as e:
            raise SlskdError(f"Can't reach slskd at {self.base}: {e.reason}") from e

    @staticmethod
    def _u(username):
        return urllib.parse.quote(username, safe="")

    # ---- status ----------------------------------------------------------
    def server_state(self):
        """Soulseek connection state: {state, isConnected, isLoggedIn, username?}."""
        if self.base not in _USE_APPLICATION_STATE:
            try:
                return self._req("GET", "/server") or {}
            except SlskdError as e:
                if e.status != 403:
                    raise
                _USE_APPLICATION_STATE.add(self.base)
        app = self._req("GET", "/application") or {}
        server = dict(app.get("server") or {})
        user = app.get("user") or {}
        if user.get("username") and not server.get("username"):
            server["username"] = user["username"]
        return server

    # ---- searches --------------------------------------------------------
    def start_search(self, text, timeout_s=45):
        sid = str(uuid.uuid4())
        body = {
            "id": sid,
            "searchText": text,
            "searchTimeout": int(timeout_s) * 1000,
            "responseLimit": 250,
            "fileLimit": 20000,
            "filterResponses": True,
            "minimumResponseFileCount": 1,
        }
        try:
            self._req("POST", "/searches", body)
        except SlskdError:
            # slskd may have recorded the failed search; don't leave it behind.
            self.delete_search(sid)
            raise
        return sid

    def list_searches(self):
        data = self._req("GET", "/searches") or []
        return data if isinstance(data, list) else []

    def search_state(self, sid):
        return self._req("GET", f"/searches/{sid}", allow_404=True)

    def search_responses(self, sid):
        return self._req("GET", f"/searches/{sid}/responses", allow_404=True) or []

    def delete_search(self, sid):
        try:
            self._req("DELETE", f"/searches/{sid}", allow_404=True)
        except SlskdError:
            pass

    # ---- transfers -------------------------------------------------------
    def enqueue(self, username, files):
        """files: [{"filename": <remote path>, "size": <bytes>}]"""
        return self._req("POST", f"/transfers/downloads/{self._u(username)}", files)

    def user_downloads(self, username):
        """Flat list of this user's download transfers."""
        data = self._req("GET", f"/transfers/downloads/{self._u(username)}", allow_404=True)
        out = []
        if not data:
            return out
        users = data if isinstance(data, list) else [data]
        for u in users:
            for d in u.get("directories", []) or []:
                for f in d.get("files", []) or []:
                    out.append(f)
        return out

    def cancel_download(self, username, transfer_id, remove=True):
        try:
            self._req(
                "DELETE",
                f"/transfers/downloads/{self._u(username)}/{transfer_id}",
                params={"remove": "true" if remove else "false"},
                allow_404=True,
            )
        except SlskdError:
            pass
