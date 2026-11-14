"""Music page for the Pi: drag-and-drop upload, and download back to a computer (standard library only).

Serves a small web page (default http://<pi>:6681) where you can drop songs or whole album folders.
They are saved into ~/Music - the folder Pimoroni's Mopidy setup already plays from - and Mopidy's
local library is refreshed in the background afterwards, without restarting or interrupting Mopidy.

Uploads are streamed straight to disk (no size limit in memory), written under a hidden temporary
name and only renamed into place once complete, so Mopidy never sees half-written files.

The same page browses the Music folder and downloads single songs, or whole folders (or everything) as
a .zip that's put together while it downloads (stored, not compressed: music files don't shrink).
"""

import argparse
import base64
import configparser
import hmac
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import mopidyconf

log = logging.getLogger("pirate_upload")

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav", ".aif", ".aiff",
              ".wma", ".ape", ".wv", ".mka", ".alac"}
EXTRA_EXTS = {".jpg", ".jpeg", ".png", ".m3u", ".m3u8"}  # album covers and playlists
REFUSED = {
    ".m4p": "This is a copy-protected iTunes/Apple Music file, so it can't play on the Pi.",
}
CHUNK = 1024 * 1024
DRAIN_LIMIT = 16 * 1024 * 1024  # on a refusal, read up to this much so the browser gets the reason
WEB_DIR = Path(__file__).with_name("web")

DEFAULTS = {
    "upload": {
        "enabled": "yes",
        "port": "6681",
        "music_dir": "~/Music",
        "password": "",
        "max_file_mb": "2048",
        "keep_free_mb": "500",
        "scan_delay": "8",
        "scan_command": "auto",
    },
}


class UploadError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ==== where files go ============================================================================
def clean_relative_path(raw: str):
    """Turn the browser's relative path ("Artist/Album/01 Song.mp3") into safe path parts.

    Rejects anything that could escape the music folder or create hidden files.
    """
    if not raw or "\x00" in raw:
        raise UploadError(400, "Missing file name.")
    text = unicodedata.normalize("NFC", raw.replace("\\", "/"))
    parts = []
    for part in text.split("/"):
        part = "".join(ch for ch in part if ch.isprintable()).strip()
        if part in ("", "."):
            continue
        if part == ".." or part.startswith("."):
            raise UploadError(400, f"Not allowed in a file name: {part!r}")
        if len(part.encode("utf-8")) > 200:
            raise UploadError(400, "A file or folder name is too long.")
        parts.append(part)
    if not parts:
        raise UploadError(400, "Missing file name.")
    if len(parts) > 8:
        raise UploadError(400, "Folders are nested too deeply (8 levels max).")
    return parts


def file_kind(name: str):
    ext = os.path.splitext(name)[1].lower()
    if ext in REFUSED:
        raise UploadError(415, REFUSED[ext])
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in EXTRA_EXTS:
        return "extra"
    raise UploadError(415, "Not a music file (or an album cover/playlist), so it was skipped.")


