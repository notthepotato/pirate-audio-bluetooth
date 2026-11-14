"""Songs from SoundCloud links, for the music page: the files artists offer for download.

Paste a SoundCloud link (a song, a playlist or album, or the on.soundcloud.com link the app's Share button
makes). For each song:

* The artist switched on SoundCloud's download button: the Pi downloads the artist's own file (often a WAV
  or their full-quality master) into ~/Music/SoundCloud/<Artist> - <Title>/ with its cover, and Mopidy's
  library picks it up. SoundCloud only gives these files to signed-in listeners, so the music page asks
  once for your SoundCloud sign-in. It stays on the Pi (~/.config/pirate-bluetooth/soundcloud-token).
* No download offered: the page shows the artist's own link instead (their Buy / Free download page, if
  they set one) and store searches. Streams aren't recorded.

SoundCloud doesn't give out API keys any more, so this talks to the same API the SoundCloud website uses,
with the website's own client id (read from the website). If SoundCloud changes that, the page says so.

Standard library only.
"""

import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .playlist import AUDIO_EXTS

log = logging.getLogger("pirate_upload")

WEB = os.environ.get("PIRATE_SC_WEB", "https://soundcloud.com")  # env: tests only
API = os.environ.get("PIRATE_SC_API", "https://api-v2.soundcloud.com")  # env: tests only
SITE_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}
SHORT_HOSTS = {"on.soundcloud.com", "soundcloud.app.goo.gl", "snd.sc"}
TEST_HOSTS = set(filter(None, os.environ.get("PIRATE_SC_TEST_HOSTS", "").split(",")))  # tests only
HOME = os.path.expanduser("~")
TOKEN_FILE = os.path.join(HOME, ".config", "pirate-bluetooth", "soundcloud-token")
CACHE_FILE = os.path.join(HOME, ".local", "state", "pirate-bluetooth", "soundcloud.json")
USER_AGENT = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CLIENT_ID_HOURS = 12
MAX_TRACKS = 500
CONTENT_TYPES = {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/mpeg": ".mp3",
                 "audio/mp3": ".mp3", "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/aiff": ".aiff",
                 "audio/x-aiff": ".aiff", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
                 "audio/ogg": ".ogg", "audio/opus": ".opus"}


class SoundCloudError(Exception):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code  # "signin": the sign-in is missing or expired


# == links ==========================================================================================
def find_link(text):
    """The SoundCloud link in whatever was pasted (the app's Share text has words around it)."""
    m = re.search(r"https?://[^\s<>\"']+", text or "")
    if not m:
        raise SoundCloudError("Paste a SoundCloud link: it starts with https://soundcloud.com or https://on.soundcloud.com.")
    url = m.group(0).rstrip(").,;!")
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in SITE_HOSTS | SHORT_HOSTS | TEST_HOSTS:
        raise SoundCloudError("That isn't a SoundCloud link.")
    return url


def _request(url, headers=None, data=None, method=None):
    h = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
    h.update(headers or {})
    return urllib.request.Request(url, headers=h, data=data, method=method)


def _read(resp, limit):
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise SoundCloudError("SoundCloud sent more than expected.")
    return data


class _Arrived(Exception):
    def __init__(self, url):
        super().__init__(url)
        self.url = url


class _StopAtSoundCloud(urllib.request.HTTPRedirectHandler):
    """Follow a share link's redirects only until they reach soundcloud.com (no need to load the page)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = (urllib.parse.urlparse(newurl).hostname or "").lower()
        if host in SITE_HOSTS or (host in TEST_HOSTS and "/short/" not in newurl):
            raise _Arrived(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def canonical(url, timeout=20):
    """https://soundcloud.com/<artist>/<song or sets/...>, following share links to where they lead."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in SHORT_HOSTS or (host in TEST_HOSTS and "/short/" in url):
        try:
            with urllib.request.build_opener(_StopAtSoundCloud).open(_request(url), timeout=timeout) as resp:
                url = resp.geturl()
        except _Arrived as arrived:
            url = arrived.url
        except (urllib.error.URLError, OSError) as err:
            raise SoundCloudError(f"Couldn't open that share link ({getattr(err, 'reason', err)}).")
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    parts = urllib.parse.urlparse(url)
    path = re.sub(r"/+$", "", parts.path)
    if host in SITE_HOSTS:
        return "https://soundcloud.com" + path
    if host in TEST_HOSTS:
        return f"{parts.scheme}://{parts.netloc}{path}"
    raise SoundCloudError("That share link didn't lead to SoundCloud.")


