"""Local music for the worker: the folders under your music directory as
collections the panel can browse and play, with tags read by mutagen.

The music directory is XDG_MUSIC_DIR from ~/.config/user-dirs.dirs (change it
with ``xdg-user-dirs-update --set MUSIC DIR``), else ~/Music;
YTM_MUSIC_DIRS (colon-separated) overrides both. Only paths inside one of
them are listed.

Tags are cached by mtime and size in ~/.cache/omarchy-ytmusic/local-tags.json.
A folder's cover is an image next to the songs (cover.jpg, folder.png, …) or
the first song's embedded picture, extracted once into
~/.cache/omarchy-ytmusic/local-art.
"""

import base64
import hashlib
import json
import os
import re
import threading
from pathlib import Path

import ytm_cache as caches
import ytm_normalize as norm

AUDIO_EXTS = frozenset((".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wv",
                        ".ape", ".aiff", ".aif", ".wma", ".mka", ".mpc", ".dsf"))
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
# Cover file names by preference (case-insensitive, any image extension).
COVER_NAMES = ("cover", "folder", "front", "album", "albumart")
MAX_TRACKS = 5000
MAX_DEPTH = 12
_DIGITS = re.compile(r"(\d+)")
_NUMBER = re.compile(r"\s*(\d+)")


class LocalError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---- music directories --------------------------------------------------------


def xdg_music_dir(env=None, home=None):
    env = os.environ if env is None else env
    home = home or str(Path.home())
    config = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    try:
        lines = Path(config, "user-dirs.dirs").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        match = re.match(r'\s*XDG_MUSIC_DIR="(.*)"\s*$', line)
        if match:
            value = match.group(1).replace("$HOME", home)
            # "$HOME/" means the directory is disabled.
            return value if os.path.normpath(value) != os.path.normpath(home) else None
    return None


def music_dirs(env=None, home=None):
    env = os.environ if env is None else env
    home = home or str(Path.home())
    raw = env.get("YTM_MUSIC_DIRS")
    dirs = raw.split(":") if raw else [xdg_music_dir(env, home) or os.path.join(home, "Music")]
    out = []
    for value in dirs:
        if value.strip():
            path = os.path.normpath(os.path.abspath(os.path.expanduser(value.strip())))
            if path not in out:
                out.append(path)
    return out


def display(path, home=None):
    home = home or str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def natural(name):
    """Sort key: "Track 2" before "Track 10", case-insensitive."""
    return [int(part) if part.isdigit() else part.casefold() for part in _DIGITS.split(name)]


def _number(value):
    match = _NUMBER.match(str(value or ""))
    return int(match.group(1)) if match else None


def file_uri(path):
    return Path(path).as_uri()


# ---- tags ---------------------------------------------------------------------


def read_tags(path):
    """Title, artists, album, track, disc, year and duration (None when missing)."""
    import mutagen

    try:
        audio = mutagen.File(path, easy=True)
    except Exception:  # noqa: BLE001 - unreadable, or not audio after all
        audio = None
    tags = getattr(audio, "tags", None)

    def values(key):
        try:
            value = tags.get(key) if tags is not None else None
        except (KeyError, ValueError, TypeError):
            value = None
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [str(v).strip() for v in value if str(v).strip()]

    def first(key):
        found = values(key)
        return found[0] if found else None

    length = getattr(getattr(audio, "info", None), "length", None)
    year = first("date")
    return {
        "title": first("title"),
        "artists": values("artist") or values("albumartist"),
        "albumArtist": first("albumartist"),
        "album": first("album"),
        "track": _number(first("tracknumber")),
        "disc": _number(first("discnumber")),
        "year": year[:4] if year and year[:4].isdigit() else None,
        "durationSec": int(round(length)) if isinstance(length, (int, float)) and length > 0 else None,
    }


def embedded_picture(path):
    """``(bytes, mime)`` of the front cover inside an audio file, or ``(None, None)``."""
    import mutagen

    try:
        audio = mutagen.File(path)
    except Exception:  # noqa: BLE001
        return None, None
    if audio is None:
        return None, None
    pictures = list(getattr(audio, "pictures", None) or [])
    tags = audio.tags
    if not pictures and tags is not None:
        if hasattr(tags, "getall"):  # ID3
            pictures = tags.getall("APIC")
        else:
            try:
                covers = tags.get("covr")  # MP4
                blocks = tags.get("metadata_block_picture")  # Ogg
            except (KeyError, ValueError, TypeError):
                covers = blocks = None
            if covers:
                from mutagen.mp4 import MP4Cover

                cover = covers[0]
                return bytes(cover), "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
            for block in blocks or []:
                try:
                    from mutagen.flac import Picture

                    pictures.append(Picture(base64.b64decode(block)))
                except Exception:  # noqa: BLE001
                    continue
    if not pictures:
        return None, None
    front = next((p for p in pictures if getattr(p, "type", None) == 3), pictures[0])
    return front.data, getattr(front, "mime", None) or "image/jpeg"


