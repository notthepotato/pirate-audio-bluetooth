"""Small wrappers around system tools: ALSA volume (amixer), Mopidy hand-off (systemctl --user)."""

import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)


def _run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as err:
        log.warning("%s failed: %s", cmd[0], err)
        return None


def find_pirate_card(cards_file="/proc/asound/cards") -> Optional[str]:
    """ALSA card id of the Pirate Audio DAC (the hifiberry-dac overlay), e.g. 'sndrpihifiberry'."""
    try:
        with open(cards_file, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"\s*\d+\s+\[([^\]]+)\]", line)
                if m and "hifiberry" in line.lower():
                    return m.group(1).strip()
    except OSError:
        pass
    return None


class HeadphoneVolume:
    """The 'volume knob' on the Pirate Audio: an ALSA softvol control in front of the DAC.

    It is separate from the phone's own volume, like the volume knob on a headphone amp.
    """

    def __init__(self, control="Pirate Headphone", card="auto", device="pirate_out",
                 state_file=None):
        self.control = control
        self._card = None if card == "auto" else card
        self.device = device
        self.state_file = state_file

    @property
    def card(self) -> Optional[str]:
        if self._card is None:
            self._card = find_pirate_card()
        return self._card

    def _amixer(self, *args):
        if not self.card:
            return None
        return _run(["amixer", "-c", self.card, *args], timeout=5)

    def get(self) -> Optional[int]:
        res = self._amixer("sget", self.control)
        if res is None or res.returncode != 0:
            return None
        m = re.search(r"\[(\d+)%\]", res.stdout)
        return int(m.group(1)) if m else None

    def set(self, pct: int) -> Optional[int]:
        pct = max(0, min(100, int(pct)))
        res = self._amixer("-q", "sset", self.control, f"{pct}%")
        if res is None or res.returncode != 0:
            return None
        self._save(pct)
        return self.get()

    def change(self, delta: int) -> Optional[int]:
        sign = "+" if delta > 0 else "-"
        res = self._amixer("-q", "sset", self.control, f"{abs(int(delta))}%{sign}")
        if res is None or res.returncode != 0:
            return None
        value = self.get()
        if value is not None:
            self._save(value)
        return value

    def ensure_control(self) -> bool:
        """A softvol control only exists after its PCM has been opened once (e.g. after boot)."""
        if self.get() is not None:
            return True
        log.info("Creating the '%s' mixer by opening %s once", self.control, self.device)
        _run(["aplay", "-q", "-D", self.device, "-d", "1", "-f", "S16_LE", "-r", "44100",
              "-c", "2", "/dev/zero"], timeout=8)
        if self.get() is None:
            return False
        saved = self._load()
        if saved is not None:
            self.set(saved)
        return True

    def _save(self, pct):
        if not self.state_file:
            return
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as fh:
                fh.write(str(pct))
        except OSError:
            pass

    def _load(self) -> Optional[int]:
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                return int(fh.read().strip())
        except (OSError, ValueError, TypeError):
            return None


class MopidyUnavailable(Exception):
    pass