# == SoundCloud's web API ===============================================================================
class Client:
    def __init__(self, timeout=20):
        self.timeout = timeout
        self._lock = threading.Lock()

    # -- the website's client id --
    def client_id(self, fresh=False):
        with self._lock:
            cache = {}
            try:
                with open(CACHE_FILE, encoding="utf-8") as fh:
                    cache = json.load(fh)
            except (OSError, ValueError):
                pass
            if not fresh and cache.get("client_id") and time.time() - cache.get("at", 0) < CLIENT_ID_HOURS * 3600:
                return cache["client_id"]
            cid = self._discover()
            try:
                os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
                with open(CACHE_FILE, "w", encoding="utf-8") as fh:
                    json.dump({"client_id": cid, "at": time.time()}, fh)
            except OSError:
                pass
            return cid

    def _discover(self):
        try:
            with urllib.request.urlopen(_request(WEB + "/"), timeout=self.timeout) as resp:
                page = _read(resp, 4 * 2**20).decode("utf-8", "replace")
            scripts = re.findall(r'<script[^>]+src="([^"]+\.js)"', page)
            for src in list(reversed(scripts))[:12]:  # the id is in one of the last scripts
                with urllib.request.urlopen(_request(urllib.parse.urljoin(WEB + "/", src)), timeout=self.timeout) as resp:
                    js = _read(resp, 12 * 2**20).decode("utf-8", "replace")
                m = (re.search(r'client_id\s*[:=]\s*"([0-9A-Za-z]{32})"', js)
                     or re.search(r'client_id=([0-9A-Za-z]{32})', js))
                if m:
                    return m.group(1)
        except (urllib.error.URLError, OSError) as err:
            raise SoundCloudError(f"Couldn't reach SoundCloud ({getattr(err, 'reason', err)}). Is the Pi online?")
        raise SoundCloudError("Couldn't read SoundCloud's web player settings (SoundCloud may have changed its website).")

    def api(self, path, params=None, token=None, _again=True):
        query = dict(params or {}, client_id=self.client_id())
        url = f"{API}{path}?{urllib.parse.urlencode(query)}"
        headers = {"Accept": "application/json", "Origin": "https://soundcloud.com", "Referer": "https://soundcloud.com/"}
        if token:
            headers["Authorization"] = f"OAuth {token}"
        try:
            with urllib.request.urlopen(_request(url, headers), timeout=self.timeout) as resp:
                return json.loads(_read(resp, 16 * 2**20))
        except urllib.error.HTTPError as err:
            if err.code in (401, 403) and _again and not token:
                self.client_id(fresh=True)  # the website's id changes now and then
                return self.api(path, params, token, _again=False)
            if err.code == 404:
                raise SoundCloudError("SoundCloud doesn't know that link (deleted, private or mistyped?).")
            if err.code == 429:
                raise SoundCloudError("SoundCloud says too many requests right now. Try again in a few minutes.")
            if err.code == 401 and token:
                raise SoundCloudError("SoundCloud didn't accept the sign-in (it may have expired). Sign in again "
                                      "on this page.", code="signin")
            raise SoundCloudError(f"SoundCloud answered {err.code}.")
        except (urllib.error.URLError, OSError) as err:
            raise SoundCloudError(f"Couldn't reach SoundCloud ({getattr(err, 'reason', err)}). Is the Pi online?")
        except ValueError:
            raise SoundCloudError("SoundCloud sent something unexpected.")

    def resolve(self, url):
        """{"kind": "track"|"playlist", "title", "tracks": [SoundCloud's track data, ...]}"""
        data = self.api("/resolve", {"url": url})
        kind = data.get("kind")
        if kind == "track":
            return {"kind": "track", "title": data.get("title", ""), "tracks": [data]}
        if kind == "playlist":
            tracks = [t for t in data.get("tracks") or [] if isinstance(t, dict) and t.get("id")][:MAX_TRACKS]
            missing = [t["id"] for t in tracks if "title" not in t]  # only the first few come complete
            full = {}
            for i in range(0, len(missing), 50):
                batch = self.api("/tracks", {"ids": ",".join(str(x) for x in missing[i:i + 50])})
                for t in batch if isinstance(batch, list) else []:
                    full[t.get("id")] = t
            tracks = [full.get(t["id"], t) for t in tracks]
            return {"kind": "playlist", "title": data.get("title", ""),
                    "tracks": [t for t in tracks if "title" in t]}
        if kind == "user":
            raise SoundCloudError("That's an artist's page. Paste a link to a song, an album or a playlist.")
        raise SoundCloudError("That SoundCloud link isn't a song, an album or a playlist.")

    def me(self, token):
        data = self.api("/me", token=token)
        return data.get("username") or data.get("permalink") or "you"

    def download_url(self, track_id, token):
        """Where the artist's own file is (SoundCloud only tells signed-in listeners)."""
        try:
            data = self.api(f"/tracks/{int(track_id)}/download", token=token, _again=False)
        except SoundCloudError as err:
            if err.code == "signin":
                raise
            if "answered 403" in str(err) or "doesn't know" in str(err):
                raise SoundCloudError("The artist's download isn't available any more.")
            raise
        url = data.get("redirectUri") if isinstance(data, dict) else None
        if not url:
            raise SoundCloudError("SoundCloud didn't say where the download is.")
        return url


