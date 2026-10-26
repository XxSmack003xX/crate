"""Turn raw slskd search responses into ranked album download candidates."""
import re
import unicodedata

AUDIO_EXT = {"flac", "mp3", "m4a", "aac", "ogg", "opus", "wav", "alac", "ape", "wv", "aiff", "aif", "wma"}
LOSSLESS = {"flac", "alac", "ape", "wv", "wav", "aiff", "aif"}
IMAGE_EXT = {"jpg", "jpeg", "png"}
DISC_RE = re.compile(r"^(cd|disc|disk|dvd)\s*[-_.]?\s*\d+\b.*$", re.I)
EXTRA_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def normalize(s):
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ").replace("’", "'")
    s = s.replace("'", "")
    s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE)
    s = s.replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


def strip_extras(title):
    """'Album (Deluxe Edition) [Remastered]' -> 'Album'"""
    t = EXTRA_RE.sub("", title or "").strip()
    return t or (title or "")


def search_queries(artist, title):
    """Search strings to try, in order.

    1. "Artist Album" — the normal case.
    2. "Album" alone — many folders don't name the artist, and some artists' names
       are on Soulseek's excluded-phrases list, so searches that include them come
       back empty (e.g. Michael Jackson). The artist is still checked against the
       path, or the tracklist has to match closely.
    3. "*rtist Album" — the usual Soulseek trick for excluded artist names: a
       wildcard instead of each word's first letter.
    """
    base = strip_extras(title)
    clean = lambda s: re.sub(r"\s+", " ", re.sub(r"[^\w\s'*]", " ", s, flags=re.UNICODE)).strip()
    queries = [clean(f"{artist} {base}")]
    words = [w for w in normalize(base).split() if w]
    # Title alone, unless it's too short to mean anything on its own ("21", "X").
    if words and len(normalize(base)) >= 3:
        queries.append(clean(base))
    art_words = clean(artist).split()
    if any(len(w) >= 3 for w in art_words):
        stop = {"the", "and", "of", "a", "an", "la", "le", "les", "el", "los", "die", "der"}
        wild = " ".join("*" + w[1:] if len(w) >= 3 and w.lower() not in stop else w for w in art_words)
        queries.append(clean(f"{wild} {base}"))
    out = []
    for q in queries:
        if q and q not in out:
            out.append(q)
    return out


REASONS = {
    "no_audio": "no audio files",
    "format": "not a format you want",
    "bitrate": "bitrate too low",
    "too_few": "too few tracks",
    "too_many": "too many tracks",
    "title": "album title not in the folder name",
    "artist": "artist not in the path",
    "tracks": "tracklist didn't match",
    "tried": "source already tried",
}


def _ext(f):
    e = (f.get("extension") or "").lower().lstrip(".")
    if not e:
        name = f.get("filename", "")
        e = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return e


def _split(path):
    parts = [p for p in re.split(r"[\\/]", path) if p]
    return parts[:-1], parts[-1] if parts else ""


def group_responses(responses):
    """Group files by (user, album directory); disc subfolders fold into their parent."""
    groups = {}
    for resp in responses or []:
        user = resp.get("username")
        if not user:
            continue
        files = list(resp.get("files") or [])
        for f in files:
            if f.get("isLocked"):
                continue
            dirs, name = _split(f.get("filename", ""))
            if not dirs:
                continue
            sub = ""
            if len(dirs) >= 2 and DISC_RE.match(dirs[-1]):
                sub = dirs[-1]
                dirs = dirs[:-1]
            key = (user, "\\".join(dirs))
            g = groups.setdefault(key, {
                "username": user,
                "directory": "\\".join(dirs),
                "free_slot": bool(resp.get("hasFreeUploadSlot")),
                "speed": int(resp.get("uploadSpeed") or 0),
                "queue": int(resp.get("queueLength") or 0),
                "files": [],
            })
            g["files"].append({
                "filename": f["filename"],
                "size": int(f.get("size") or 0),
                "name": name,
                "sub": sub,
                "ext": _ext(f),
                "bitrate": f.get("bitRate"),
                "bitdepth": f.get("bitDepth"),
                "samplerate": f.get("sampleRate"),
            })
    return list(groups.values())


