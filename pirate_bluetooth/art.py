"""Album art for the screen, fetched in the background and kept on disk.

* The Pi's own music: the images Mopidy knows for the song (Mopidy-Local pulls them out of the files),
  or else a cover picture in the song's folder (cover.jpg, folder.jpg, ...).
* Bluetooth: phones only send art over Bluetooth with newer BlueZ than Raspberry Pi OS Bookworm has, so
  when the Pi is online the cover is looked up in Apple's iTunes catalogue by artist and album (only those
  names are sent), and kept, so it also shows offline next time. [bluetooth] art_lookup = no turns it off.
"""

import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

from gi.repository import GLib

log = logging.getLogger(__name__)

COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "albumartsmall")
IMAGE_EXTS = (".jpg", ".jpeg", ".png")
MISS_RETRY_S = 6 * 3600  # an online lookup that found nothing is retried after this long
OFFLINE_RETRY_S = 120  # ...or after this long if the Pi was offline
ITUNES_URL = os.environ.get("PIRATE_ITUNES_URL", "https://itunes.apple.com/search")  # env: tests only
MAX_BYTES = 4 * 2**20


def _norm(text):
    text = unicodedata.normalize("NFKD", text or "").casefold()
    text = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", text)  # (Deluxe Edition), [Remastered] ...
    text = re.sub(r"\s+-\s+(single|ep)$", "", text)  # Apple Music's "Song - Single"
    return re.sub(r"[^0-9a-z]+", " ", text).strip()


def _similar(a, b):
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or a in b or b in a)


