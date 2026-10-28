"""Artist recommendations from the artists you follow.

Similar artists come from ListenBrainz (no account needed; based on what people
listen to together, and keyed by MusicBrainz ids like everything else in Crate).
If you add a Last.fm API key in Settings, Last.fm's similar artists are blended in
too, which helps for smaller artists ListenBrainz knows little about.

Each followed artist's similar list is fetched once and cached for two weeks; a
background loop fills the cache a little at a time so a big library never floods
either service. Recommendations are then scored across all your artists: an artist
similar to several of yours ranks above one that's very similar to only one.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

LB_ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
INTERVAL = float(os.environ.get("CRATE_RECS_INTERVAL", "1.5"))  # seconds between fetches
TTL_DAYS = 14
UA = "Crate/1.5 (self-hosted music library; https://github.com/XxSmack003xX/crate)"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _get(url, params):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def listenbrainz_similar(mbid):
    """[{mbid, name, type, score}] — most similar first."""
    lb = os.environ.get("CRATE_LISTENBRAINZ_URL", "https://labs.api.listenbrainz.org")
    data = _get(lb.rstrip("/") + "/similar-artists/json", {"artist_mbids": mbid, "algorithm": LB_ALGORITHM})
    rows = []

    def walk(x):  # the labs API has returned both flat and nested lists over time
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            if x.get("artist_mbid") and x.get("artist_mbid") != mbid:
                rows.append(x)
            elif isinstance(x.get("data"), list):
                walk(x["data"])
    walk(data)
    out = [{"mbid": r["artist_mbid"], "name": r.get("name") or "", "type": r.get("type") or "",
            "score": float(r.get("score") or 0)} for r in rows]
    out.sort(key=lambda r: -r["score"])
    return out


def lastfm_similar(mbid, name, key):
    """[{mbid, name, score}] from Last.fm (only artists Last.fm knows the MBID of)."""
    params = {"method": "artist.getsimilar", "api_key": key, "format": "json", "limit": 60, "autocorrect": 1}
    params.update({"mbid": mbid} if mbid else {"artist": name})
    url = os.environ.get("CRATE_LASTFM_URL", "https://ws.audioscrobbler.com/2.0/")
    data = _get(url, params)
    if "error" in data and name and "mbid" in params:  # Last.fm doesn't know this MBID: try the name
        params.pop("mbid")
        params["artist"] = name
        data = _get(url, params)
    if "error" in data:
        raise RuntimeError(f"Last.fm: {data.get('message') or data['error']}")
    arts = ((data.get("similarartists") or {}).get("artist")) or []
    return [{"mbid": a["mbid"], "name": a.get("name") or "", "type": "", "score": float(a.get("match") or 0)}
            for a in arts if a.get("mbid")]


class Recommender:
    def __init__(self, app):
        self.app, self.db = app, app.db
        self.wake = threading.Event()
        self._lock = threading.Lock()
        self._next = 0.0
        self.last_error = None
        self._cache = {"key": None, "items": None}

    # ------------------------------------------------------------ fetching
    def _fetch(self, artist):
        lists, errors = [], []
        try:
            lists.append(listenbrainz_similar(artist["mbid"]))
        except Exception as e:
            errors.append(f"ListenBrainz: {e}")
        key = self.app.settings.raw().get("lastfm_api_key")
        if key:
            try:
                lists.append(lastfm_similar(artist["mbid"], artist["name"], key))
            except Exception as e:
                errors.append(str(e))
        if errors and not any(lists):
            raise RuntimeError("; ".join(errors))
        # Blend: rank-based weights so the two services' different score scales mix fairly.
        merged = {}
        for lst in lists:
            for rank, r in enumerate(lst[:100]):
                w = 1.0 / (1 + rank * 0.08)
                m = merged.setdefault(r["mbid"], {"mbid": r["mbid"], "name": r["name"], "type": r["type"], "w": 0.0})
                m["w"] = max(m["w"], w) + (0.15 * min(m["w"], w))
                if not m["type"] and r["type"]:
                    m["type"] = r["type"]
        return sorted(merged.values(), key=lambda r: -r["w"])[:100]

    def refresh_one(self, force_id=None):
        """Fetch similar artists for one followed artist that has none cached (or stale ones)."""
        if time.monotonic() < self._next or not self.app.settings.bool("recommendations"):
            return False
        cutoff = (_now() - timedelta(days=TTL_DAYS)).isoformat()
        if force_id:
            artist = self.db.one("SELECT id, mbid, name FROM artists WHERE id = ?", (force_id,))
        else:
            artist = self.db.one(
                "SELECT ar.id, ar.mbid, ar.name FROM artists ar LEFT JOIN similar s ON s.artist_mbid = ar.mbid "
                "WHERE COALESCE(ar.followed, 1) = 1 AND (s.fetched_at IS NULL OR s.fetched_at < ?) "
                "ORDER BY s.fetched_at IS NOT NULL, ar.added_at DESC LIMIT 1", (cutoff,))
        if not artist:
            return False
        with self._lock:
            self._next = time.monotonic() + INTERVAL
            try:
                items = self._fetch(artist)
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
                # Back off; mark it fetched-with-nothing for a day so one bad artist can't stall the rest.
                self._next = time.monotonic() + 60
                items = None
            fetched = _now().isoformat() if items is not None else \
                (_now() - timedelta(days=TTL_DAYS - 1)).isoformat()
            self.db.x("INSERT INTO similar (artist_mbid, data, fetched_at) VALUES (?,?,?) "
                      "ON CONFLICT(artist_mbid) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
                      (artist["mbid"], json.dumps(items or []), fetched))
        return True

    def loop(self, stop):
        """Background: keep every followed artist's similar list fresh, one at a time."""
        while not stop.is_set():
            try:
                did = self.refresh_one()
            except Exception as e:  # never let this thread die
                self.last_error, did = str(e), False
            wait = max(INTERVAL, self._next - time.monotonic()) if did else 30
            self.wake.wait(wait)
            self.wake.clear()

    # ------------------------------------------------------------ reading
    def similar_to(self, mbid, limit=12):
        row = self.db.one("SELECT data FROM similar WHERE artist_mbid = ?", (mbid,))
        items = json.loads(row["data"]) if row and row["data"] else []
        known = self._known()
        out = []
        for r in items:
            k = known.get(r["mbid"])
            out.append({"mbid": r["mbid"], "name": r["name"], "type": r.get("type") or "",
                        "artist_id": k["id"] if k else None, "followed": bool(k and k["followed"])})
            if len(out) >= limit:
                break
        return {"items": out, "fetched": row is not None}

    def _known(self):
        return {r["mbid"]: r for r in self.db.q("SELECT id, mbid, COALESCE(followed, 1) AS followed FROM artists")}

    def recommendations(self, limit=200):
        key = self.db.version("artists", "similar", "rec_dismissed")
        if self._cache["key"] == key:
            return self._cache["items"]
        seeds = {r["mbid"]: r["name"] for r in self.db.q(
            "SELECT mbid, name FROM artists WHERE COALESCE(followed, 1) = 1")}
        known = self._known()
        dismissed = {r["mbid"] for r in self.db.q("SELECT mbid FROM rec_dismissed")}
        agg = {}
        for row in self.db.q("SELECT artist_mbid, data FROM similar"):
            seed = row["artist_mbid"]
            if seed not in seeds:
                continue
            for r in json.loads(row["data"] or "[]"):
                m = r["mbid"]
                k = known.get(m)
                if m in seeds or m in dismissed or (k and k["followed"]):
                    continue
                a = agg.setdefault(m, {"mbid": m, "name": r["name"], "type": r.get("type") or "", "score": 0.0,
                                       "because": [], "artist_id": k["id"] if k else None})
                a["score"] += r.get("w", 0)
                a["because"].append((r.get("w", 0), seeds[seed]))
        items = sorted(agg.values(), key=lambda a: (-(a["score"] * (1 + 0.25 * (len(a["because"]) - 1))), a["name"]))
        for a in items:
            a["because"] = [n for _, n in sorted(a["because"], reverse=True)[:3]]
            a["seeds"] = len(a["because"])
            a["score"] = round(a["score"], 3)
        items = items[:limit]
        self._cache.update(key=key, items=items)
        return items

    def progress(self):
        cutoff = (_now() - timedelta(days=TTL_DAYS)).isoformat()
        total = self.db.one("SELECT COUNT(*) n FROM artists WHERE COALESCE(followed, 1) = 1")["n"]
        done = self.db.one(
            "SELECT COUNT(*) n FROM artists ar JOIN similar s ON s.artist_mbid = ar.mbid "
            "WHERE COALESCE(ar.followed, 1) = 1 AND s.fetched_at >= ?", (cutoff,))["n"]
        return {"checked": done, "total": total, "error": self.last_error}

    def dismiss(self, mbid, username=None):
        self.db.x("INSERT INTO rec_dismissed (mbid, username, dismissed_at) VALUES (?,?,?) "
                  "ON CONFLICT(mbid) DO NOTHING", (mbid, username, _now().isoformat()))

    def undismiss_all(self):
        self.db.x("DELETE FROM rec_dismissed")