def rank_candidates(responses, artist, title, track_count=None, track_titles=None,
                    preferred_formats=("flac", "mp3"), min_bitrate=256, primary_type="album",
                    exclude_users=(), limit=15, stats=None):
    """Ranked download candidates. If `stats` (a dict) is given, it counts why folders
    were rejected (keys of REASONS) and how many were looked at ("folders")."""
    stats = stats if stats is not None else {}

    def reject(why):
        stats[why] = stats.get(why, 0) + 1
    n_artist = normalize(artist)
    n_title = normalize(strip_extras(title))
    n_full_title = normalize(title)
    n_tracks = [normalize(strip_extras(t)) for t in (track_titles or []) if t]
    n_tracks = [t for t in n_tracks if t]
    prefs = [p.lower() for p in preferred_formats] or ["flac", "mp3"]
    exclude = set(exclude_users or ())
    single = (primary_type or "").lower() == "single"

    out = []
    for g in group_responses(responses):
        stats["folders"] = stats.get("folders", 0) + 1
        if g["username"] in exclude:
            reject("tried")
            continue
        audio = [f for f in g["files"] if f["ext"] in AUDIO_EXT]
        if not audio:
            reject("no_audio")
            continue
        # Choose the best preferred format available in this folder.
        by_fmt = {}
        for f in audio:
            by_fmt.setdefault(f["ext"], []).append(f)
        fmt = next((p for p in prefs if p in by_fmt), None)
        if fmt is None:
            reject("format")
            continue
        files = by_fmt[fmt]
        n = len(files)

        # Bitrate floor for lossy formats.
        bitrates = [int(f["bitrate"]) for f in files if f.get("bitrate")]
        avg_br = int(sum(bitrates) / len(bitrates)) if bitrates else None
        if fmt not in LOSSLESS and avg_br is not None and avg_br < int(min_bitrate) - 5:
            reject("bitrate")
            continue

        # Track count sanity.
        if track_count:
            if n < track_count:
                reject("too_few")
                continue
            if n > max(track_count * 2, track_count + 6):
                reject("too_many")
                continue
        else:
            if n < (1 if single else 3):
                reject("too_few")
                continue

        dirs = g["directory"].split("\\")
        last = normalize(dirs[-1])
        last_two = normalize(" ".join(dirs[-2:]))
        whole = normalize(g["directory"] + " " + " ".join(f["name"] for f in files))

        # Album title must appear in the folder name (or parent for short titles).
        if n_title and n_title not in last_two and n_full_title not in last_two:
            reject("title")
            continue

        # Track-title overlap with the expected tracklist.
        ratio = None
        if n_tracks:
            names = [normalize(f["name"].rsplit(".", 1)[0]) for f in files]
            hits = sum(1 for t in n_tracks if any(t in nm for nm in names))
            ratio = hits / len(n_tracks)
            if len(n_tracks) >= 4 and ratio < 0.4:
                reject("tracks")
                continue

        # Artist should appear somewhere in the path or filenames. Plenty of folders
        # are just "Thriller (1982)", so a closely matching tracklist also counts.
        artist_ok = not n_artist or n_artist in whole or (
            n_artist.startswith("the ") and n_artist[4:] in whole)
        if not artist_ok and not (ratio is not None and len(n_tracks) >= 3 and ratio >= 0.75):
            reject("artist")
            continue

        score = 1000 - prefs.index(fmt) * 300 - (0 if artist_ok else 150)
        if ratio is not None:
            score += int(ratio * 200)
        if track_count and n == track_count:
            score += 60
        if n_title and last.endswith(n_title):
            score += 20
        if fmt in LOSSLESS:
            bd = [f.get("bitdepth") for f in files if f.get("bitdepth")]
            if bd and max(bd) > 16:
                score -= 15  # prefer 16-bit unless nothing else (smaller, more compatible)
        elif avg_br:
            score += min(avg_br, 320) // 16
        score += 120 if g["free_slot"] else 0
        score -= min(g["queue"] * 3, 150)
        score += min(g["speed"] // (256 * 1024), 80)

        # Grab a cover image too if the folder has a small one.
        images = sorted(
            (f for f in g["files"] if f["ext"] in IMAGE_EXT and 0 < f["size"] < 8 * 1024 * 1024),
            key=lambda f: (0 if re.search(r"cover|folder|front", f["name"], re.I) else 1, -f["size"]),
        )[:1]

        dl = files + images
        out.append({
            "username": g["username"],
            "directory": g["directory"],
            "format": fmt,
            "track_files": n,
            "bitrate": avg_br,
            "size": sum(f["size"] for f in dl),
            "free_slot": g["free_slot"],
            "queue": g["queue"],
            "speed": g["speed"],
            "title_match": round(ratio, 2) if ratio is not None else None,
            "artist_in_path": artist_ok,
            "score": score,
            "files": [{"filename": f["filename"], "size": f["size"], "sub": f["sub"]} for f in dl],
        })

    out.sort(key=lambda c: -c["score"])
    return out[:limit]


def explain(stats):
    """'12 too few tracks, 9 tracklist didn't match' from rank_candidates stats."""
    parts = sorted(((n, k) for k, n in stats.items() if k in REASONS and n), reverse=True)
    return ", ".join(f"{n} {REASONS[k]}" for n, k in parts[:3])
