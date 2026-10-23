"""Find and clean up duplicate albums in the music library.

Two kinds of duplicates:
  * "copies" — the same album (same artist + title) in two or more folders,
    e.g. "First Light [FLAC]" and "First Light (2001)". Resolve by keeping one.
  * "mixed"  — one folder holding the album in two formats (FLAC *and* MP3
    copies of the tracks). Resolve by removing one format's files.

Removed files go to <library>/.crate-trash/<timestamp>/ (or are deleted, if the
"dupes_action" setting is "delete"). Nothing outside the library is touched.
"""
import os
import re
import shutil
import threading
from datetime import datetime

from .db import now_iso
from .library import _tags, guess_artist_album
from .matcher import AUDIO_EXT, DISC_RE, IMAGE_EXT, LOSSLESS, normalize, strip_extras

try:
    import mutagen  # type: ignore
except Exception:  # pragma: no cover
    mutagen = None

TRASH_DIR = ".crate-trash"

# "01 - Song", "1-02 Song", "01. Song", "01_Song", "Disc 1 - 03 Song"
TRACKNO_RE = re.compile(r"^\s*(?:(?:cd|disc|disk)\s*\d+\s*[-_. ]\s*)?\d{1,3}(?:[-_.]\d{1,3})?(?:\s*[-_.)]\s*|\s+)", re.I)
# "Song (1)", "Song copy", "Song - Copy (2)"
COPY_RE = re.compile(r"(?:\s*[-_]?\s*(?:\(\d{1,2}\)|\[\d{1,2}\]|copy(?:\s*\d+)?|\(copy(?:\s*\d+)?\)))+\s*$", re.I)
# "(Remastered 2011)", "[2009 Remaster]", "- Remastered" — same recording
REMASTER_RE = re.compile(r"\s*(?:[\(\[][^\)\]]*remaster[^\)\]]*[\)\]]|-\s*(?:\d{4}\s*)?remaster(?:ed)?(?:\s*\d{4})?(?:\s*version)?)", re.I)
# Titles that many different songs share (an "Intro" on every album isn't a duplicate).
GENERIC_RE = re.compile(r"^(?:intro|outro|interlude|untitled|skit|hidden track|bonus track|silence|prelude|"
                        r"reprise|instrumental|demo|track|audio track|piste|pista)(?: \d{1,3})?$")
VARIOUS = {"various artists", "various", "va", "v a", "unknown artist", "unknown", "compilations", "soundtrack"}


class DupeError(Exception):
    pass


