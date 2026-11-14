"""Apple Music playlists kept in sync on the Pi, and their icons.

Each playlist saved from the music page is remembered here (with its Mopidy playlist and its icon).
A background thread brings the Pi's copy up to date:
* every few hours (settings.ini [upload] sync_hours) it reads the Apple Music playlist again, so songs you
  add or remove there follow;
* shortly after new music arrives on the Pi (uploads), it matches again without asking Apple.
Clear matches and your earlier picks go into the Pi's playlist; songs that need a pick or aren't on the
Pi yet are listed on the music page ("Get missing songs").

Icons: Apple's own picture for the playlist, or one you upload. Both are kept as 300x300 JPEGs in
~/.local/state/pirate-bluetooth/playlist-icons/, where the screen program finds them too.
"""

import io
import json
import logging
import os
import re
import threading
import time
import urllib.request

from . import playlist as pl

log = logging.getLogger("pirate_upload")

STATE = os.path.expanduser("~/.local/state/pirate-bluetooth")
REGISTRY = os.path.join(STATE, "playlists.json")
ICON_DIR = os.path.join(STATE, "playlist-icons")
ICON_SIZE = 300
MAX_ICON_BYTES = 10 * 2**20
REFETCH_MIN_S = 3600  # never read the same Apple playlist more often than this (except "Sync now")
AFTER_UPLOAD_S = float(os.environ.get("PIRATE_SYNC_QUIET", "45"))  # quiet time after the last upload (env: tests)


def _safe_id(playlist_id):
    if not re.fullmatch(r"pl\.(u-)?[A-Za-z0-9]+", playlist_id or ""):
        raise pl.PlaylistError("Unknown playlist.")
    return playlist_id


def icon_path(playlist_id):
    return os.path.join(ICON_DIR, _safe_id(playlist_id) + ".jpg")


