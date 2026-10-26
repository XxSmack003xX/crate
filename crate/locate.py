"""Find the files slskd downloaded, as Crate sees them.

slskd saves a download as <downloads>/<remote folder name>/<file name> by default, but:
  * its "subdirectory" setting can add levels (e.g. the uploader's username);
  * a name that's already taken gets a suffix: "01 Song_638650123456789012.flac";
  * characters invalid on the host are replaced with "_";
  * Crate may see the downloads folder at a different path than slskd does
    (/downloads here, /app/downloads in slskd's container).

So we ask slskd where it saved each file (its event log, when it has one), map that
path onto Crate's downloads folder, and otherwise search the folder by a normalised
file name, size and parent folder. We also report why a file can't be used
(permissions, wrong folder) instead of just waiting.
"""
import os
import re
import unicodedata

WIN_BAD = set('<>:"/\\|?*') | {chr(i) for i in range(32)}
TICKS_RE = re.compile(r"_\d{15,20}$")  # slskd's "already exists" suffix (UTC ticks)


def split_remote(path):
    return [p for p in re.split(r"[\\/]", path or "") if p]


def norm(name):
    """Comparable form of a file or folder name."""
    name = unicodedata.normalize("NFC", name or "")
    stem, ext = os.path.splitext(name)
    stem = TICKS_RE.sub("", stem)
    s = "".join("_" if c in WIN_BAD else c for c in stem + ext)
    return re.sub(r"\s+", " ", s).strip().casefold()


class Finder:
    def __init__(self, root):
        self.root = root
        self._index = None
        self.unreadable = []   # folders Crate isn't allowed to open

    # ---- index of every file under the downloads folder
    def index(self):
        if self._index is None:
            idx = {}

            def err(e):
                if isinstance(e, PermissionError):
                    self.unreadable.append(getattr(e, "filename", None) or str(e))
            for dp, _, fns in os.walk(self.root, onerror=err):
                for fn in fns:
                    idx.setdefault(norm(fn), []).append(os.path.join(dp, fn))
            self._index = idx
        return self._index

    def map_local(self, local):
        """slskd's path for a file → the same file under our root (tries every suffix)."""
        parts = split_remote(local)
        for i in range(1, len(parts)):
            p = os.path.join(self.root, *parts[i:])
            if os.path.isfile(p):
                return p
        return None

    @staticmethod
    def _size(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return None

    def find(self, f, username=None, saved=None, taken=()):
        """Path of downloaded file `f` ({filename, size}), or (None, reason)."""
        parts = split_remote(f["filename"])
        if not parts:
            return None, "no file name"
        name, size = parts[-1], f.get("size")
        # 1. Where slskd says it saved it.
        local = (saved or {}).get((username or "", f["filename"]))
        if local:
            p = self.map_local(local)
            if p and p not in taken:
                return p, None
        # 2. The usual places.
        tries = []
        if len(parts) >= 2:
            tries.append(os.path.join(self.root, parts[-2], name))
        if len(parts) >= 3:
            tries.append(os.path.join(self.root, parts[-3], parts[-2], name))
        tries.append(os.path.join(self.root, name))
        for p in tries:
            if p not in taken and os.path.isfile(p) and (not size or self._size(p) == size):
                return p, None
        # 3. Anywhere under the downloads folder, by normalised name.
        cands = [p for p in self.index().get(norm(name), []) if p not in taken]
        parent = norm(parts[-2]) if len(parts) >= 2 else None
        in_dir = lambda p: parent is not None and norm(os.path.basename(os.path.dirname(p))) == parent
        exact = [p for p in cands if not size or self._size(p) == size]
        if exact:
            return sorted(exact, key=lambda p: (not in_dir(p), -os.path.getmtime(p)))[0], None
        # 4. Same name, in a folder with the album's folder name, and within 2% of the
        #    expected size (some clients report sizes loosely).
        near = [p for p in cands if in_dir(p)]
        if len(near) == 1 and size and self._size(near[0]):
            got = self._size(near[0])
            if abs(got - size) <= max(4096, size * 0.02):
                return near[0], None
        if local:
            return None, f"slskd saved it as {local}, but it isn't under {self.root}"
        if cands:
            return None, f"found {os.path.relpath(cands[0], self.root)}, but its size doesn't match"
        return None, None

    @staticmethod
    def readable(path):
        return os.access(path, os.R_OK)
