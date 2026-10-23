"""Scan the music library on disk and check whether an album is already there."""
import difflib
import os
import re
import threading

from .matcher import AUDIO_EXT, DISC_RE, normalize, strip_extras

try:  # optional: read real tags when mutagen is installed
    import mutagen  # type: ignore
except Exception:  # pragma: no cover
    mutagen = None

YEAR_RE = re.compile(r"^\s*[\(\[]?(19|20)\d{2}[\)\]]?\s*[-–.]?\s*|\s*[\(\[](19|20)\d{2}[\)\]]\s*$")
TAG_RE = re.compile(r"\s*[\(\[](flac|mp3|v0|320|24bit|16bit|web|cd|lossless)[^\)\]]*[\)\]]", re.I)


def _clean_album_folder(name):
    n = TAG_RE.sub("", name)
    n = YEAR_RE.sub("", n)
    return n.strip()


def _tags(path):
    if mutagen is None:
        return None, None
    try:
        f = mutagen.File(path, easy=True)
        if not f or not f.tags:
            return None, None
        artist = (f.tags.get("albumartist") or f.tags.get("artist") or [None])[0]
        album = (f.tags.get("album") or [None])[0]
        return artist, album
    except Exception:
        return None, None


def guess_artist_album(parts):
    """(artist, album) from a folder path relative to the library root."""
    if not parts:
        return "", ""
    album_name = _clean_album_folder(parts[-1])
    artist_name = parts[-2] if len(parts) >= 2 else ""
    if " - " in album_name and not artist_name:
        artist_name, album_name = album_name.split(" - ", 1)
    elif " - " in album_name and normalize(album_name.split(" - ", 1)[0]) == normalize(artist_name):
        album_name = album_name.split(" - ", 1)[1]
    return artist_name.strip(), _clean_album_folder(album_name)


class Library:
    def __init__(self, settings):
        self.settings = settings
        self.entries = []          # [(norm_artist, norm_album, path)]
        self.lock = threading.Lock()
        self.scanned_root = None

    def scan(self):
        root = os.path.abspath(os.path.expanduser(self.settings.library_dir))
        entries = []
        if os.path.isdir(root):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]  # skips .crate-trash
                audio = [f for f in filenames if f.rsplit(".", 1)[-1].lower() in AUDIO_EXT]
                if not audio:
                    continue
                rel = os.path.relpath(dirpath, root)
                parts = [] if rel == "." else rel.split(os.sep)
                if parts and DISC_RE.match(parts[-1]) and len(parts) >= 2:
                    album_dir = os.path.dirname(dirpath)
                    parts = parts[:-1]
                else:
                    album_dir = dirpath
                names = set()
                # From folder structure: Artist/Album or "Artist - Album"
                if parts:
                    album_name = _clean_album_folder(parts[-1])
                    artist_name = parts[-2] if len(parts) >= 2 else ""
                    if " - " in album_name and not artist_name:
                        artist_name, album_name = album_name.split(" - ", 1)
                    elif " - " in album_name and normalize(album_name.split(" - ", 1)[0]) == normalize(artist_name):
                        album_name = album_name.split(" - ", 1)[1]
                    names.add((artist_name, album_name))
                # From tags of the first track
                ta, tb = _tags(os.path.join(dirpath, sorted(audio)[0]))
                if tb:
                    names.add((ta or (parts[-2] if len(parts) >= 2 else ""), tb))
                for a, b in names:
                    entries.append((normalize(a), normalize(strip_extras(b)), album_dir))
        with self.lock:
            self.entries = entries
            self.scanned_root = root
        return len({e[2] for e in entries})

    def find(self, artist, title):
        """Return the path of a matching album folder, or None."""
        if self.scanned_root != os.path.abspath(os.path.expanduser(self.settings.library_dir)):
            self.scan()
        na = normalize(artist)
        na_alt = na[4:] if na.startswith("the ") else na
        nt = normalize(strip_extras(title))
        if not nt:
            return None
        with self.lock:
            entries = list(self.entries)
        for ea, eb, path in entries:
            if not ea:
                continue
            if ea not in (na, na_alt) and "the " + ea != na:
                # Allow "Artist feat. X" style folders
                if not (na and ea.startswith(na + " ")):
                    continue
            if eb == nt:
                return path
            if len(nt) >= 5 and difflib.SequenceMatcher(None, eb, nt).ratio() >= 0.92:
                return path
        return None

    def add(self, artist, title, path):
        with self.lock:
            self.entries.append((normalize(artist), normalize(strip_extras(title)), path))