class MopidyHandoff:
    """Shares the screen and buttons with Pimoroni's Mopidy setup (Mopidy-PiDi + raspberry-gpio).

    While a phone is playing (or pairing) we stop the Mopidy user service (which frees the LCD, buttons and
    the DAC) and start it again afterwards - but only if it was running before. Mopidy's configuration is
    never touched. Because a restart would empty Mopidy's queue, we save the queue, current track,
    position, repeat/shuffle and volume over Mopidy's own JSON-RPC API first, and put them back after.
    """

    def __init__(self, mode="auto", unit="mopidy", rpc_url="http://127.0.0.1:6680/mopidy/rpc",
                 state_file=None, ready_timeout=180):
        self.mode = mode
        self.unit = unit
        self.rpc_url = rpc_url
        self.state_file = state_file
        self.ready_timeout = ready_timeout
        self._stopped_it = False
        self._snapshot = None
        self._enabled = None
        self._restore_thread = None
        # Talk to Mopidy directly, never through an HTTP proxy from the environment.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _systemctl(self, *args, timeout=30):
        return _run(["systemctl", "--user", *args, self.unit], timeout=timeout)

    # -- Mopidy's JSON-RPC API ----------------------------------------------------------
    def rpc(self, method, timeout=3.0, **params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.rpc_url, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                reply = json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError) as err:
            raise MopidyUnavailable(str(err)) from err
        if "error" in reply:
            raise MopidyUnavailable(f"{method}: {reply['error'].get('message', reply['error'])}")
        return reply.get("result")

    def snapshot(self):
        """Queue, current track, position, options and volume - or None if Mopidy doesn't answer."""
        try:
            tl_tracks = self.rpc("core.tracklist.get_tl_tracks", timeout=10) or []
            current = self.rpc("core.playback.get_current_tl_track")
            snap = {
                "uris": [t["track"]["uri"] for t in tl_tracks],
                "current_uri": current["track"]["uri"] if current else None,
                "current_index": next((i for i, t in enumerate(tl_tracks)
                                       if current and t["tlid"] == current["tlid"]), None),
                "state": self.rpc("core.playback.get_state"),
                "position": self.rpc("core.playback.get_time_position") or 0,
                "volume": self.rpc("core.mixer.get_volume"),
                "mute": bool(self.rpc("core.mixer.get_mute")),
            }
            for opt in ("repeat", "random", "single", "consume"):
                snap[opt] = bool(self.rpc(f"core.tracklist.get_{opt}"))
        except (MopidyUnavailable, KeyError, TypeError) as err:
            log.warning("Couldn't save Mopidy's queue (%s); it will start empty", err)
            return None
        log.info("Saved Mopidy state: %d queued, %s at %d ms", len(snap["uris"]), snap["state"],
                 snap["position"])
        return snap

    def _wait_until_ready(self) -> bool:
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            try:
                self.rpc("core.get_version", timeout=2)
                return True
            except MopidyUnavailable:
                time.sleep(1)
        return False

    def restore(self, snap):
        """Blocking: wait for Mopidy to come back, then put the saved state back."""
        if not self._wait_until_ready():
            log.warning("Mopidy didn't come back within %d s; not restoring its queue", self.ready_timeout)
            return
        try:
            if snap is None:
                return
            if self.rpc("core.tracklist.get_length"):
                log.info("Mopidy already has a queue; leaving it as it is")
                return
            added = self.rpc("core.tracklist.add", uris=snap["uris"], timeout=120) or [] \
                if snap["uris"] else []
            for opt in ("repeat", "random", "single", "consume"):
                self.rpc(f"core.tracklist.set_{opt}", value=snap[opt])
            if snap.get("volume") is not None:
                self.rpc("core.mixer.set_volume", volume=snap["volume"])
            target = None
            if snap.get("current_uri"):
                matches = [t for t in added if t["track"]["uri"] == snap["current_uri"]]
                # Prefer the copy at the same queue position if a song is queued twice.
                idx = snap.get("current_index")
                same_spot = [t for t in matches if idx is not None and added.index(t) == idx]
                target = (same_spot or matches or [None])[0]
            if target and snap["state"] in ("playing", "paused"):
                # Select the track and position without making a sound: play muted, pause, seek.
                self.rpc("core.mixer.set_mute", mute=True)
                try:
                    self.rpc("core.playback.play", tlid=target["tlid"])
                    for _ in range(50):
                        if self.rpc("core.playback.get_state") == "playing":
                            break
                        time.sleep(0.1)
                    self.rpc("core.playback.pause")
                    if snap["position"]:
                        self.rpc("core.playback.seek", time_position=int(snap["position"]))
                finally:
                    self.rpc("core.mixer.set_mute", mute=snap["mute"])
            elif snap["mute"]:
                self.rpc("core.mixer.set_mute", mute=True)
            log.info("Restored Mopidy's queue (%d tracks)", len(added))
        except (MopidyUnavailable, KeyError, TypeError, ValueError) as err:
            log.warning("Couldn't fully restore Mopidy's queue: %s", err)
        finally:
            self._clear_state()

    # -- remembering that we stopped Mopidy, in case this program is restarted ---------------------
    def _write_state(self, data):
        if not self.state_file:
            return
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError as err:
            log.warning("Couldn't save %s: %s", self.state_file, err)

    def _clear_state(self):
        if self.state_file:
            try:
                os.remove(self.state_file)
            except OSError:
                pass

    def recover(self) -> bool:
        """At start-up: did a previous run stop Mopidy and not start it again? Then we owe it a restart."""
        if not self.state_file or not os.path.exists(self.state_file):
            return False
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            self._clear_state()
            return False
        if data.get("stopped_by_us"):
            log.info("A previous run stopped Mopidy; it will be started again")
            self._stopped_it = True
            self._snapshot = data.get("snapshot")
            return True
        return False

    @property
    def enabled(self) -> bool:
        if self._enabled is None:
            if self.mode == "off":
                self._enabled = False
            elif self.mode == "on":
                self._enabled = True
            else:
                res = self._systemctl("is-enabled", timeout=5)
                self._enabled = bool(res and res.returncode == 0)
            log.info("Mopidy hand-off is %s", "on" if self._enabled else "off")
        return self._enabled

    def take(self) -> None:
        """Blocking: save Mopidy's state and stop it, if it is running. Call from a worker thread."""
        if not self.enabled or self._stopped_it:
            return
        if self._restore_thread and self._restore_thread.is_alive():
            self._restore_thread.join(timeout=self.ready_timeout + 30)  # let a restart finish first
        for _ in range(60):  # don't race a Mopidy that is still starting up
            res = self._systemctl("is-active", timeout=5)
            if not res or res.stdout.strip() not in ("activating", "reloading"):
                break
            time.sleep(1)
        if res and res.returncode == 0:
            snap = self.snapshot()
            self._write_state({"stopped_by_us": True, "snapshot": snap})
            log.info("Stopping %s while Bluetooth has the screen", self.unit)
            self._systemctl("stop")
            self._stopped_it = True
            self._snapshot = snap

    def wait_restore(self, timeout: float) -> None:
        if self._restore_thread and self._restore_thread.is_alive():
            self._restore_thread.join(timeout=timeout)

    def give_back(self) -> None:
        """Start Mopidy again (if we stopped it) and restore its queue in the background."""
        if not self._stopped_it:
            return
        log.info("Starting %s again", self.unit)
        self._systemctl("start", "--no-block", timeout=10)
        self._stopped_it = False
        snap, self._snapshot = self._snapshot, None
        self._restore_thread = threading.Thread(target=self.restore, args=(snap,), daemon=True)
        self._restore_thread.start()


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are sent; this just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "not connected"
    finally:
        s.close()
