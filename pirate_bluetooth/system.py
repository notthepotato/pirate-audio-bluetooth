"""Small wrappers around system tools: ALSA volume (amixer), Mopidy hand-off (systemctl --user)."""

import glob
import json
import logging
import os
import sys
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


def _card_index(card, cards_file="/proc/asound/cards"):
    try:
        with open(cards_file, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"\s*(\d+)\s+\[([^\]]+)\]", line)
                if m and m.group(2).strip() == card:
                    return int(m.group(1))
    except OSError:
        pass
    return None


def dac_in_use(card=None, asound_dir="/proc/asound") -> bool:
    """Is something (Mopidy, or Bluetooth audio) playing through the Pirate Audio's DAC right now?

    The DAC can only be opened by one program at a time; a paused Mopidy keeps it open too."""
    card = card or find_pirate_card()
    index = _card_index(card, os.path.join(asound_dir, "cards")) if card else None
    if index is None:
        return False
    for status in glob.glob(os.path.join(asound_dir, f"card{index}", "pcm*p", "sub*", "status")):
        try:
            with open(status, encoding="utf-8") as fh:
                if fh.read().strip() != "closed":
                    return True
        except OSError:
            pass
    return False


def wait_dac_free(timeout=4.0, card=None) -> bool:
    deadline = time.monotonic() + timeout
    while dac_in_use(card):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


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


BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
QUEUE_SAVE_EVERY_S = 30  # while Mopidy plays, how often the position is written down (for power cuts)