def _ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _bitrate(path):
    if mutagen is None:
        return None
    try:
        f = mutagen.File(path)
        br = getattr(getattr(f, "info", None), "bitrate", 0)
        return int(br // 1000) if br else None
    except Exception:
        return None


def _song_info(path):
    """(artist, title, length_seconds, bitrate_kbps) from tags; any may be None."""
    if mutagen is None:
        return None, None, None, None
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        return None, None, None, None
    if f is None:
        return None, None, None, None
    info = getattr(f, "info", None)
    length = getattr(info, "length", None)
    br = getattr(info, "bitrate", 0)
    tags = f.tags or {}
    try:
        artist = (tags.get("artist") or tags.get("albumartist") or [None])[0]
        title = (tags.get("title") or [None])[0]
    except Exception:
        artist = title = None
    return artist, title, (round(length) if length else None), (int(br // 1000) if br else None)


def song_from_filename(filename, folder_artist=""):
    """Parse '03 - Artist - Song (1).mp3' into (artist or None, title, copy_marked)."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = stem.replace("_", " ").strip()
    marked = bool(COPY_RE.search(stem))
    stem = COPY_RE.sub("", stem).strip()
    stem = TRACKNO_RE.sub("", stem, count=1).strip() or stem
    artist = None
    if " - " in stem:
        head, tail = stem.split(" - ", 1)
        fa = normalize(folder_artist)
        if fa and normalize(head) in (fa, fa[4:] if fa.startswith("the ") else fa):
            stem = tail
        elif not fa or fa in VARIOUS:
            artist, stem = head, tail
    return artist, stem.strip(), marked


def song_key(title):
    return normalize(COPY_RE.sub("", REMASTER_RE.sub("", title or "")))


def _dir_size(path):
    total = 0
    for dp, _, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total


class DupeScanner:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.state = "idle"          # idle | scanning | done | error
        self.error = None
        self.folders_scanned = 0
        self.scanned_at = None
        self.groups = {}             # gid -> group
        self.copies = {}             # cid -> copy (with private file lists)
        self.song_groups = {}        # sgid -> group of duplicate songs
        self.songs = {}              # sid -> one song file
        self.files_scanned = 0

    # ------------------------------------------------------------------ paths
    def root(self):
        return os.path.realpath(os.path.expanduser(self.app.settings.library_dir))

    def trash_root(self):
        return os.path.join(self.root(), TRASH_DIR)

    def _inside(self, path):
        root = self.root()
        real = os.path.realpath(path)
        return real != root and real.startswith(root + os.sep) and \
            not real.startswith(self.trash_root() + os.sep)

    # ------------------------------------------------------------------- scan
    def start_scan(self):
        with self.lock:
            if self.state == "scanning":
                return False
            self.state, self.error, self.folders_scanned, self.files_scanned = "scanning", None, 0, 0
        threading.Thread(target=self._scan_safe, daemon=True, name="dupe-scan").start()
        return True

    def _scan_safe(self):
        try:
            self._scan()
            with self.lock:
                self.state, self.scanned_at = "done", now_iso()
            n, ns = len(self.groups), len(self.song_groups)
            self.app.db.event(f"Duplicate scan: {n} duplicate album(s) and {ns} duplicate song(s) found"
                              if n or ns else "Duplicate scan: no duplicates found")
        except Exception as e:  # pragma: no cover
            with self.lock:
                self.state, self.error = "error", str(e)
            self.app.db.event(f"Duplicate scan failed: {e}", "error")

    def _scan(self):
        root = self.root()
        if not os.path.isdir(root):
            raise DupeError(f"Library folder not found: {root}")
        prefs = self.app.settings.list("preferred_formats") or ["flac", "mp3"]

        units = {}  # album folder -> list of (path, ext, size)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            audio = [f for f in filenames if _ext(f) in AUDIO_EXT]
            if not audio or dirpath == root:
                continue
            self.folders_scanned += 1
            parent = os.path.dirname(dirpath)
            unit = parent if DISC_RE.match(os.path.basename(dirpath)) and parent != root else dirpath
            lst = units.setdefault(unit, [])
            for f in sorted(audio):
                p = os.path.join(dirpath, f)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                lst.append((p, _ext(f), size))

        unit_paths = sorted(units)
        copies, by_key = {}, {}
        for i, unit in enumerate(unit_paths):
            files = units[unit]
            if not files:
                continue
            parts = os.path.relpath(unit, root).split(os.sep)
            artist, album = guess_artist_album(parts)
            ta, tb = _tags(files[0][0])
            if tb:
                album = tb
                artist = ta or artist
            na = normalize(artist)
            na = na[4:] if na.startswith("the ") else na
            nt = normalize(strip_extras(album))
            if not na or not nt:
                continue

            formats = {}
            for p, ext, size in files:
                fm = formats.setdefault(ext, {"count": 0, "size": 0, "files": []})
                fm["count"] += 1
                fm["size"] += size
                fm["files"].append(p)
            for ext, fm in formats.items():
                fm["lossless"] = ext in LOSSLESS
                fm["bitrate"] = None if fm["lossless"] else _bitrate(fm["files"][0])

            def fmt_rank(ext):
                return prefs.index(ext) if ext in prefs else len(prefs) + (0 if ext in LOSSLESS else 1)

            top = max(fm["count"] for fm in formats.values())
            main = sorted(formats, key=lambda e: (-formats[e]["count"], fmt_rank(e)))[0]
            # A folder is "mixed" when 2+ formats each hold a real share of the tracks.
            real = [e for e, fm in formats.items() if fm["count"] >= max(1, top * 0.5)]
            nested = any(u.startswith(unit + os.sep) and not DISC_RE.match(os.path.basename(u))
                         for u in unit_paths[i + 1:i + 50])
            cid = f"c{len(copies) + 1}"
            copies[cid] = {
                "id": cid, "path": unit, "rel": os.path.relpath(unit, root),
                "artist": artist, "album": album, "formats": formats, "main": main,
                "mixed_formats": sorted(real, key=fmt_rank) if len(real) >= 2 else [],
                "tracks": formats[main]["count"], "size": sum(fm["size"] for fm in formats.values()),
                "nested": nested, "rank": fmt_rank(main),
                "discs": len({os.path.dirname(p) for p, _, _ in files}),
            }
            by_key.setdefault((na, nt), []).append(cid)

        groups = {}
        crate_paths = {os.path.realpath(r["path"]): r["id"] for r in self.app.db.q(
            "SELECT id, path FROM albums WHERE path IS NOT NULL AND status = 'have'")}
        for key, cids in sorted(by_key.items()):
            if len(cids) >= 2:
                gid = f"g{len(groups) + 1}"
                groups[gid] = {"id": gid, "type": "copies", "copies": cids,
                               "artist": copies[cids[0]]["artist"], "album": copies[cids[0]]["album"]}
            for cid in cids:
                if copies[cid]["mixed_formats"]:
                    gid = f"g{len(groups) + 1}"
                    groups[gid] = {"id": gid, "type": "mixed", "copies": [cid],
                                   "artist": copies[cid]["artist"], "album": copies[cid]["album"]}
        for c in copies.values():
            c["crate_album"] = crate_paths.get(os.path.realpath(c["path"]))
        for g in groups.values():
            self._suggest(g, copies)

        songs, song_groups = self._scan_songs(root, units, copies, groups, prefs)
        with self.lock:
            self.copies = copies
            self.groups = groups
            self.songs = songs
            self.song_groups = song_groups

    # ------------------------------------------------------------------ songs
    def _scan_songs(self, root, units, copies, groups, prefs):
        """Group individual tracks that are the same song (artist + title, similar length)."""
        def fmt_rank(ext):
            return prefs.index(ext) if ext in prefs else len(prefs) + (0 if ext in LOSSLESS else 1)

        # Which album-level group (if any) each album folder belongs to, so songs that an
        # album duplicate already covers aren't listed twice.
        unit_album_group, mixed_units = {}, set()
        for g in groups.values():
            for cid in g["copies"]:
                if g["type"] == "copies":
                    unit_album_group[copies[cid]["path"]] = g["id"]
                else:
                    mixed_units.add(copies[cid]["path"])

        songs, by_key = {}, {}
        for unit, files in units.items():
            parts = os.path.relpath(unit, root).split(os.sep)
            folder_artist, album = guess_artist_album(parts)
            for p, ext, size in files:
                self.files_scanned += 1
                ta, tt, length, br = _song_info(p)
                fa, ft, marked = song_from_filename(os.path.basename(p), folder_artist)
                artist = ta or fa or folder_artist
                title = tt or ft
                ka = normalize(artist)
                ka = ka[4:] if ka.startswith("the ") else ka
                kt = song_key(title)
                if not ka or not kt or ka in VARIOUS or GENERIC_RE.match(kt) or kt.isdigit():
                    continue
                sid = f"s{len(songs) + 1}"
                songs[sid] = {
                    "id": sid, "path": p, "rel": os.path.relpath(p, root), "unit": unit,
                    "unit_rel": os.path.relpath(unit, root), "album": album, "artist": artist, "title": title,
                    "ext": ext, "size": size, "lossless": ext in LOSSLESS, "duration": length,
                    "bitrate": None if ext in LOSSLESS else br, "copy_marked": marked,
                    "unit_tracks": len(files), "rank": fmt_rank(ext),
                }
                by_key.setdefault((ka, kt), []).append(sid)

        song_groups = {}
        for key, sids in sorted(by_key.items()):
            if len(sids) < 2:
                continue
            # Split by length when we know it: a 3:10 studio take and a 7:45 live
            # version with the same title are different recordings.
            clusters, timed = [], sorted((s for s in sids if songs[s]["duration"]), key=lambda s: songs[s]["duration"])
            for s in timed:
                if clusters and songs[s]["duration"] - songs[clusters[-1][-1]]["duration"] <= 8:
                    clusters[-1].append(s)
                else:
                    clusters.append([s])
            untimed = [s for s in sids if not songs[s]["duration"]]
            if clusters:
                clusters.sort(key=len, reverse=True)
                clusters[0].extend(untimed)
            else:
                clusters = [untimed]
            for cl in clusters:
                if len(cl) < 2:
                    continue
                us = {songs[s]["unit"] for s in cl}
                # Already handled on the Albums tab?
                if len(us) >= 2 and len({unit_album_group.get(u) for u in us}) == 1 and unit_album_group.get(next(iter(us))):
                    continue
                if len(us) == 1 and next(iter(us)) in mixed_units and len({songs[s]["ext"] for s in cl}) == len(cl):
                    continue
                gid = f"sg{len(song_groups) + 1}"
                # Name the group by its plainest title ("Big Tune", not "Big Tune (Remastered 2019)").
                first = min((songs[x] for x in cl), key=lambda x: (x["copy_marked"], len(x["title"] or "")))
                g = {"id": gid, "artist": first["artist"], "title": first["title"], "songs": cl}
                self._suggest_song(g, songs)
                song_groups[gid] = g
        return songs, song_groups

    @staticmethod
    def _suggest_song(g, songs):
        def score(sid):
            s = songs[sid]
            return (s["copy_marked"],          # "Song (1).mp3" is the copy
                    s["rank"],                  # preferred format
                    -(s["bitrate"] or 0),       # higher bitrate
                    -s["unit_tracks"],          # the copy inside a full album over a loose single
                    -s["size"])
        g["suggest_keep"] = sorted(g["songs"], key=score)[0]
        g["savings"] = sum(songs[s]["size"] for s in g["songs"] if s != g["suggest_keep"])

    def _suggest(self, g, copies):
        if g["type"] == "copies":
            most = max(copies[c]["tracks"] for c in g["copies"])

            def score(cid):
                c = copies[cid]
                fm = c["formats"][c["main"]]
                return (c["tracks"] < most,            # complete first
                        c["rank"],                     # preferred format
                        -(fm["bitrate"] or 0),         # higher bitrate
                        -c["size"])                    # bigger
            g["suggest_keep"] = sorted(g["copies"], key=score)[0]
            g["savings"] = sum(copies[c]["size"] for c in g["copies"] if c != g["suggest_keep"])
        else:
            c = copies[g["copies"][0]]
            keep = c["mixed_formats"][0]
            # Don't suggest dropping a format that has more tracks than the one we'd keep.
            drop = [e for e in c["mixed_formats"][1:] if c["formats"][e]["count"] <= c["formats"][keep]["count"]]
            g["suggest_keep_format"] = keep
            g["suggest_remove_formats"] = drop
            g["savings"] = sum(c["formats"][e]["size"] for e in drop)

    # ------------------------------------------------------------------ output
    def snapshot(self):
        with self.lock:
            groups = []
            for g in self.groups.values():
                out = dict(g)
                out["copies"] = [self._public(self.copies[c]) for c in g["copies"] if c in self.copies]
                groups.append(out)
            groups.sort(key=lambda g: (normalize(g["artist"]), normalize(g["album"]), g["type"]))
            song_groups = []
            for g in self.song_groups.values():
                out = dict(g)
                out["songs"] = [self._public_song(self.songs[s]) for s in g["songs"] if s in self.songs]
                song_groups.append(out)
            song_groups.sort(key=lambda g: (normalize(g["artist"]), normalize(g["title"])))
            return {"state": self.state, "error": self.error, "folders_scanned": self.folders_scanned,
                    "files_scanned": self.files_scanned,
                    "scanned_at": self.scanned_at, "groups": groups,
                    "savings": sum(g.get("savings", 0) for g in groups),
                    "song_groups": song_groups,
                    "song_savings": sum(g.get("savings", 0) for g in song_groups),
                    "action": (self.app.settings.raw().get("dupes_action") or "trash"),
                    "trash": self.trash_info()}

    @staticmethod
    def _public_song(s):
        return {k: s[k] for k in ("id", "rel", "unit_rel", "album", "title", "ext", "size", "lossless",
                                  "duration", "bitrate", "copy_marked", "unit_tracks")}

    @staticmethod
    def _public(c):
        return {
            "id": c["id"], "rel": c["rel"], "tracks": c["tracks"], "size": c["size"], "main": c["main"],
            "discs": c["discs"], "crate_album": c["crate_album"], "mixed_formats": c["mixed_formats"],
            "formats": {e: {k: v for k, v in fm.items() if k != "files"} for e, fm in c["formats"].items()},
        }

    def trash_info(self):
        t = self.trash_root()
        if not os.path.isdir(t):
            return {"path": t, "size": 0, "batches": 0}
        return {"path": t, "size": _dir_size(t), "batches": len(os.listdir(t))}

    # ------------------------------------------------------------------ actions
    def resolve(self, body):
        """body: {group, keep} | {group, remove_format} | {all: "suggested"} | {all: "keep_format", format}"""
        with self.lock:
            if self.state == "scanning":
                raise DupeError("A scan is running; try again when it finishes")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            result = {"folders": 0, "files": 0, "bytes": 0, "groups": 0, "skipped": 0}
            mode = body.get("all")
            if mode in ("songs_suggested", "songs_keep_format"):
                fmt = (body.get("format") or "").lower()
                if mode == "songs_keep_format" and not fmt:
                    raise DupeError("format required")
                for g in list(self.song_groups.values()):
                    if g["id"] not in self.song_groups:
                        continue
                    keep = g["suggest_keep"]
                    if mode == "songs_keep_format":
                        mine = [s for s in g["songs"] if self.songs[s]["ext"] == fmt]
                        if not mine:
                            result["skipped"] += 1
                            continue
                        keep = sorted(mine, key=lambda s: (self.songs[s]["copy_marked"], -(self.songs[s]["bitrate"] or 0),
                                                           -self.songs[s]["unit_tracks"], -self.songs[s]["size"]))[0]
                    try:
                        self._song_keep(g, keep, stamp, result)
                        result["groups"] += 1
                    except DupeError:
                        result["skipped"] += 1
            elif body.get("song_group"):
                g = self.song_groups.get(body["song_group"])
                if not g:
                    raise DupeError("That duplicate is gone — rescan to refresh the list")
                if body.get("keep"):
                    if body["keep"] not in g["songs"]:
                        raise DupeError("Pick one of this song's copies to keep")
                    self._song_keep(g, body["keep"], stamp, result)
                elif body.get("remove"):
                    if body["remove"] not in g["songs"]:
                        raise DupeError("That copy isn't part of this song")
                    if len(g["songs"]) < 2:
                        raise DupeError("That's the last copy of this song")
                    self._song_remove(g, body["remove"], stamp, result)
                else:
                    raise DupeError("Nothing to do")
                result["groups"] = 1
            elif mode:
                fmt = (body.get("format") or "").lower()
                if mode == "keep_format" and not fmt:
                    raise DupeError("format required")
                # Folder copies first, then mixed folders that still exist.
                for g in sorted(list(self.groups.values()), key=lambda g: g["type"] != "copies"):
                    if g["id"] not in self.groups:
                        continue
                    try:
                        if g["type"] == "copies":
                            keep = g["suggest_keep"]
                            if mode == "keep_format":
                                mine = [c for c in g["copies"] if self.copies[c]["main"] == fmt]
                                if not mine:
                                    result["skipped"] += 1
                                    continue
                                keep = sorted(mine, key=lambda c: (-self.copies[c]["tracks"], -self.copies[c]["size"]))[0]
                            self._keep_copy(g, keep, stamp, result)
                        else:
                            c = self.copies[g["copies"][0]]
                            if mode == "keep_format":
                                if fmt not in c["mixed_formats"]:
                                    result["skipped"] += 1
                                    continue
                                drop = [e for e in c["mixed_formats"] if e != fmt]
                            else:
                                drop = g["suggest_remove_formats"]
                            if not drop:
                                result["skipped"] += 1
                                continue
                            for e in drop:
                                self._remove_format(g, e, stamp, result, drop_group=False)
                            self._drop_group(g["id"])
                        result["groups"] += 1
                    except DupeError:
                        result["skipped"] += 1
            else:
                g = self.groups.get(body.get("group"))
                if not g:
                    raise DupeError("That duplicate is gone — rescan to refresh the list")
                if body.get("keep"):
                    if g["type"] != "copies" or body["keep"] not in g["copies"]:
                        raise DupeError("Pick one of this album's copies to keep")
                    self._keep_copy(g, body["keep"], stamp, result)
                elif body.get("remove_format"):
                    if g["type"] != "mixed":
                        raise DupeError("Not a mixed-format folder")
                    self._remove_format(g, body["remove_format"].lower(), stamp, result)
                else:
                    raise DupeError("Nothing to do")
                result["groups"] = 1
        self.app.library.scan()
        verb = "Moved to trash" if self._action() == "trash" else "Deleted"
        if result["groups"]:
            self.app.db.event(
                f"Duplicates: {verb.lower()} {result['folders']} folder(s) and {result['files']} file(s) "
                f"({result['bytes'] / 1e9:.2f} GB) across {result['groups']} album(s)", "success")
        return result

    def _song_remove(self, g, sid, stamp, result):
        s = self.songs[sid]
        if os.path.exists(s["path"]):
            result["bytes"] += os.path.getsize(s["path"])
            self._dispose(s["path"], stamp)
            result["files"] += 1
            self._prune(os.path.dirname(s["path"]))
        self.songs.pop(sid, None)
        g["songs"] = [x for x in g["songs"] if x != sid]
        if len(g["songs"]) < 2:
            self.song_groups.pop(g["id"], None)
        else:
            self._suggest_song(g, self.songs)

    def _song_keep(self, g, keep_sid, stamp, result):
        if not os.path.exists(self.songs[keep_sid]["path"]):
            raise DupeError(f"{self.songs[keep_sid]['rel']} no longer exists — rescan first")
        for sid in [x for x in g["songs"] if x != keep_sid]:
            if g["id"] not in self.song_groups:
                break
            self._song_remove(g, sid, stamp, result)
        self.song_groups.pop(g["id"], None)

    def _action(self):
        return "delete" if (self.app.settings.raw().get("dupes_action") or "trash") == "delete" else "trash"

    def _keep_copy(self, g, keep_cid, stamp, result):
        keep = self.copies[keep_cid]
        if not os.path.isdir(keep["path"]):
            raise DupeError(f"{keep['rel']} no longer exists — rescan first")
        for cid in g["copies"]:
            if cid == keep_cid:
                continue
            c = self.copies[cid]
            if not os.path.exists(c["path"]):
                continue
            size = _dir_size(c["path"]) if not c["nested"] else c["size"]
            if c["nested"]:
                # Folder also holds other albums: only remove this album's own audio + images.
                for fm in c["formats"].values():
                    for p in fm["files"]:
                        self._dispose(p, stamp)
                        result["files"] += 1
                for fn in os.listdir(c["path"]):
                    p = os.path.join(c["path"], fn)
                    if os.path.isfile(p) and _ext(fn) in IMAGE_EXT | {"cue", "log", "m3u", "m3u8", "nfo"}:
                        self._dispose(p, stamp)
            else:
                self._dispose(c["path"], stamp)
                result["folders"] += 1
            result["bytes"] += size
            self._prune(os.path.dirname(c["path"]))
            # Albums Crate tracks at the removed path now point at the kept copy.
            self.app.db.x("UPDATE albums SET path = ? WHERE path = ?", (keep["path"], c["path"]))
            self._forget_copy(cid)
        self._drop_group(g["id"])

    def _remove_format(self, g, ext, stamp, result, drop_group=True):
        c = self.copies[g["copies"][0]]
        if ext not in c["formats"]:
            raise DupeError(f"No {ext.upper()} files in that folder")
        others = [e for e in c["mixed_formats"] if e != ext]
        if not others:
            raise DupeError("That would remove every copy of the album")
        for p in c["formats"][ext]["files"]:
            if os.path.exists(p):
                result["bytes"] += os.path.getsize(p)
                self._dispose(p, stamp)
                result["files"] += 1
        for d in {os.path.dirname(p) for p in c["formats"][ext]["files"]}:
            self._prune(d)
        fm = c["formats"].pop(ext)
        c["size"] -= fm["size"]
        c["mixed_formats"] = others if len(others) >= 2 else []
        if drop_group:
            self._drop_group(g["id"])

    def _dispose(self, path, stamp):
        if not self._inside(path):
            raise DupeError(f"Refusing to touch {path}: outside the library folder")
        if self._action() == "delete":
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            return
        dest = os.path.join(self.trash_root(), stamp, os.path.relpath(path, self.root()))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(path, dest)

    def _prune(self, d):
        """Remove now-empty folders up to (not including) the library root."""
        root = self.root()
        d = os.path.realpath(d)
        while d.startswith(root + os.sep):
            try:
                if os.listdir(d):
                    break
                os.rmdir(d)
            except OSError:
                break
            d = os.path.dirname(d)

    def _forget_copy(self, cid):
        self.copies.pop(cid, None)
        for gid, g in list(self.groups.items()):
            if cid in g["copies"]:
                g["copies"] = [c for c in g["copies"] if c != cid]
                if (g["type"] == "copies" and len(g["copies"]) < 2) or not g["copies"]:
                    self.groups.pop(gid, None)

    def _drop_group(self, gid):
        self.groups.pop(gid, None)

    def empty_trash(self):
        t = self.trash_root()
        info = self.trash_info()
        if os.path.isdir(t):
            shutil.rmtree(t)
            self.app.db.event(f"Emptied duplicate trash ({info['size'] / 1e9:.2f} GB)")
        return info