class ArtLoader:
    def __init__(self, cache_dir, on_ready, lookup_online=True, music_dirs=()):
        """on_ready(key, path_or_None) is called on the GLib main loop."""
        self.cache_dir = cache_dir
        self.lookup_online = lookup_online
        self.music_dirs = [os.path.realpath(os.path.expanduser(d)) for d in music_dirs]
        self._on_ready = on_ready
        self._jobs = queue.Queue()
        self._known = {}  # key -> path or None
        self._pending = set()
        self._missed = {}  # key -> monotonic time to retry after
        self._opener = urllib.request.build_opener()  # internet: honours a proxy if one is set
        self._local = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Mopidy on this Pi
        os.makedirs(cache_dir, exist_ok=True)
        threading.Thread(target=self._loop, daemon=True, name="album-art").start()

    # -- requests from the main loop ---------------------------------------------------------
    def known(self, key):
        """The art for key if it's already on disk (None if none or not looked up yet)."""
        return self._known.get(key)

    def for_mopidy(self, track_uri, image_urls_fn):
        """image_urls_fn() returns Mopidy's image URLs for the song (runs on the art thread)."""
        key = f"pi:{track_uri}"
        if key not in self._known and key not in self._pending:
            self._pending.add(key)
            self._jobs.put((key, self._mopidy_art, (track_uri, image_urls_fn)))
        return key

    def for_phone(self, artist, album, title):
        key = "phone:" + hashlib.sha1(f"{_norm(artist)}|{_norm(album) or _norm(title)}".encode()).hexdigest()
        if key in self._known or key in self._pending:
            return key
        path = self._cache_path(key)
        if os.path.exists(path):
            self._known[key] = path
        elif artist and (album or title) and self.lookup_online and time.monotonic() >= self._missed.get(key, 0):
            self._pending.add(key)
            self._jobs.put((key, self._itunes_art, (artist, album, title)))
        return key

    # -- worker ---------------------------------------------------------------------------------
    def _loop(self):
        while True:
            key, fn, args = self._jobs.get()
            if key in self._known:
                path = self._known[key]
            else:
                try:
                    path = fn(key, *args)
                except Exception:
                    log.exception("Album art lookup failed")
                    path = None
                if path or not key.startswith("phone:"):
                    self._known[key] = path
            GLib.idle_add(self._deliver, key, path)

    def _deliver(self, key, path):
        self._pending.discard(key)
        self._on_ready(key, path)
        return False


    def _cache_path(self, key, ext=".jpg"):
        return os.path.join(self.cache_dir, hashlib.sha1(key.encode()).hexdigest() + ext)

    def _download(self, url, key):
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
        path = self._cache_path(key, ext if ext in IMAGE_EXTS else ".jpg")
        if os.path.exists(path):
            return path
        if url.startswith("file://"):
            local = urllib.parse.unquote(urllib.parse.urlparse(url).path)
            return local if os.path.exists(local) else None
        host = urllib.parse.urlparse(url).hostname or ""
        opener = self._local if host in ("127.0.0.1", "localhost", "::1") else self._opener
        with opener.open(url, timeout=8) as resp:
            data = resp.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES or not data:
            return None
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return path

    def _mopidy_art(self, key, track_uri, image_urls_fn):
        for url in image_urls_fn():
            try:
                path = self._download(url, f"{key}|{url}")
            except OSError as err:
                log.debug("Couldn't fetch %s: %s", url, err)
                continue
            if path:
                return path
        return self._folder_cover(track_uri)

    def _folder_cover(self, track_uri):
        """cover.jpg & co. next to the song - but not for loose songs right in the Music folder."""
        path = None
        if track_uri.startswith("file://"):
            path = urllib.parse.unquote(urllib.parse.urlparse(track_uri).path)
        elif track_uri.startswith("local:track:") and self.music_dirs:
            path = os.path.join(self.music_dirs[0], urllib.parse.unquote(track_uri[len("local:track:"):]))
        if not path:
            return None
        folder = os.path.dirname(os.path.realpath(path))
        if folder in self.music_dirs or not os.path.isdir(folder):
            return None
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return None
        pictures = [n for n in names if n.lower().endswith(IMAGE_EXTS) and not n.startswith(".")]
        for want in COVER_NAMES:
            for n in pictures:
                if os.path.splitext(n)[0].lower() == want:
                    return os.path.join(folder, n)
        return os.path.join(folder, pictures[0]) if len(pictures) == 1 else None

    def _itunes_art(self, key, artist, album, title):
        try:
            url = self._itunes_find(artist, album, title)
        except (OSError, ValueError) as err:
            log.info("Album art lookup didn't work (offline?): %s", err)
            self._missed[key] = time.monotonic() + OFFLINE_RETRY_S
            return None
        if not url:
            log.info("No album art found for %s - %s", artist, album or title)
            self._missed[key] = time.monotonic() + MISS_RETRY_S
            return None
        try:
            data = self._fetch(url, max_bytes=MAX_BYTES)
        except OSError as err:
            self._missed[key] = time.monotonic() + OFFLINE_RETRY_S
            log.info("Couldn't download album art: %s", err)
            return None
        path = self._cache_path(key)
        with open(path + ".tmp", "wb") as fh:
            fh.write(data)
        os.replace(path + ".tmp", path)
        log.info("Album art for %s - %s saved", artist, album or title)
        return path

    def _fetch(self, url, max_bytes):
        with self._opener.open(url, timeout=8) as resp:
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise OSError("too big")
        return data

    def _itunes_find(self, artist, album, title):
        searches = []
        if album:
            searches.append(("album", f"{artist} {re.sub(r' - (Single|EP)$', '', album)}"))
        if title:
            searches.append(("song", f"{artist} {title}"))
        for entity, term in searches:
            query = urllib.parse.urlencode({"term": term, "media": "music", "entity": entity, "limit": 10})
            results = json.loads(self._fetch(f"{ITUNES_URL}?{query}", max_bytes=2**20)).get("results", [])
            for r in results:
                if not _similar(r.get("artistName"), artist):
                    continue
                if entity == "album" and not _similar(r.get("collectionName"), album):
                    continue
                if entity == "song" and not (_similar(r.get("trackName"), title)
                                             or (album and _similar(r.get("collectionName"), album))):
                    continue
                art = r.get("artworkUrl100") or r.get("artworkUrl60")
                if art:
                    return re.sub(r"/\d+x\d+(bb)?\.(jpg|png)$", r"/600x600bb.jpg", art)
        return None
