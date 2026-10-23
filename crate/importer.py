"""Import artists from the music already in your library.

1. Scan the library folder and collect artists (from folder names, or tags when
   mutagen is installed) together with their local album titles.
2. Match each artist to MusicBrainz. When several artists share a name, the one
   whose releases overlap most with your local albums wins.
3. You review the matches and add the ones you want. Imported artists don't
   auto-download by default: albums you don't have are marked "missing" and
   wait for you to click Download.
"""
import os
import threading
from collections import Counter

from .db import now_iso
from .library import _tags, guess_artist_album
from .matcher import AUDIO_EXT, DISC_RE, normalize, strip_extras

SKIP_ARTISTS = {"various artists", "various", "va", "v a", "unknown artist", "unknown", "soundtrack",
                "soundtracks", "compilations", "compilation", "misc", "singles", "downloads", "music"}


def _key(name):
    n = normalize(name)
    return n[4:] if n.startswith("the ") else n


class LibraryImporter:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.state = "idle"     # idle | scanning | matching | done | error
        self.error = None
        self.folders = 0
        self.total = 0
        self.matched = 0
        self.finished_at = None
        self.results = {}       # id -> result
        self.adding = False

    # ------------------------------------------------------------------ scan
    def start(self):
        with self.lock:
            if self.state in ("scanning", "matching"):
                return False
            self.state, self.error = "scanning", None
            self.folders = self.total = self.matched = 0
            self.results = {}
        threading.Thread(target=self._run, daemon=True, name="library-import").start()
        return True

    def _run(self):
        try:
            artists = self._scan()
            followed = {}
            for r in self.app.db.q("SELECT id, name, local_name FROM artists"):
                followed[_key(r["name"])] = r["id"]
                if r["local_name"]:
                    followed[_key(r["local_name"])] = r["id"]
            todo = sorted(artists.values(), key=lambda a: (-len(a["albums"]), a["name"].lower()))
            with self.lock:
                self.state = "matching"
                self.total = len(todo)
            for i, a in enumerate(todo, 1):
                rid = f"i{i}"
                base = {
                    "id": rid, "local_name": a["name"], "album_count": len(a["albums"]),
                    "local_albums": sorted(a["albums"])[:12], "folders": a["folders"][:3],
                }
                if a["key"] in followed:
                    res = dict(base, status="following", artist_id=followed[a["key"]], match=None,
                               alternatives=[], selected=False)
                else:
                    try:
                        res = dict(base, **self._match(a["name"], a["albums"]))
                    except Exception as e:
                        res = dict(base, status="error", error=str(e), match=None, alternatives=[], selected=False)
                with self.lock:
                    self.results[rid] = res
                    self.matched = i
            with self.lock:
                self.state, self.finished_at = "done", now_iso()
            n_new = sum(1 for r in self.results.values() if r["status"] not in ("following",))
            self.app.db.event(f"Library import scan: {self.total} artist(s) in your library, "
                              f"{n_new} not followed yet")
        except Exception as e:
            with self.lock:
                self.state, self.error = "error", str(e)
            self.app.db.event(f"Library import scan failed: {e}", "error")

    def _scan(self):
        root = os.path.realpath(os.path.expanduser(self.app.settings.library_dir))
        if not os.path.isdir(root):
            raise RuntimeError(f"Library folder not found: {root}")
        units = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            audio = sorted(f for f in filenames if f.rsplit(".", 1)[-1].lower() in AUDIO_EXT)
            if not audio or dirpath == root:
                continue
            parent = os.path.dirname(dirpath)
            unit = parent if DISC_RE.match(os.path.basename(dirpath)) and parent != root else dirpath
            units.setdefault(unit, os.path.join(dirpath, audio[0]))
            self.folders += 1

        artists = {}
        for unit, first in sorted(units.items()):
            parts = os.path.relpath(unit, root).split(os.sep)
            artist, album = guess_artist_album(parts)
            ta, tb = _tags(first)
            if ta:
                artist = ta
            if tb:
                album = tb
            key = _key(artist)
            if not key or key in SKIP_ARTISTS or not normalize(album):
                continue
            a = artists.setdefault(key, {"key": key, "names": Counter(), "albums": set(), "folders": []})
            a["names"][artist.strip()] += 1
            a["albums"].add(strip_extras(album).strip() or album)
            a["folders"].append(os.path.relpath(unit, root))
        for a in artists.values():
            a["name"] = a["names"].most_common(1)[0][0]
        return artists

    def _match(self, name, albums):
        results = self.app.mb.search_artists(name, limit=8)
        key = _key(name)
        exact = [r for r in results if _key(r["name"]) == key or _key(r.get("sort_name") or "") == key]
        cands = exact or [r for r in results if (r.get("score") or 0) >= 85][:3]
        alternatives = [self._alt(r) for r in results[:6]]
        if not cands:
            return {"status": "none", "confidence": "none", "match": None,
                    "alternatives": alternatives, "selected": False}

        local = {normalize(strip_extras(t)) for t in albums}
        scored = []
        for r in cands[:3]:
            try:
                info = self.app.artist_info(r["mbid"])
            except Exception:
                info = {}
            titles = {normalize(strip_extras(t)) for t in info.get("titles", [])}
            overlap = len(local & titles)
            scored.append((overlap, r.get("score") or 0, r, info))
        scored.sort(key=lambda s: (-s[0], -s[1]))
        overlap, _, best, info = scored[0]
        if overlap:
            confidence = "albums"            # your albums found in this artist's releases
        elif exact and len(exact) == 1:
            confidence = "name"              # one artist with exactly this name
        else:
            confidence = "unsure"            # same-name artists, or only a fuzzy match
        match = dict(self._alt(best), image=info.get("image"), covers=info.get("covers", []),
                     albums=info.get("albums", []), overlap=overlap)
        # Put the chosen one first in the alternatives list.
        alternatives = [match] + [a for a in alternatives if a["mbid"] != best["mbid"]]
        return {"status": "matched", "confidence": confidence, "match": match,
                "alternatives": alternatives, "selected": confidence in ("albums", "name")}

    @staticmethod
    def _alt(r):
        return {"mbid": r["mbid"], "name": r["name"], "sort_name": r.get("sort_name"),
                "disambiguation": r.get("disambiguation", ""), "country": r.get("country", ""),
                "type": r.get("type", ""), "begin": r.get("begin", ""), "score": r.get("score")}

    # ------------------------------------------------------------------ output
    def snapshot(self):
        with self.lock:
            results = sorted(self.results.values(), key=lambda r: int(r["id"][1:]))
            counts = Counter(r["status"] for r in results)
            return {"state": self.state, "error": self.error, "folders": self.folders,
                    "total": self.total, "matched": self.matched, "finished_at": self.finished_at,
                    "adding": self.adding, "counts": dict(counts), "results": results}

    # ------------------------------------------------------------------ add
    def add(self, items, auto_download=False):
        """items: [{"id": result id, "mbid": chosen MusicBrainz id}]"""
        added, skipped, ids = [], 0, []
        with self.lock:
            for it in items or []:
                res = self.results.get(it.get("id"))
                mbid = it.get("mbid")
                if not res or not mbid or res["status"] in ("following", "added"):
                    skipped += 1
                    continue
                choice = next((a for a in res["alternatives"] if a["mbid"] == mbid), None)
                existing = self.app.db.one("SELECT id FROM artists WHERE mbid = ?", (mbid,))
                if existing:
                    res.update(status="following", artist_id=existing["id"], selected=False)
                    if res["local_name"]:
                        self.app.db.x("UPDATE artists SET local_name = COALESCE(local_name, ?) WHERE id = ?",
                                      (res["local_name"], existing["id"]))
                    skipped += 1
                    continue
                if not choice:
                    skipped += 1
                    continue
                aid = self.app.db.x(
                    "INSERT INTO artists (mbid, name, sort_name, disambiguation, country, added_at, sync_status, "
                    "local_name, auto_download) VALUES (?,?,?,?,?,?, 'pending', ?, ?)",
                    (mbid, choice["name"], choice.get("sort_name"), choice.get("disambiguation"),
                     choice.get("country"), now_iso(), res["local_name"], 1 if auto_download else 0))
                res.update(status="added", artist_id=aid, selected=False)
                added.append(choice["name"])
                ids.append(aid)
        if ids:
            self.app.db.event(
                f"Imported {len(ids)} artist(s) from your library"
                + ("" if auto_download else "; missing albums will wait for you to click Download"))
            threading.Thread(target=self._sync_all, args=(ids,), daemon=True, name="import-sync").start()
        return {"added": len(ids), "skipped": skipped, "names": added}

    def _sync_all(self, ids):
        with self.lock:
            self.adding = True
        try:
            for aid in ids:
                self.app.sync_artist(aid)
        finally:
            with self.lock:
                self.adding = False