def boot_id() -> str:
    """Changes every time the Pi boots, so saved state can tell "after a reboot" from "same session"."""
    try:
        with open(os.environ.get("PIRATE_BOOT_ID_FILE", BOOT_ID_FILE), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def uptime() -> float:
    """Seconds since the Pi booted."""
    try:
        with open(os.environ.get("PIRATE_UPTIME_FILE", "/proc/uptime"), encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return float("inf")


def write_json(path, data) -> bool:
    """Write a small state file safely: a power cut mid-write leaves the old file, never half a file."""
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
        return True
    except OSError as err:
        log.warning("Couldn't save %s: %s", path, err)
        return False


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


class MopidyHandoff:
    """Shares the screen and buttons with Pimoroni's Mopidy setup (Mopidy-PiDi + raspberry-gpio).

    While a phone is playing (or pairing) we stop the Mopidy user service (which frees the LCD, buttons and
    the DAC) and start it again afterwards - but only if it was running before. Mopidy's configuration is
    never touched. Because a restart would empty Mopidy's queue, we save the queue, current track,
    position, repeat/shuffle and volume over Mopidy's own JSON-RPC API first, and put them back after.

    The same saved queue also carries Mopidy over reboots: while Mopidy runs, `watch_step()` keeps
    `queue_file` up to date, and when Mopidy comes up empty after the Pi starts, the queue is put back
    (paused where it was), so pressing play after power-on plays something.
    """

    def __init__(self, mode="auto", unit="mopidy", rpc_url="http://127.0.0.1:6680/mopidy/rpc",
                 state_file=None, queue_file=None, ready_timeout=180):
        self.mode = mode
        self.unit = unit
        self.rpc_url = rpc_url
        self.state_file = state_file
        self.queue_file = queue_file
        self.ready_timeout = ready_timeout
        self._stopped_it = False
        self._snapshot = None
        self._enabled = None
        self.restoring = False  # putting a saved queue back right now (it plays muted for a moment)
        self._restore_thread = None
        self._lock = threading.Lock()  # one of take() / boot restore / queue saving at a time
        self._boot = boot_id()
        saved = self._read_queue()
        # Saved during an earlier boot and not empty: put it back once Mopidy is up (if it comes up empty).
        self.boot_restore_pending = bool(saved and saved.get("boot_id") != self._boot
                                         and (saved.get("snapshot") or {}).get("uris"))
        self._watch_key = None
        self._watch_saved_at = 0.0
        self._watch_snap = None
        # Talk to Mopidy directly, never through an HTTP proxy from the environment.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _systemctl(self, *args, timeout=30):
        return _run(["systemctl", "--user", *args, self.unit], timeout=timeout)

    @property
    def stopped_by_us(self) -> bool:
        return self._stopped_it

    @property
    def busy(self) -> bool:
        return self._stopped_it or bool(self._restore_thread and self._restore_thread.is_alive())

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

    def snapshot(self, quiet=False):
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
            if not quiet:
                log.warning("Couldn't read Mopidy's queue (%s)", err)
            return None
        if not quiet:
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

    def restore(self, snap, wait=True):
        """Blocking: wait for Mopidy to come back, then put the saved state back (paused, silently)."""
        self.restoring = True
        try:
            self._restore(snap, wait)
        finally:
            self.restoring = False

    def _restore(self, snap, wait):
        if wait and not self._wait_until_ready():
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

    # -- the queue kept across reboots ---------------------------------------------------------------
    def _read_queue(self):
        data = read_json(self.queue_file) if self.queue_file else None
        return data if isinstance(data, dict) else None

    def _write_queue(self, snap, boot=None):
        if self.queue_file and snap is not None:
            write_json(self.queue_file, {"boot_id": self._boot if boot is None else boot,
                                         "saved_at": time.time(), "snapshot": snap})

    def _saved_from_earlier_boot(self):
        """The queue saved before this boot, if it hasn't been put back yet."""
        if not self.boot_restore_pending:
            return None
        saved = self._read_queue()
        return (saved or {}).get("snapshot")

    def watch_step(self):
        """Call every few seconds from a worker thread while Mopidy may be running. Keeps the saved queue
        current, puts it back after a reboot, and returns Mopidy's playback state ("playing", ...) or
        None when Mopidy isn't reachable or is busy being handed over."""
        if not self.enabled or self.busy or not self._lock.acquire(blocking=False):
            return None
        try:
            if self.busy:
                return None
            try:
                key = (self.rpc("core.tracklist.get_version", timeout=2),
                       self.rpc("core.playback.get_state", timeout=2),
                       self.rpc("core.playback.get_current_tlid", timeout=2),
                       self.rpc("core.mixer.get_volume", timeout=2))
            except MopidyUnavailable:
                return None
            if self.boot_restore_pending:
                snap = self._saved_from_earlier_boot()
                self.boot_restore_pending = False
                if snap and key[0] is not None:
                    log.info("Mopidy started after power-on: putting its queue back")
                    self.restore(snap, wait=False)  # leaves it alone if Mopidy already has a queue
                    return None  # report nothing this round: the restore played (muted) for a moment
            now = time.monotonic()
            if key != self._watch_key:
                snap = self.snapshot(quiet=True)
                if snap is not None:
                    self._write_queue(snap)
                    self._watch_key, self._watch_snap, self._watch_saved_at = key, snap, now
            elif key[1] == "playing" and now - self._watch_saved_at >= QUEUE_SAVE_EVERY_S and self._watch_snap:
                try:
                    self._watch_snap["position"] = self.rpc("core.playback.get_time_position", timeout=2) or 0
                except MopidyUnavailable:
                    return None
                self._write_queue(self._watch_snap)
                self._watch_saved_at = now
            return key[1]
        finally:
            self._lock.release()

    def save_now(self):
        """At shutdown: write down exactly where Mopidy is, if it's running and ours to read."""
        if not self.enabled or self.busy or self.boot_restore_pending:
            return
        with self._lock:
            snap = self.snapshot(quiet=True)
            if snap is not None:
                self._write_queue(snap)
                log.info("Saved Mopidy's queue for next time (%d tracks)", len(snap["uris"]))

    # -- remembering that we stopped Mopidy, in case this program is restarted ---------------------
    def _write_state(self, data):
        if self.state_file:
            write_json(self.state_file, data)

    def _clear_state(self):
        if self.state_file:
            try:
                os.remove(self.state_file)
            except OSError:
                pass

    def recover(self) -> bool:
        """At start-up: did a previous run stop Mopidy and not start it again? Then we owe it a restart.

        After a reboot we don't: Mopidy starts by itself at boot, and its saved queue is put back from
        the queue file instead."""
        if not self.state_file or not os.path.exists(self.state_file):
            return False
        data = read_json(self.state_file)
        if not isinstance(data, dict) or not data.get("stopped_by_us"):
            self._clear_state()
            return False
        if data.get("boot_id", self._boot) != self._boot:
            if data.get("snapshot") is not None:
                self._write_queue(data["snapshot"], boot=data.get("boot_id"))
                self.boot_restore_pending = bool(data["snapshot"].get("uris"))
            self._clear_state()
            return False
        log.info("A previous run stopped Mopidy; it will be started again")
        self._stopped_it = True
        self._snapshot = data.get("snapshot")
        return True

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
        with self._lock:
            for _ in range(60):  # don't race a Mopidy that is still starting up
                res = self._systemctl("is-active", timeout=5)
                if not res or res.stdout.strip() not in ("activating", "reloading"):
                    break
                time.sleep(1)
            if not (res and res.returncode == 0):
                return
            snap = self.snapshot(quiet=self.boot_restore_pending)  # just after boot it may not answer yet
            earlier = self._saved_from_earlier_boot()
            if earlier and (snap is None or not snap["uris"]):
                # Mopidy has only just started after power-on: carry over the queue from before the reboot.
                log.info("Using Mopidy's queue from before the reboot")
                snap = earlier
            elif snap is None:
                snap = (self._read_queue() or {}).get("snapshot")
                if snap:
                    log.info("Mopidy didn't answer; using its last saved queue")
            self.boot_restore_pending = False
            if snap and snap.get("state") in ("playing", "paused"):
                try:  # let go of the DAC right away, so the phone's audio isn't held up while Mopidy exits
                    self.rpc("core.playback.stop", timeout=2)
                except MopidyUnavailable:
                    pass
            self._write_state({"stopped_by_us": True, "snapshot": snap, "boot_id": self._boot})
            if snap is not None:
                self._write_queue(snap)
            log.info("Stopping %s while Bluetooth has the screen", self.unit)
            self._systemctl("stop")
            self._stopped_it = True
            self._snapshot = snap
            self._watch_key = None

    def wait_restore(self, timeout: float) -> None:
        if self._restore_thread and self._restore_thread.is_alive():
            self._restore_thread.join(timeout=timeout)

    def give_back(self) -> None:
        """Start Mopidy again (if we stopped it) and restore its queue in the background."""
        if not self._stopped_it:
            return
        log.info("Starting %s again", self.unit)
        self._systemctl("start", "--no-block", timeout=10)
        snap, self._snapshot = self._snapshot, None
        self._restore_thread = threading.Thread(target=self.restore, args=(snap,), daemon=True)
        self._restore_thread.start()
        self._stopped_it = False


def restart_mopidy_keeping_queue(rpc_url="http://127.0.0.1:6680/mopidy/rpc"):
    """Restart the Mopidy user service (after a settings change) and put its queue back afterwards.

    A restart empties Mopidy's queue; this saves it first and restores it paused at the same spot."""
    res = _run(["systemctl", "--user", "is-active", "mopidy"], timeout=10)
    if not res or res.stdout.strip() != "active":
        return False  # not running: the new settings apply when it starts
    handoff = MopidyHandoff(mode="on", rpc_url=rpc_url, ready_timeout=120)
    snap = handoff.snapshot(quiet=True)
    _run(["systemctl", "--user", "restart", "mopidy"], timeout=120)
    if snap and snap.get("uris"):
        handoff.restore(snap)
    return True


def shutting_down() -> bool:
    """Is the Pi shutting down or rebooting (or the user's session ending)? Then don't restart things."""
    for scope in (["--user"], []):
        res = _run(["systemctl", *scope, "is-system-running"], timeout=5)
        if res and res.stdout.strip() == "stopping":
            return True
    return False


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are sent; this just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "not connected"
    finally:
        s.close()


if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "restart-mopidy":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    restart_mopidy_keeping_queue(*(sys.argv[2:3]))
