"""Minimal MusicBrainz client (stdlib only, rate-limited to 1 req/sec)."""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

VERSION = "1.4"
# MusicBrainz allows ~1 request/second per client.
MIN_INTERVAL = float(os.environ.get("CRATE_MB_INTERVAL", "1.1"))


WIKIDATA_API = os.environ.get("CRATE_WIKIDATA_URL", "https://www.wikidata.org/w/api.php")
COMMONS_FILEPATH = "https://commons.wikimedia.org/wiki/Special:FilePath/"


def commons_thumb(filename, width=320):
    name = urllib.parse.unquote(filename).replace(" ", "_")
    return f"{COMMONS_FILEPATH}{urllib.parse.quote(name)}?width={width}"


def wikidata_file(qid, prop):
    """Filename of an image property (e.g. P18 photo) on a Wikidata item, or None."""
    url = WIKIDATA_API + "?" + urllib.parse.urlencode(
        {"action": "wbgetclaims", "entity": qid, "property": prop, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": f"Crate/{VERSION} (self-hosted music library)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    for claim in (data.get("claims") or {}).get(prop, []):
        if claim.get("rank") == "deprecated":
            continue
        v = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(v, str) and v:
            return v
    return None


class MusicBrainz:
    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._last = 0.0

    def _get(self, path, params):
        base = self.settings.mb_url.rstrip("/")
        params = dict(params, fmt="json")
        url = f"{base}/{path}?{urllib.parse.urlencode(params)}"
        ua = f"Crate/{VERSION} ( {self.settings.mb_contact} )"
        for attempt in range(5):
            with self._lock:
                wait = MIN_INTERVAL - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and attempt < 4:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 4:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("MusicBrainz request failed")

    # ---------------------------------------------------------------------
    def search_artists(self, query, limit=10):
        data = self._get("artist", {"query": query, "limit": limit})
        out = []
        for a in data.get("artists", []):
            span = a.get("life-span") or {}
            out.append({
                "mbid": a["id"],
                "name": a.get("name"),
                "sort_name": a.get("sort-name"),
                "disambiguation": a.get("disambiguation", ""),
                "country": a.get("country", ""),
                "type": a.get("type", ""),
                "score": a.get("score"),
                "begin": span.get("begin", ""),
                "end": span.get("end", ""),
                "tags": [t["name"] for t in sorted(a.get("tags", []), key=lambda t: -t.get("count", 0))[:3]],
            })
        return out

    def get_artist(self, mbid):
        a = self._get(f"artist/{mbid}", {})
        return {
            "mbid": a["id"],
            "name": a.get("name"),
            "sort_name": a.get("sort-name"),
            "disambiguation": a.get("disambiguation", ""),
            "country": a.get("country", ""),
        }

    def artist_info(self, mbid):
        """Picture + a few notable releases, to help tell same-named artists apart.

        Picture comes from (in order): an 'image' URL relation on MusicBrainz,
        the artist's Wikidata photo (P18) or logo (P154). If none exists the UI
        falls back to album covers from the Cover Art Archive.
        """
        a = self._get(f"artist/{mbid}", {"inc": "url-rels+release-groups"})
        image, source, wikidata = None, None, None
        for rel in a.get("relations", []) or []:
            url = ((rel.get("url") or {}).get("resource") or "")
            if rel.get("type") == "image" and "commons.wikimedia.org/wiki/File:" in url and not image:
                image, source = commons_thumb(url.split("File:", 1)[1]), "Wikimedia Commons"
            elif rel.get("type") == "wikidata":
                wikidata = url.rstrip("/").rsplit("/", 1)[-1]
        if not image and wikidata:
            for prop in ("P18", "P154"):
                try:
                    fn = wikidata_file(wikidata, prop)
                except Exception:
                    fn = None
                if fn:
                    image, source = commons_thumb(fn), "Wikidata"
                    break

        groups = a.get("release-groups", []) or []
        def rank(g):
            pt = (g.get("primary-type") or "")
            sec = g.get("secondary-types") or []
            return (0 if pt == "Album" and not sec else 1 if pt in ("Album", "EP") and not sec else 2,
                    g.get("first-release-date") or "9999")
        groups = sorted(groups, key=rank)
        albums = [{"title": g.get("title", ""), "year": (g.get("first-release-date") or "")[:4],
                   "type": g.get("primary-type") or ""} for g in groups[:4]]
        covers = [g["id"] for g in groups if (g.get("primary-type") in ("Album", "EP", "Single"))][:4]
        return {"image": image, "image_source": source, "albums": albums, "covers": covers,
                "release_count": len(groups),
                # all release titles (up to 25), used to confirm library-import matches
                "titles": [g.get("title", "") for g in groups]}

    @staticmethod
    def _credit(g):
        credit = g.get("artist-credit") or []
        first = next((c.get("artist") for c in credit if isinstance(c, dict) and c.get("artist")), None) or {}
        name = "".join((c.get("name") or (c.get("artist") or {}).get("name", "")) + (c.get("joinphrase") or "")
                       for c in credit if isinstance(c, dict)) or first.get("name", "")
        return first.get("id"), name

    def _group(self, g):
        date = g.get("first-release-date") or ""
        aid, aname = self._credit(g)
        return {"mbid": g["id"], "title": g.get("title", ""), "primary_type": g.get("primary-type") or "Other",
                "secondary_types": list(g.get("secondary-types") or []), "release_date": date,
                "year": date[:4] if date else "", "artist_mbid": aid, "artist_name": aname,
                "disambiguation": g.get("disambiguation", ""), "score": g.get("score")}

    def search_release_groups(self, query, limit=12):
        """Albums/EPs/singles matching free text, e.g. 'ok computer' or 'radiohead kid a'."""
        data = self._get("release-group", {"query": query, "limit": limit})
        return [self._group(g) for g in data.get("release-groups", [])]

    def release_group(self, rg_mbid):
        return self._group(self._get(f"release-group/{rg_mbid}", {"inc": "artist-credits"}))

    def release_groups(self, artist_mbid):
        """All release groups (albums, EPs, singles...) for an artist."""
        out, offset = [], 0
        while True:
            data = self._get("release-group", {
                "artist": artist_mbid,
                "limit": 100,
                "offset": offset,
            })
            groups = data.get("release-groups", [])
            for g in groups:
                date = g.get("first-release-date") or ""
                out.append({
                    "mbid": g["id"],
                    "title": g.get("title", ""),
                    "primary_type": (g.get("primary-type") or "Other"),
                    "secondary_types": [s for s in g.get("secondary-types", [])],
                    "release_date": date,
                    "year": date[:4] if date else "",
                })
            offset += len(groups)
            total = data.get("release-group-count", 0)
            if not groups or offset >= total:
                break
        return out

    def tracklist(self, rg_mbid):
        """Pick a representative official release and return (track_count, [titles]).

        Uses the most common track count across official releases (so a
        deluxe reissue doesn't skew it), and the earliest release with that count.
        """
        data = self._get("release", {
            "release-group": rg_mbid,
            "inc": "media",
            "limit": 100,
        })
        releases = data.get("releases", [])
        official = [r for r in releases if (r.get("status") or "").lower() == "official"] or releases
        if not official:
            return None, []

        def count(r):
            return sum((m.get("track-count") or 0) for m in r.get("media", []))

        counts = Counter(count(r) for r in official if count(r) > 0)
        if not counts:
            return None, []
        best_count = counts.most_common(1)[0][0]
        pick = sorted(
            (r for r in official if count(r) == best_count),
            key=lambda r: (r.get("date") or "9999"),
        )[0]
        rel = self._get(f"release/{pick['id']}", {"inc": "recordings"})
        titles = []
        for m in rel.get("media", []):
            for t in m.get("tracks", []):
                titles.append(t.get("title", ""))
        return (len(titles) or best_count), titles