# == what the music page shows ===========================================================================
def summarize(t):
    user = (t.get("user") or {}).get("username", "")
    artist = ((t.get("publisher_metadata") or {}).get("artist") or user or "").strip()
    title = (t.get("title") or "").strip()
    if artist and title.lower().startswith(artist.lower() + " - "):
        title = title[len(artist) + 3:].strip()
    art = t.get("artwork_url") or (t.get("user") or {}).get("avatar_url") or ""
    offered = bool(t.get("downloadable"))
    return {"id": t.get("id"), "title": title or "Untitled", "artist": artist, "uploader": user,
            "url": t.get("permalink_url", ""), "artwork": art.replace("-large.", "-t500x500."),
            "duration": t.get("full_duration") or t.get("duration") or 0,
            "downloadable": offered and t.get("has_downloads_left") is not False,
            "limit_reached": offered and t.get("has_downloads_left") is False,
            "buy_url": t.get("purchase_url") or "", "buy_title": (t.get("purchase_title") or "").strip()}


class Account:
    """Your SoundCloud sign-in, kept on the Pi (only readable by you) and never sent back to the page."""

    def __init__(self, path=TOKEN_FILE):
        self.path = path

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if data.get("token") else {}
        except (OSError, ValueError):
            return {}

    def token(self):
        return self.load().get("token", "")

    def user(self):
        return self.load().get("user", "")

    def save(self, token, user):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"token": token, "user": user, "at": int(time.time())}, fh)

    def clear(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


def clean_token(text):
    """The sign-in pasted from the browser: the oauth_token cookie's value (maybe with "OAuth " in front)."""
    token = (text or "").strip().strip('"').removeprefix("OAuth ").strip()
    token = token.split("oauth_token=", 1)[-1].split(";", 1)[0].strip()
    if not re.fullmatch(r"[0-9A-Za-z._-]{10,200}", token):
        raise SoundCloudError("That doesn't look like a SoundCloud sign-in (it usually starts with 2-).")
    return token


def safe_name(text, limit=120):
    text = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    while len(text.encode("utf-8")) > limit:
        text = text[:-1]
    return text.strip(" .") or "Untitled"


def _filename(resp, url):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    if m:
        return urllib.parse.unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    if m:
        return m.group(1).strip()
    return urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])


