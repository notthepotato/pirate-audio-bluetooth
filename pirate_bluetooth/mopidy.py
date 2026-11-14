"""Remote control for Mopidy (the Pi's own music) over its JSON-RPC API.

Used when the add-on runs the Pirate Audio's screen and buttons for both Bluetooth and Mopidy. A background
thread keeps a small picture of Mopidy's state (current song, playing/paused, position, volume, repeat and
shuffle) and hands it to the main loop; commands run one at a time on a worker thread, so a slow Mopidy
never freezes the screen.
"""

import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request

from gi.repository import GLib

log = logging.getLogger(__name__)

POLL_FAST_S = 1.0  # while the Pi's music is on screen
POLL_SLOW_S = 4.0  # otherwise
OPTIONS_EVERY = 3  # read repeat/shuffle every few polls
MAX_QUEUE_ADD = 1000  # picking a song in a huge list queues at most this many songs from there on


class MopidyError(Exception):
    pass


def _artists(track):
    return ", ".join(a.get("name", "") for a in (track.get("artists") or []) if a.get("name"))


def track_info(track):
    """The bits of a Mopidy Track the screen needs."""
    if not track:
        return None
    album = track.get("album") or {}
    return {
        "uri": track.get("uri", ""),
        "title": track.get("name") or urllib.parse.unquote(track.get("uri", "").rsplit("/", 1)[-1]),
        "artist": _artists(track) or _artists(album),
        "album": album.get("name", ""),
        "length": track.get("length") or 0,
    }