class TagCache:
    """path -> [mtime_ns, size, tags], saved as one JSON file."""

    VERSION = 1
    MAX_ENTRIES = 50000

    def __init__(self, path, reader=read_tags):
        self.path = Path(path)
        self.reader = reader
        self._files = None
        self._dirty = False

    def _load(self):
        if self._files is not None:
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = None
        files = data.get("files") if isinstance(data, dict) and data.get("version") == self.VERSION else None
        self._files = files if isinstance(files, dict) else {}

    def get(self, path, stat):
        self._load()
        stamp = [stat.st_mtime_ns, stat.st_size]
        hit = self._files.get(path)
        if isinstance(hit, list) and len(hit) == 3 and hit[:2] == stamp and isinstance(hit[2], dict):
            return hit[2]
        tags = self.reader(path)
        self._files.pop(path, None)
        self._files[path] = stamp + [tags]
        self._dirty = True
        return tags

    def save(self):
        if not self._dirty:
            return
        # Oldest entries first (dicts keep insertion order; a re-read moves to the end).
        excess = len(self._files) - self.MAX_ENTRIES
        if excess > 0:
            for key in list(self._files)[:excess]:
                del self._files[key]
        caches.private_dir(self.path.parent)
        data = {"version": self.VERSION, "files": self._files}
        caches.write_private(self.path, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
        self._dirty = False


# ---- the library ----------------------------------------------------------------


def track(path, tags, thumb):
    """A local song, shaped like the worker's YouTube tracks. ``path`` is its id."""
    artist_list = [{"name": name, "id": None} for name in tags.get("artists") or []]
    return {
        "type": "track",
        "local": True,
        "path": path,
        "videoId": "",
        "title": tags.get("title") or os.path.splitext(os.path.basename(path))[0],
        "artists": artist_list,
        "artistText": norm.artist_text(artist_list),
        "album": {"name": tags["album"], "id": None} if tags.get("album") else None,
        "durationSec": tags.get("durationSec"),
        "thumb": thumb,
        "thumbLarge": thumb,
        "explicit": False,
        "likeStatus": None,
        "setVideoId": None,
        "available": True,
        "kind": "song",
        "played": None,
    }


def _counts(folders, songs):
    return norm._join(f"{folders} folder{'s' if folders != 1 else ''}" if folders else "",
                      f"{songs} song{'s' if songs != 1 else ''}" if songs else "")


class Library:
    ART_MAX_FILES = 3000

    def __init__(self, roots=None, cache_dir=None, reader=read_tags, picture=embedded_picture):
        self._roots = roots
        self.cache_dir = Path(cache_dir) if cache_dir else caches.cache_root()
        self.tags = TagCache(self.cache_dir / "local-tags.json", reader)
        self._picture = picture
        # One lane calls in, but a CLI call and the lane must not share a half-written cache.
        self._lock = threading.Lock()

    def roots(self):
        return list(self._roots) if self._roots is not None else music_dirs()

    def folder(self, path=""):
        """A folder's songs (tags, cover) and subfolders, as a collection."""
        with self._lock:
            try:
                return self._folder(path)
            finally:
                self._save_tags()

    def tracks_under(self, path=""):
        """Every song inside a folder and its subfolders, in folder order."""
        with self._lock:
            try:
                return self._tracks_under(path)
            finally:
                self._save_tags()

    def _save_tags(self):
        try:
            self.tags.save()
        except OSError as exc:
            norm.log(f"could not save the tag cache: {exc}")

    # ---- paths ----

    def _inside(self, path):
        """The normalized path when it lies in a music directory."""
        if not isinstance(path, str) or not path.strip():
            roots = self.roots()
            if len(roots) != 1:
                return None  # several roots: the virtual top level
            path = roots[0]
        full = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
        for root in self.roots():
            if full == root or full.startswith(root.rstrip("/") + "/"):
                if not os.path.isdir(full):
                    raise LocalError("NOT_FOUND", f"{display(full)} doesn't exist")
                return full
        raise LocalError("BAD_REQUEST", f"{display(full)} is not in your music folders")

    def _scan(self, path):
        """``(folders, songs, images)`` directly inside ``path``, sorted."""
        folders, songs, images = [], [], []
        try:
            entries = list(os.scandir(path))
        except OSError as exc:
            raise LocalError("NOT_FOUND", f"Can't read {display(path)}: {exc.strerror or exc}") from exc
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    folders.append(entry.path)
                elif entry.is_file():
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in AUDIO_EXTS:
                        songs.append(entry.path)
                    elif ext in IMAGE_EXTS:
                        images.append(entry.path)
            except OSError:
                continue
        for group in (folders, songs, images):
            group.sort(key=lambda p: natural(os.path.basename(p)))
        return folders, songs, images

    def _scan_quiet(self, path):
        try:
            return self._scan(path)
        except LocalError:
            return [], [], []

    # ---- covers ----

    def _cover(self, path, scan=None, depth=1):
        """file:// URI of the folder's cover, else of a subfolder's (``depth`` levels down)."""
        folders, songs, images = scan or self._scan_quiet(path)
        if images:
            stems = {os.path.splitext(os.path.basename(p))[0].lower(): p for p in reversed(images)}
            named = next((stems[n] for n in COVER_NAMES if n in stems), None)
            loose = next((p for p in images if any(w in os.path.basename(p).lower() for w in ("cover", "front"))), None)
            return file_uri(named or loose or images[0])
        if songs:
            art = self._embedded(songs[0])
            if art:
                return art
        if depth > 0:
            for folder in folders[:3]:
                art = self._cover(folder, None, depth - 1)
                if art:
                    return art
        return None

    def _embedded(self, path):
        try:
            stat = os.stat(path)
        except OSError:
            return None
        art_dir = self.cache_dir / "local-art"
        key = hashlib.sha1(f"{path}\0{stat.st_mtime_ns}\0{stat.st_size}".encode()).hexdigest()[:24]
        for ext in (".jpg", ".png", ".webp", ".none"):
            if (art_dir / (key + ext)).exists():
                return None if ext == ".none" else file_uri(art_dir / (key + ext))
        data, mime = self._picture(path)
        ext = ".png" if mime == "image/png" else ".webp" if mime == "image/webp" else ".jpg"
        if not data or len(data) > 10_000_000:
            data, ext = b"", ".none"  # remembered, so the file isn't read again
        try:
            caches.private_dir(self.cache_dir)
            caches.private_dir(art_dir)
            caches.write_private(art_dir / (key + ext), data)
            self._prune_art(art_dir)
        except OSError as exc:
            norm.log(f"could not save a cover: {exc}")
            return None
        return file_uri(art_dir / (key + ext)) if data else None

    def _prune_art(self, art_dir):
        files = sorted(art_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        for path in files[:-self.ART_MAX_FILES]:
            path.unlink(missing_ok=True)

    # ---- listings ----

    def _tracks(self, songs, cover):
        """Tracks in disc / track number order (then by name), and their tags."""
        rows = []
        for path in songs:
            try:
                stat = os.stat(path)
            except OSError:
                continue
            rows.append((self.tags.get(path, stat), path))
        rows.sort(key=lambda row: (row[0].get("disc") or 1, row[0].get("track") or 1_000_000,
                                   natural(os.path.basename(row[1]))))
        return [track(path, tags, cover) for tags, path in rows], [tags for tags, _ in rows]

    def _folder_item(self, path):
        folders, songs, images = self._scan_quiet(path)
        if not folders and not songs:
            return None
        return {
            "type": "folder",
            "path": path,
            "title": os.path.basename(path),
            "subtitle": _counts(len(folders), len(songs)),
            "thumb": self._cover(path, (folders, songs, images)),
        }

    def _folder(self, path):
        full = self._inside(path)
        if full is None:
            items = [self._folder_item(root) or {"type": "folder", "path": root, "title": os.path.basename(root),
                                                 "subtitle": "Empty", "thumb": None}
                     for root in self.roots() if os.path.isdir(root)]
            if not items:
                raise LocalError("NOT_FOUND", "None of your music folders exist")
            return self._collection("", "Music", _counts(len(items), 0), None, [], items)
        folders, songs, images = self._scan(full)
        cover = self._cover(full, (folders, songs, images), depth=0 if songs else 1)
        tracks, tags = self._tracks(songs, cover)
        items = [item for item in map(self._folder_item, folders) if item]
        counts = _counts(len(items), len(tracks))
        if full in self.roots():
            subtitle = norm._join(display(full), counts)
        else:
            first = tags[0] if tags else {}
            artist = first.get("albumArtist") or (first.get("artists") or [None])[0]
            subtitle = norm._join(artist, first.get("year"), counts)
        return self._collection(full, os.path.basename(full) or full, subtitle, cover, tracks, items)

    def _tracks_under(self, path):
        full = self._inside(path)
        starts = [full] if full else [r for r in self.roots() if os.path.isdir(r)]
        out = []
        seen = set()

        def walk(folder, depth):
            if len(out) > MAX_TRACKS or depth > MAX_DEPTH:
                return
            real = os.path.realpath(folder)
            if real in seen:  # symlink loops
                return
            seen.add(real)
            folders, songs, images = self._scan_quiet(folder)
            if songs:
                out.extend(self._tracks(songs, self._cover(folder, (folders, songs, images), depth=0))[0])
            for sub in folders:
                walk(sub, depth + 1)

        for start in starts:
            walk(start, 0)
        title = os.path.basename(full) if full else "Music"
        result = self._collection(full or "", title, _counts(0, len(out)), None, out[:MAX_TRACKS], [])
        result["trackCount"] = len(out)
        result["truncated"] = len(out) > MAX_TRACKS
        return result

    @staticmethod
    def _collection(path, title, subtitle, cover, tracks, folders):
        return {
            "type": "collection",
            "kind": "folder",
            "id": path,
            "path": path,
            "playlistId": None,
            "title": title,
            "subtitle": subtitle,
            "owned": False,
            "editable": False,
            "trackCount": len(tracks),
            "folderCount": len(folders),
            "truncated": False,
            "thumb": cover,
            "thumbLarge": cover,
            "tracks": tracks,
            "folders": folders,
        }