def _image_kind(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def store_icon(playlist_id, data):
    """Keep a picture as the playlist's icon: square, 300x300 JPEG (as is, if PIL isn't there)."""
    if not data or len(data) > MAX_ICON_BYTES:
        raise pl.PlaylistError("That picture is empty or too big (10 MB at most).")
    kind = _image_kind(data)
    if not kind:
        raise pl.PlaylistError("That isn't a picture (JPEG, PNG, WebP or GIF).")
    path = icon_path(playlist_id)
    os.makedirs(ICON_DIR, exist_ok=True)
    try:
        from PIL import Image, ImageOps
    except ImportError:
        if kind != "jpeg":
            raise pl.PlaylistError("Only JPEG pictures work on this Pi (python3-pil is missing).")
        out = data
    else:
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:
            raise pl.PlaylistError("That picture couldn't be read.")
        img = ImageOps.exif_transpose(img).convert("RGB")
        img = ImageOps.fit(img, (ICON_SIZE, ICON_SIZE), method=Image.LANCZOS if hasattr(Image, "LANCZOS")
                           else Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        out = buf.getvalue()
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(out)
    os.replace(tmp, path)
    return path


def fetch_apple_icon(playlist_id, url, timeout=15):
    if not url or not pl._image_host_ok(url):
        raise pl.PlaylistError("Apple didn't give a picture for this playlist.")
    try:
        with urllib.request.build_opener().open(url, timeout=timeout) as resp:
            data = resp.read(MAX_ICON_BYTES + 1)
    except OSError as err:
        raise pl.PlaylistError(f"Couldn't download the playlist's picture from Apple ({err}).")
    return store_icon(playlist_id, data)


class Registry:
    """playlists.json: the playlists saved from the music page. Shared with the screen program."""

    def __init__(self, path=REGISTRY):
        self.path = path
        self._lock = threading.RLock()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) and isinstance(data.get("playlists"), list) else {"playlists": []}
        except (OSError, ValueError):
            return {"playlists": []}

    def _save(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def all(self):
        with self._lock:
            return self._load()["playlists"]

    def get(self, playlist_id):
        return next((p for p in self.all() if p.get("id") == playlist_id), None)

    def put(self, entry):
        with self._lock:
            data = self._load()
            data["playlists"] = [p for p in data["playlists"] if p.get("id") != entry["id"]] + [entry]
            self._save(data)

    def update(self, playlist_id, **changes):
        with self._lock:
            data = self._load()
            for p in data["playlists"]:
                if p.get("id") == playlist_id:
                    p.update(changes)
                    self._save(data)
                    return p
        return None

    def remove(self, playlist_id):
        with self._lock:
            data = self._load()
            data["playlists"] = [p for p in data["playlists"] if p.get("id") != playlist_id]
            self._save(data)
        try:
            os.remove(icon_path(playlist_id))
        except OSError:
            pass


def public(entry):
    """What the music page gets for one playlist."""
    has_icon = os.path.exists(icon_path(entry["id"]))
    return {k: entry.get(k) for k in ("id", "name", "url", "sync", "icon", "counts", "last_check", "last_fetch",
                                      "error", "apple_name", "note")} | {
        "has_icon": has_icon,
        "icon_version": os.stat(icon_path(entry["id"])).st_mtime_ns // 1000 if has_icon else 0,  # for the page's cache
        "missing": len(entry.get("missing") or []),
    }


class Syncer:
    def __init__(self, registry, index, picks, rpc, music_dir, sync_hours=6.0):
        self.registry = registry
        self.index = index
        self.picks = picks
        self.rpc = rpc
        self.music_dir = music_dir
        self.sync_hours = sync_hours
        self._lock = threading.Lock()  # one sync at a time
        self._library_changed_at = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="playlist-sync").start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def library_changed(self):
        """New music arrived (an upload): match the synced playlists again soon."""
        self._library_changed_at = time.monotonic()
        self._wake.set()

    def _loop(self):
        matched_for = 0.0
        while not self._stop.is_set():
            self._wake.wait(60)
            self._wake.clear()
            if self._stop.is_set():
                return
            changed = self._library_changed_at
            if changed > matched_for and time.monotonic() - changed < AFTER_UPLOAD_S:
                self._wake.wait(AFTER_UPLOAD_S - (time.monotonic() - changed))  # let the uploads finish
                self._wake.clear()
                if self._library_changed_at != changed:
                    continue  # more arrived meanwhile: wait again
            for entry in self.registry.all():
                if not entry.get("sync") or self._stop.is_set():
                    continue
                due = time.time() - (entry.get("last_fetch") or 0) >= self.sync_hours * 3600
                if due or changed > matched_for:
                    try:
                        self.sync(entry["id"], refetch=due)
                    except Exception:
                        log.exception("Syncing playlist %s failed", entry.get("name"))
            matched_for = max(matched_for, changed)

    def sync(self, playlist_id, refetch=False, force=False):
        """Bring one playlist's copy on the Pi up to date. Returns the updated entry."""
        with self._lock:
            entry = self.registry.get(playlist_id)
            if not entry:
                raise pl.PlaylistError("That playlist isn't saved on the Pi.")
            tracks = entry.get("tracks") or []
            error = ""
            if refetch:
                self.index.invalidate()  # also notice music that arrived another way (scp, a USB stick...)
            fetched = entry.get("last_fetch") or 0
            if refetch and (force or time.time() - fetched >= REFETCH_MIN_S or not tracks):
                try:
                    page = pl.parse_page(pl.fetch_page(entry.get("country", "us"), playlist_id))
                    tracks, fetched = page["tracks"], time.time()
                    note = (f"Apple's page lists {len(tracks)} of {page['reported']} songs; the rest can't be checked."
                            if page["reported"] and page["reported"] > len(tracks) else "")
                    self.registry.update(playlist_id, tracks=tracks, last_fetch=fetched, apple_name=page["name"],
                                         artwork=page.get("artwork", ""), note=note)
                    if entry.get("icon") == "apple" and page.get("artwork"):
                        try:
                            fetch_apple_icon(playlist_id, page["artwork"])
                        except pl.PlaylistError as err:
                            log.info("%s", err)
                except pl.PlaylistError as err:
                    error = f"Couldn't read the playlist from Apple Music: {err}"
                    log.info("Playlist %r: %s", entry.get("name"), error)
            rows = pl.match(tracks, self.index.entries(), self.picks.load())
            songs = [{"uri": r["match"]["uri"], "title": r["title"], "artist": r["artist"]}
                     for r in rows if r["status"] == "on_pi"]
            uris = [s["uri"] for s in songs]
            changes = {"counts": {s: sum(1 for r in rows if r["status"] == s) for s in ("on_pi", "choose", "missing")},
                       "last_check": time.time(), "error": error}
            if uris != entry.get("saved_uris") or not entry.get("mopidy_uri"):
                if songs:
                    _where, _n, name, uri = pl.save_playlist(entry["name"], songs, self.rpc, self.music_dir)
                    changes.update(saved_uris=uris, mopidy_uri=uri, name=name)
                    log.info("Playlist %r on the Pi now has %d songs", name, len(songs))
            unresolved = [r for r in rows if r["status"] != "on_pi"]
            old_links = {m["key"]: m.get("links") for m in entry.get("missing") or [] if m.get("links")}
            fresh = [r for r in unresolved if r["key"] not in old_links]
            if fresh:
                pl.store_links(fresh, entry.get("country", "us"))
            changes["missing"] = [{"key": r["key"], "n": r["n"], "title": r["title"], "artist": r["artist"],
                                   "album": r["album"], "status": r["status"],
                                   "links": r.get("links") or old_links.get(r["key"]) or {}} for r in unresolved]
            return self.registry.update(playlist_id, **changes)