class MopidyRemote:
    def __init__(self, rpc_url, on_state, on_error=None):
        """on_state(state_dict) and on_error(message) are called on the GLib main loop."""
        self.rpc_url = rpc_url
        self.http_base = rpc_url.split("/mopidy/rpc")[0]
        self._on_state = on_state
        self._on_error = on_error
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.fast = False
        self.resume_point = None  # (tlid, position ms) saved when Bluetooth took over
        self._tlid = object()
        self._track = None
        self._polls = 0
        self._options = {}
        self._stop = threading.Event()
        self._poke = threading.Event()
        self._jobs = queue.Queue()
        threading.Thread(target=self._poll_loop, daemon=True, name="mopidy-poll").start()
        threading.Thread(target=self._work_loop, daemon=True, name="mopidy-jobs").start()

    # -- plumbing -------------------------------------------------------------------------
    def rpc(self, method, timeout=5.0, **params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.rpc_url, data=body, headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                reply = json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError) as err:
            log.debug("%s: %s", method, err)
            raise MopidyError("Mopidy isn't answering (is it still starting?)") from err
        if "error" in reply:
            err = reply["error"]
            raise MopidyError(f"{method}: {err.get('message', err) if isinstance(err, dict) else err}")
        return reply.get("result")

    def close(self):
        self._stop.set()
        self._poke.set()
        self._jobs.put(None)

    def poke(self):
        """Poll again right away (after a command, or when the Pi's music comes on screen)."""
        self._poke.set()

    def run(self, fn, *args, on_done=None, on_error=None):
        """Queue fn(*args) for the worker thread. on_done(result) / on_error(msg) run on the main loop."""
        self._jobs.put((fn, args, on_done, on_error))

    def _work_loop(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, args, on_done, on_error = job
            try:
                result = fn(*args)
            except MopidyError as err:
                log.warning("%s", err)
                handler = on_error or self._on_error
                if handler:
                    GLib.idle_add(_once(handler, str(err)))
            except Exception:
                log.exception("Mopidy command failed")
                handler = on_error or self._on_error
                if handler:
                    GLib.idle_add(_once(handler, "Something went wrong talking to Mopidy"))
            else:
                if on_done:
                    GLib.idle_add(_once(on_done, result))
            self.poke()

    # -- state ----------------------------------------------------------------------------
    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                state = self.read_state()
            except MopidyError:
                state = {"available": False}
                self._tlid = object()
            GLib.idle_add(_once(self._on_state, state))
            self._poke.wait(POLL_FAST_S if self.fast else POLL_SLOW_S)
            self._poke.clear()

    def read_state(self):
        tlid = self.rpc("core.playback.get_current_tlid", timeout=3)
        if tlid != self._tlid:
            tl = self.rpc("core.playback.get_current_tl_track", timeout=3)
            self._track = track_info(tl["track"]) if tl else None
            self._tlid = tlid
        if self._polls % OPTIONS_EVERY == 0 or not self._options:
            self._options = {opt: bool(self.rpc(f"core.tracklist.get_{opt}", timeout=3))
                             for opt in ("repeat", "random", "single")}
            self._options["length"] = self.rpc("core.tracklist.get_length", timeout=3) or 0
        self._polls += 1
        return {
            "available": True,
            "state": self.rpc("core.playback.get_state", timeout=3),
            "tlid": tlid,
            "track": self._track,
            "position": self.rpc("core.playback.get_time_position", timeout=3) or 0,
            "volume": self.rpc("core.mixer.get_volume", timeout=3),
            "at": time.monotonic(),
            **self._options,
        }

    # -- playback (blocking: call through run()) ------------------------------------------------
    def play_pause(self):
        state = self.rpc("core.playback.get_state")
        if state == "playing":
            self.rpc("core.playback.pause")
            return "paused"
        return self.resume()

    def resume(self):
        """Carry on with the Pi's music: unpause, or pick up where Bluetooth interrupted it."""
        state = self.rpc("core.playback.get_state")
        if state == "paused":
            self.rpc("core.playback.resume")
            return "playing"
        if state == "playing":
            return "playing"
        point, self.resume_point = self.resume_point, None
        if point and self._play_at(*point):
            return "playing"
        if not self.rpc("core.tracklist.get_length"):
            return "empty"
        self.rpc("core.playback.play")  # the current song, or the first one
        return "playing"

    def _play_at(self, tlid, position):
        tlids = {t["tlid"] for t in self.rpc("core.tracklist.get_tl_tracks", timeout=10) or []}
        if tlid not in tlids:
            return False
        mute = bool(self.rpc("core.mixer.get_mute"))
        if position > 1000:
            self.rpc("core.mixer.set_mute", mute=True)  # don't blip the song's start before seeking
        try:
            self.rpc("core.playback.play", tlid=tlid)
            if position > 1000:
                for _ in range(30):
                    if self.rpc("core.playback.get_state") == "playing":
                        break
                    time.sleep(0.1)
                self.rpc("core.playback.seek", time_position=int(position))
        finally:
            if position > 1000:
                self.rpc("core.mixer.set_mute", mute=mute)
        return True

    def stop_and_remember(self):
        """Let go of the sound card (a paused Mopidy keeps it open) and remember where it was."""
        state = self.rpc("core.playback.get_state")
        if state in ("playing", "paused"):
            tlid = self.rpc("core.playback.get_current_tlid")
            position = self.rpc("core.playback.get_time_position") or 0
            if tlid is not None:
                self.resume_point = (tlid, position)
            self.rpc("core.playback.stop")
            log.info("Pi music stopped at %d ms so Bluetooth can use the sound card", position)
        return state

    def next(self):
        self.rpc("core.playback.next")

    def previous(self):
        self.rpc("core.playback.previous")

    def change_volume(self, delta):
        volume = self.rpc("core.mixer.get_volume")
        volume = max(0, min(100, (volume if volume is not None else 50) + delta))
        self.rpc("core.mixer.set_volume", volume=volume)
        return volume

    def cycle_repeat(self):
        """Off -> all -> one -> off. Returns the new mode."""
        repeat = bool(self.rpc("core.tracklist.get_repeat"))
        single = bool(self.rpc("core.tracklist.get_single"))
        mode = "one" if repeat and single else "all" if repeat else "off"
        new = {"off": "all", "all": "one", "one": "off"}[mode]
        self.rpc("core.tracklist.set_repeat", value=new != "off")
        self.rpc("core.tracklist.set_single", value=new == "one")
        self._options = {}
        return new

    def toggle_shuffle(self):
        new = not bool(self.rpc("core.tracklist.get_random"))
        self.rpc("core.tracklist.set_random", value=new)
        self._options = {}
        return new

    def play_tracks(self, uris, index):
        """Replace the queue with these songs and play the one at index."""
        if len(uris) > MAX_QUEUE_ADD:
            uris, index = uris[index:index + MAX_QUEUE_ADD], 0
        self.resume_point = None
        self.rpc("core.tracklist.clear")
        added = self.rpc("core.tracklist.add", uris=uris, timeout=120) or []
        if not added:
            raise MopidyError("Mopidy couldn't add those songs")
        target = added[min(index, len(added) - 1)]
        self.rpc("core.playback.play", tlid=target["tlid"])
        self._options = {}
        return len(added)

    def add_tracks(self, uris):
        added = self.rpc("core.tracklist.add", uris=uris[:MAX_QUEUE_ADD], timeout=120) or []
        self._options = {}
        return len(added)

    def play_tlid(self, tlid):
        self.resume_point = None
        self.rpc("core.playback.play", tlid=tlid)

    # -- browsing (blocking) ------------------------------------------------------------------
    def roots(self):
        """Mopidy's top-level folders, e.g. Files and Local media."""
        return self.rpc("core.library.browse", uri=None) or []

    def browse(self, uri):
        return self.rpc("core.library.browse", uri=uri, timeout=30) or []

    def playlists(self):
        return self.rpc("core.playlists.as_list", timeout=30) or []

    def playlist_items(self, uri):
        return self.rpc("core.playlists.get_items", uri=uri, timeout=30) or []

    def queue(self):
        current = self.rpc("core.playback.get_current_tlid")
        return current, self.rpc("core.tracklist.get_tl_tracks", timeout=30) or []

    def images(self, uri):
        """Album art URLs for a song, biggest first."""
        result = self.rpc("core.library.get_images", uris=[uri], timeout=10) or {}
        images = sorted(result.get(uri) or [], key=lambda i: -(i.get("width") or 0))
        urls = []
        for img in images:
            u = img.get("uri", "")
            if u.startswith("/"):
                u = self.http_base + u
            if u:
                urls.append(u)
        return urls


def _once(fn, arg):
    def call():
        fn(arg)
        return False
    return call