class Downloads:
    """Downloads one song at a time in the background; the page asks how they're going."""

    def __init__(self, client, account, music_dir, keep_free_bytes=0, on_saved=None):
        self.client = client
        self.account = account
        self.root = Path(os.path.expanduser(music_dir))
        self.keep_free = keep_free_bytes
        self.on_saved = on_saved
        self.jobs = {}  # track id -> {"title", "artist", "state", "done", "total", "error", "path"}
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, tracks):
        added = 0
        with self._lock:
            for t in tracks:
                job = self.jobs.get(t["id"])
                if job and job["state"] in ("queued", "downloading"):
                    continue
                self.jobs[t["id"]] = {"title": t["title"], "artist": t["artist"], "state": "queued",
                                      "done": 0, "total": 0, "error": "", "path": ""}
                self._queue.put(t)
                added += 1
        return added

    def status(self):
        with self._lock:
            return {str(k): dict(v) for k, v in self.jobs.items()}

    def _set(self, tid, **changes):
        with self._lock:
            self.jobs[tid].update(changes)

    def _worker(self):
        while True:
            t = self._queue.get()
            try:
                self._one(t)
            except SoundCloudError as err:
                self._set(t["id"], state="failed", error=str(err), code=err.code)
            except OSError as err:
                self._set(t["id"], state="failed", error=f"Couldn't save it: {err.strerror or err}")
            except Exception:
                log.exception("SoundCloud download of %s failed", t.get("title"))
                self._set(t["id"], state="failed", error="Something went wrong on the Pi (pirate-bluetooth logs upload).")

    def _one(self, t):
        tid = t["id"]
        token = self.account.token()
        if not token:
            raise SoundCloudError("Sign in to SoundCloud on this page first.", code="signin")
        self._set(tid, state="downloading")
        url = self.client.download_url(tid, token)
        name = safe_name(f"{t['artist']} - {t['title']}" if t["artist"] else t["title"])
        folder = self.root / "SoundCloud" / name
        with urllib.request.urlopen(_request(url), timeout=60) as resp:  # no sign-in sent here: it's a file host
            original = _filename(resp, resp.geturl())
            ext = os.path.splitext(original)[1].lower()
            if ext not in AUDIO_EXTS:
                ext = CONTENT_TYPES.get(resp.headers.get_content_type(), ext)
            if ext not in AUDIO_EXTS:
                raise SoundCloudError(f"The artist's file is {original or 'not a music file'}, which the Pi can't play.")
            total = int(resp.headers.get("Content-Length") or 0)
            self._set(tid, total=total)
            final = folder / f"{name}{ext}"
            if final.exists() and (not total or final.stat().st_size == total):
                self._set(tid, state="exists", path=str(final.relative_to(self.root)), done=total)
                return
            folder.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(folder).free
            if total and free - total < self.keep_free:
                raise SoundCloudError("Not enough free space on the Pi.")
            tmp = folder / f".{name}{ext}.part"
            done = 0
            try:
                with open(tmp, "wb") as out:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        self._set(tid, done=done)
                if total and done != total:
                    raise SoundCloudError("The download stopped halfway. Try again.")
                os.replace(tmp, final)
            finally:
                if tmp.exists():
                    tmp.unlink()
        if t.get("artwork") and not any((folder / c).exists() for c in ("cover.jpg", "cover.png")):
            try:
                with urllib.request.urlopen(_request(t["artwork"]), timeout=20) as resp:
                    kind = resp.headers.get_content_type()
                    data = _read(resp, 8 * 2**20)
                if kind in ("image/jpeg", "image/png"):
                    (folder / ("cover.png" if kind == "image/png" else "cover.jpg")).write_bytes(data)
            except (urllib.error.URLError, OSError, SoundCloudError) as err:
                log.info("No cover for %s: %s", name, err)
        rel = str(final.relative_to(self.root))
        log.info("Downloaded %s from SoundCloud (the artist's file)", rel)
        self._set(tid, state="done", path=rel, done=done)
        if self.on_saved:
            self.on_saved()