class MusicStore:
    def __init__(self, music_dir, max_file_bytes, keep_free_bytes):
        self.root = Path(os.path.expanduser(music_dir))
        self.max_file_bytes = max_file_bytes
        self.keep_free_bytes = keep_free_bytes
        self._lock = threading.Lock()  # guards choosing names, so two uploads never pick the same one
        self._reserved = set()  # final paths of uploads still in progress
        self._recent_cache = (0.0, [])

    def _inside_root(self, path: Path) -> bool:
        root = os.path.realpath(self.root)
        real = os.path.realpath(path)
        return real == root or real.startswith(root + os.sep)

    def free_bytes(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free

    def check(self, raw_path: str, length: int):
        """Validate an upload before any bytes are sent. Returns (parts, kind, parent)."""
        parts = clean_relative_path(raw_path)
        kind = file_kind(parts[-1])
        if length < 0:
            raise UploadError(411, "The browser didn't say how big the file is.")
        if length > self.max_file_bytes:
            raise UploadError(413, f"Too big: files can be up to {self.max_file_bytes // 2**20} MB.")
        if self.free_bytes() - length < self.keep_free_bytes:
            raise UploadError(507, "Not enough free space on the Pi for this file.")
        parent = self.root.joinpath(*parts[:-1])
        if not self._inside_root(parent):
            raise UploadError(400, "That folder is outside the music folder.")
        return parts, kind, parent

    def already_there(self, raw_path: str, length: int) -> bool:
        parts, _kind, parent = self.check(raw_path, length)
        target = parent / parts[-1]
        return target.is_file() and target.stat().st_size == length

    def save(self, raw_path: str, length: int, read) -> dict:
        """Stream `length` bytes from read(n) into the music folder. Returns what happened."""
        parts, kind, parent = self.check(raw_path, length)
        parent.mkdir(parents=True, exist_ok=True)
        if not self._inside_root(parent):  # a symlink inside Music could point elsewhere
            raise UploadError(400, "That folder is outside the music folder.")

        name = parts[-1]
        with self._lock:
            target = parent / name
            if (target not in self._reserved and target.is_file()
                    and target.stat().st_size == length):
                skip = True
            else:
                skip = False
                target = self._free_name(parent, name)
                self._reserved.add(target)  # no other upload may pick this name until we're done
        if skip:
            self._drain(length, read)
            return {"status": "exists", "path": self._rel(target),
                    "message": "Already on the Pi, so it was skipped."}
        # A hidden name while writing: Mopidy ignores dotfiles, so it never sees a half-written song.
        tmp = parent / f".{target.name}.uploading-{os.getpid()}-{threading.get_ident()}"
        try:
            remaining = length
            with open(tmp, "wb") as fh:
                while remaining > 0:
                    chunk = read(min(CHUNK, remaining))
                    if not chunk:
                        raise UploadError(400, "The upload was interrupted.")
                    fh.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp, target)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        finally:
            with self._lock:
                self._reserved.discard(target)
        self._recent_cache = (0.0, [])  # the song list just changed
        log.info("Saved %s (%d bytes)", target, length)
        return {"status": "saved", "kind": kind, "path": self._rel(target)}

    @staticmethod
    def _drain(length, read):
        remaining = length
        while remaining > 0:
            chunk = read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _free_name(self, parent: Path, name: str) -> Path:
        stem, ext = os.path.splitext(name)
        candidate, n = parent / name, 2
        while candidate.exists() or candidate in self._reserved:
            candidate = parent / f"{stem} ({n}){ext}"
            n += 1
        return candidate

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    # -- downloads ----------------------------------------------------------------------------------
    def resolve(self, raw_path: str, folder: bool) -> Path:
        """A file or folder inside the music folder ("" = the music folder itself), or UploadError 404."""
        parts = clean_relative_path(raw_path) if raw_path.strip("/ ") else []
        target = self.root.joinpath(*parts)
        ok = self._inside_root(target) and (target.is_dir() if folder else target.is_file())
        if not ok or (not folder and os.path.splitext(target.name)[1].lower() not in AUDIO_EXTS | EXTRA_EXTS):
            raise UploadError(404, "That isn't on the Pi (any more?).")
        return target

    def _music_files(self, folder: Path):
        """(path, size) of the songs, covers and playlists under a folder, in a stable order."""
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = sorted((d for d in dirnames if not d.startswith(".")), key=_sort_key)
            for f in sorted(filenames, key=_sort_key):
                if f.startswith(".") or os.path.splitext(f)[1].lower() not in AUDIO_EXTS | EXTRA_EXTS:
                    continue
                p = Path(dirpath) / f
                try:
                    if p.is_file() and self._inside_root(p):
                        yield p, p.stat().st_size
                except OSError:
                    pass

    def listing(self, raw_path: str) -> dict:
        """What's in one folder: its subfolders (with how many songs and how big) and its files."""
        folder = self.resolve(raw_path, folder=True)
        folders, files = [], []
        total_songs = total_bytes = 0
        try:
            entries = sorted(os.scandir(folder), key=lambda e: _sort_key(e.name))
        except OSError as err:
            raise UploadError(500, f"Couldn't read that folder: {err.strerror or err}")
        for e in entries:
            if e.name.startswith("."):
                continue
            path = Path(e.path)
            try:
                if e.is_dir() and self._inside_root(path):
                    songs = size = 0
                    for p, n in self._music_files(path):
                        size += n
                        songs += os.path.splitext(p.name)[1].lower() in AUDIO_EXTS
                    if size:  # folders with no music (or covers/playlists) in them aren't shown
                        folders.append({"name": e.name, "songs": songs, "size": size})
                    total_songs, total_bytes = total_songs + songs, total_bytes + size
                elif e.is_file() and self._inside_root(path):
                    ext = os.path.splitext(e.name)[1].lower()
                    if ext in AUDIO_EXTS | EXTRA_EXTS:
                        size = e.stat().st_size
                        files.append({"name": e.name, "size": size, "kind": "audio" if ext in AUDIO_EXTS else "extra"})
                        total_songs, total_bytes = total_songs + (ext in AUDIO_EXTS), total_bytes + size
            except OSError:
                continue
        rel = self._rel(folder)
        return {"path": "" if rel == "." else rel, "folders": folders, "files": files,
                "songs": total_songs, "size": total_bytes}

    def write_zip(self, folder: Path, out) -> int:
        """Write a .zip of a folder to out (a socket: written as it goes, never all in memory)."""
        top = folder.name or "Music"
        count = 0
        with zipfile.ZipFile(_Unseekable(out), "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for path, _size in self._music_files(folder):
                arcname = "/".join((top, *path.relative_to(folder).parts))
                try:
                    info = zipfile.ZipInfo.from_file(path, arcname, strict_timestamps=False)
                    src = open(path, "rb")
                except OSError as err:
                    log.warning("Leaving %s out of the zip: %s", path, err.strerror or err)
                    continue
                info.compress_type = zipfile.ZIP_STORED
                with src, zf.open(info, "w") as dest:
                    shutil.copyfileobj(src, dest, CHUNK)
                count += 1
        return count

    def recent(self, limit=12):
        """The newest music files (cached for a few seconds; walking a big library on a Pi Zero isn't free)."""
        at, cached = self._recent_cache
        if time.monotonic() - at < 5:
            return cached
        found = []
        if self.root.is_dir():
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for f in filenames:
                    if f.startswith(".") or os.path.splitext(f)[1].lower() not in AUDIO_EXTS:
                        continue
                    p = Path(dirpath) / f
                    try:
                        found.append((p.stat().st_mtime, p))
                    except OSError:
                        pass
        found.sort(reverse=True)
        result = {"count": len(found),
                  "recent": [{"path": self._rel(p), "added": int(m)} for m, p in found[:limit]]}
        self._recent_cache = (time.monotonic(), result)
        return result


def _sort_key(name: str):
    """Natural order: "2 Song" before "10 Song", ignoring case."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", name.casefold()) if t]


class _Unseekable:
    """A socket as zipfile sees it: written in order, no seeking (it then adds sizes after each file)."""

    def __init__(self, out):
        self.out = out

    def write(self, data):
        self.out.write(data)
        return len(data)

    def flush(self):
        pass


class _Chunked:
    """HTTP/1.1 chunked body. If the download breaks off, the last chunk is never sent, so the browser
    reports a failed download instead of keeping a cut-off .zip."""

    def __init__(self, out, size=256 * 1024):
        self.out, self.size, self.buf = out, size, bytearray()

    def write(self, data):
        self.buf += data
        if len(self.buf) >= self.size:
            self._send()

    def flush(self):
        pass

    def _send(self):
        if self.buf:
            self.out.write(b"%X\r\n" % len(self.buf) + bytes(self.buf) + b"\r\n")
            self.buf.clear()

    def finish(self):
        self._send()
        self.out.write(b"0\r\n\r\n")


def content_disposition(name: str) -> str:
    """attachment; with a plain-ASCII fallback name for old browsers and the real (UTF-8) name for the rest."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    plain = re.sub(r'[^A-Za-z0-9 ._()\-]', "_", plain).strip() or "download"
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


# ==== refreshing Mopidy's library ===================================================================
# Mopidy's service command and config files (shared with the screen program and the CLI)
_mopidy_execstart = mopidyconf.service_command
mopidy_scan_command = mopidyconf.scan_command
mopidy_config_files = mopidyconf.config_files


def mopidy_library_enabled(files):
    """Is Mopidy-Local (Mopidy's music library) switched on in Mopidy's settings? None if unreadable.

    Pimoroni's installer saves Mopidy's settings as they were at install time into defaults.conf. If
    Mopidy-Local wasn't working at that exact moment, that file says "[local] enabled = false" for good."""
    return mopidyconf.extension_enabled(files, "local")


LIBRARY_OFF = ("Mopidy's music library (Mopidy-Local) is switched off in Mopidy's settings, so new songs only "
               "show in Iris under Browse → Files. To switch it on, run this on the Pi: pirate-bluetooth library on")


class LibraryScanner:
    """Runs Mopidy-Local's scan a few seconds after the last upload (one scan per batch, not per file)."""

    def __init__(self, command, delay, config_files=None):
        self.command = command
        self.delay = delay
        self.config_files = config_files
        self._off_from_config = False
        self.state = "idle" if command else "unavailable"
        self.message = "" if command else "Mopidy wasn't found, so its library isn't refreshed automatically."
        self.finished_at = 0
        self._timer = None
        self._running = False
        self._again = False
        self._lock = threading.Lock()

    def library_off(self) -> bool:
        return bool(self.config_files) and mopidy_library_enabled(self.config_files) is False

    def request(self, delay=None):
        if not self.command:
            return
        if self.library_off():
            with self._lock:
                self.state, self.message, self._off_from_config = "off", LIBRARY_OFF, True
            return
        with self._lock:
            if self._running:
                self._again = True
                return
            if self._timer:
                self._timer.cancel()
            self.state = "waiting"
            self._timer = threading.Timer(self.delay if delay is None else delay, self._run)
            self._timer.daemon = True
            self._timer.start()

    def _run(self):
        with self._lock:
            self._timer = None
            self._running = True
            self._again = False
            self.state = "running"
            self.message = "Adding new music to Mopidy's library…"
        log.info("Running %s", " ".join(self.command))
        state = "done"
        try:
            res = subprocess.run(self.command, capture_output=True, text=True, timeout=3600)
            output = f"{res.stdout}\n{res.stderr}"
            unreadable = [(urllib.parse.unquote(uri.rsplit("/", 1)[-1]), why.strip())
                          for uri, why in re.findall(r"Failed scanning (file://\S+?):? (.+)", output)]
            for name, why in unreadable:
                log.warning("Mopidy couldn't read %s: %s", name, why)
            if "disabled extension local" in output:  # switched off by Mopidy itself, not in the files
                state, message = "off", LIBRARY_OFF
                self._off_from_config = False
            elif res.returncode != 0:
                tail = output.strip().splitlines()[-1:] or [""]
                state, message = "failed", f"Library refresh failed: {tail[0].strip()}"
            elif unreadable:
                shown = ", ".join(f"{name} ({why})" for name, why in unreadable[:3])
                more = f" and {len(unreadable) - 3} more" if len(unreadable) > 3 else ""
                message = f"Mopidy's library is up to date, but Mopidy couldn't read: {shown}{more}."
            else:
                message = "Mopidy's library is up to date."
        except (OSError, subprocess.TimeoutExpired) as err:
            state, message = "failed", f"Library refresh failed: {err}"
        (log.info if state == "done" else log.warning)(message)
        with self._lock:
            self._running = False
            self.state = state
            self.message = message
            self.finished_at = int(time.time())
            again = self._again
        if again:
            self.request()

    def status(self):
        with self._lock:
            state, message = self.state, self.message
        if state not in ("waiting", "running") and self.command and self.library_off():
            state, message = "off", LIBRARY_OFF
        elif state == "off" and self._off_from_config and not self.library_off():
            state, message = "idle", ""  # switched back on in Mopidy's settings since
        return {"state": state, "message": message, "finished_at": self.finished_at}


# ==== HTTP ============================================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "PirateUpload/1.0"
    timeout = 120  # seconds of silence before a stalled upload is dropped
    store: MusicStore = None
    scanner: LibraryScanner = None
    password = ""
    iris_port = 6680

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # -- helpers --
    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            log.info("%s went away before the reply was sent", self.address_string())
            self.close_connection = True

    def _authorized(self):
        if not self.password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _user, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except ValueError:
                given = ""
            if hmac.compare_digest(given.encode(), self.password.encode()):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Pirate Audio music upload"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _same_origin(self):
        """Refuse uploads started by other websites (a page elsewhere can't push files to your Pi)."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urllib.parse.urlparse(origin).netloc == host

    # -- routes --
    def do_GET(self):
        if not self._authorized():
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/check":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            raw_path = (query.get("path") or [""])[0]
            try:
                size = int((query.get("size") or ["-1"])[0])
                exists = self.store.already_there(raw_path, size)
                self._send(200, {"ok": True, "exists": exists})
            except UploadError as err:
                self._send(200, {"ok": False, "status": err.status, "error": err.message})
            except ValueError:
                self._send(200, {"ok": False, "status": 400, "error": "Bad size."})
        elif path in ("/api/browse", "/api/download", "/api/zip"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            raw_path = (query.get("path") or [""])[0]
            if path == "/api/browse":
                try:
                    self._send(200, self.store.listing(raw_path))
                except UploadError as err:
                    self._send(err.status, {"error": err.message})
            elif path == "/api/download":
                self._send_file(raw_path)
            else:
                self._send_zip(raw_path)
        elif path == "/api/status":
            library = self.store.recent()
            self._send(200, {
                "free": self.store.free_bytes(),
                "keep_free": self.store.keep_free_bytes,
                "max_file": self.store.max_file_bytes,
                "music_dir": str(self.store.root).replace(os.path.expanduser("~"), "~", 1),
                "count": library["count"],
                "recent": library["recent"],
                "scan": self.scanner.status(),
                "iris_port": self.iris_port,
                "audio": sorted(AUDIO_EXTS),
                "extra": sorted(EXTRA_EXTS),
                "refused": REFUSED,
            })
        else:
            self._send(404, {"error": "Not found"})

    # -- downloads --
    def _send_file(self, raw_path):
        try:
            target = self.store.resolve(raw_path, folder=False)
            fh = open(target, "rb")
            size = os.fstat(fh.fileno()).st_size
        except UploadError as err:
            return self._send(err.status, {"error": err.message})
        except OSError as err:
            return self._send(500, {"error": f"Couldn't read the file: {err.strerror or err}"})
        with fh:
            start, end, status = 0, size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if m and (m.group(1) or m.group(2)):  # a download picking up where it stopped
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            length = max(0, end - start + 1)
            try:
                self.send_response(status)
                self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Disposition", content_disposition(target.name))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("X-Content-Type-Options", "nosniff")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                log.info("%s stopped downloading %s", self.address_string(), target.name)
                self.close_connection = True
                return
        log.info("Sent %s to %s", self.store._rel(target), self.address_string())

    def _send_zip(self, raw_path):
        try:
            folder = self.store.resolve(raw_path, folder=True)
        except UploadError as err:
            return self._send(err.status, {"error": err.message})
        name = (folder.name if raw_path.strip("/ ") else "Music") + ".zip"
        self.protocol_version = "HTTP/1.1"  # for a chunked reply (see _Chunked)
        self.close_connection = True
        body = _Chunked(self.wfile)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", content_disposition(name))
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            count = self.store.write_zip(folder, body)
            body.finish()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            log.info("%s stopped downloading %s", self.address_string(), name)
            return
        except (OSError, ValueError, zipfile.LargeZipFile) as err:
            log.warning("Making %s failed: %s", name, err)  # the reply just stops: the browser says it failed
            return
        log.info("Sent %s (%d files) to %s", name, count, self.address_string())

    def do_PUT(self):
        if not self._authorized():
            return
        url = urllib.parse.urlparse(self.path)
        if url.path != "/api/upload":
            self._send(404, {"error": "Not found"})
            return
        if not self._same_origin():
            self._send(403, {"error": "Uploads have to come from this page."})
            return
        query = urllib.parse.parse_qs(url.query)
        raw_path = (query.get("path") or [""])[0]
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        try:
            result = self.store.save(raw_path, length, self.rfile.read)
        except UploadError as err:
            if 0 < length <= DRAIN_LIMIT and err.message != "The upload was interrupted.":
                try:
                    MusicStore._drain(length, self.rfile.read)  # so the browser can read our answer
                except OSError:
                    pass
            self.close_connection = True
            self._send(err.status, {"error": err.message, "path": raw_path})
            return
        except OSError as err:
            self.close_connection = True
            log.warning("Saving %s failed: %s", raw_path, err)
            self._send(500, {"error": f"Couldn't save the file: {err.strerror or err}", "path": raw_path})
            return
        if result["status"] == "saved" and result.get("kind") == "audio":
            self.scanner.request()
        self._send(200, result)

    def do_POST(self):
        if not self._authorized():
            return
        if urllib.parse.urlparse(self.path).path == "/api/scan" and self._same_origin():
            self.scanner.request(delay=0)
            self._send(202, self.scanner.status())
        else:
            self._send(404, {"error": "Not found"})


def load_config(path):
    cfg = configparser.ConfigParser()
    cfg.read_dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.read(path)
    return cfg


def mopidy_http_port():
    conf = Path(os.path.expanduser("~/.config/mopidy/mopidy.conf"))
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cfg.read(conf)
        return cfg.getint("http", "port", fallback=6680)
    except (configparser.Error, ValueError):
        return 6680


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pirate_bluetooth.upload", description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.path.expanduser("~/.config/pirate-bluetooth/settings.ini"))
    parser.add_argument("--port", type=int, help="override the port from settings.ini")
    parser.add_argument("--bind", default="0.0.0.0", help="address to listen on (default: all)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    up = cfg["upload"]
    if not up.getboolean("enabled"):
        log.info("The upload page is turned off in settings.ini ([upload] enabled = no)")
        return
    command = up.get("scan_command").strip()
    mopidy_argv = _mopidy_execstart()
    if command == "auto":
        scan_cmd = mopidy_scan_command(mopidy_argv)
    elif command in ("", "none", "off"):
        scan_cmd = None
    else:
        scan_cmd = shlex.split(command)

    Handler.store = MusicStore(up.get("music_dir"), up.getint("max_file_mb") * 2**20,
                               up.getint("keep_free_mb") * 2**20)
    Handler.scanner = LibraryScanner(scan_cmd, up.getfloat("scan_delay"),
                                     mopidy_config_files(mopidy_argv) if mopidy_argv else None)
    Handler.password = up.get("password").strip()
    Handler.iris_port = mopidy_http_port()
    port = args.port or up.getint("port")

    server = ThreadingHTTPServer((args.bind, port), Handler)
    server.daemon_threads = True
    log.info("Music upload page on port %d, saving to %s%s", port, Handler.store.root,
             " (password protected)" if Handler.password else "")
    if not scan_cmd:
        log.info("Mopidy library refresh: off (%s)", Handler.scanner.message or "disabled in settings")
    elif Handler.scanner.library_off():
        log.warning(LIBRARY_OFF)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
