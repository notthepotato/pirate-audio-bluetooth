"""Main loop: Bluetooth and Mopidy state in, frames to the LCD and button presses out.

Two ways to share the Pirate Audio with Pimoroni's Mopidy setup:

* One screen (the default since the add-on can browse music): Mopidy's own screen and button extensions
  (PiDi, raspberry-gpio) are switched off, and this program runs the screen and buttons all the time, for
  the phone (Bluetooth) and the Pi's own music (Mopidy, over its JSON-RPC API). Mopidy keeps running.
* Hand-off (if PiDi or raspberry-gpio are still switched on): this program only borrows the screen and
  buttons while the phone plays, by stopping Mopidy and starting it again afterwards.
"""

import argparse
import configparser
import logging
import os
import signal
import threading
import time

from gi.repository import GLib

from . import __version__, mopidyconf
from .art import ArtLoader
from .bluetooth import BluetoothClient
from .menu import Item, Menu, Page, scroll_step
from .state import NowPlaying, format_time
from .system import (HeadphoneVolume, MopidyHandoff, boot_id, local_ip, read_json, shutting_down,
                     uptime, wait_dac_free, write_json)
from .view import View

log = logging.getLogger("pirate_bluetooth")

DEFAULTS = {
    "bluetooth": {"name": "Pirate Audio", "pair_seconds": "180", "reconnect_on_start": "yes",
                  "reconnect_seconds": "180", "boot_wait": "300", "art_lookup": "yes",
                  "auto_pair_if_none": "yes", "handback_after": "60", "bus": "system"},
    "display": {"rotation": "90", "spi_mhz": "80", "sleep_after": "120"},
    "buttons": {"y_pin": "24", "hold_time": "0.7"},
    "volume": {"step": "5", "card": "auto", "control": "Pirate Headphone", "device": "pirate_out"},
    "mopidy": {"handoff": "auto", "rpc_url": "http://127.0.0.1:6680/mopidy/rpc"},
    "upload": {"enabled": "yes", "port": "6681"},
}

STATE_DIR = os.path.join(os.path.expanduser("~"), ".local", "state", "pirate-bluetooth")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "pirate-bluetooth")
TICK_MS = 500
HW_RETRIES = 20
MOPIDY_WATCH_S = 5  # how often Mopidy's queue is written down (so it survives reboots)
FRESH_BOOT_S = 600  # started this soon after power-on (and first time this boot) = "just switched on"
PRESS_RECONNECT_S = 30  # A pressed with no phone connected: keep trying this long
PLAY_WAIT_S = 12  # ...and send Play if the phone shows up within this long after it connects
DAC_WAIT_S = 4  # how long to wait for the phone's audio to let go of the sound card
REPEAT_LABELS = {"off": "Off", "all": "All", "one": "One song"}


def load_config(path):
    cfg = configparser.ConfigParser()
    cfg.read_dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.read(path)
    return cfg


class RecordRenderer:
    """Test renderer: hands the View straight to RecordDisplay."""

    def render(self, view):
        return view


def _repeat_mode(state):
    if not state.get("repeat"):
        return "off"
    return "one" if state.get("single") else "all"


class App:
    def __init__(self, cfg, args, loop):
        self.cfg, self.args, self.loop = cfg, args, loop
        self.np = NowPlaying()
        self.present = False
        self.phone = ""
        self.has_player = False
        self.pairing = False
        self.paired = []
        self.alias = ""
        self.confirm = None  # {"phone": str, "code": str}
        self.last_playing_at = 0.0
        self.hold_until = 0.0  # keep the screen a while after pairing, so the new phone can start playing
        self.connecting = False  # looking for a paired phone right now
        self.bt_ready = False  # BlueZ has finished starting up (so self.paired is complete)
        self.play_pending_until = 0.0  # A was pressed before the phone was there: play when it is
        # Hand-off mode only: after power-on, if the phone was what played last, the Bluetooth screen
        # keeps the buttons for a while (instead of Mopidy), so that A plays the phone.
        self.boot_hold_until = 0.0
        self.boot_saw_connecting = False
        self.boot_saw_phone = False
        self._stop = threading.Event()

        self.overlay = None
        self.overlay_until = 0.0
        self.info_until = 0.0
        self.last_activity = time.monotonic()
        self.asleep = False

        self.display = None
        self.buttons = None
        self.hw_owned = False
        self._hw_attempts = 0
        self._acquire_source = 0
        self._render_pending = False
        self._taking = False

        self.step = cfg.getint("volume", "step")
        self.sleep_after = cfg.getfloat("display", "sleep_after")
        self.handback_after = cfg.getfloat("bluetooth", "handback_after")
        self.volume = HeadphoneVolume(
            control=cfg.get("volume", "control"),
            card=cfg.get("volume", "card"),
            device=cfg.get("volume", "device"),
            state_file=os.path.join(STATE_DIR, "volume"),
        )
        self.mopidy = MopidyHandoff(mode=args.mopidy or cfg.get("mopidy", "handoff"),
                                    rpc_url=cfg.get("mopidy", "rpc_url"),
                                    state_file=os.path.join(STATE_DIR, "mopidy-handoff.json"),
                                    queue_file=os.path.join(STATE_DIR, "mopidy-queue.json"))
        if self.mopidy.recover():
            self.mopidy.give_back()  # a previous run stopped Mopidy and didn't get to restart it

        # One screen for both, or hand-off? (see the module docstring)
        self.pi = None  # MopidyRemote when the Pi's own music is controlled from here
        self.pi_state = {}
        self.unified = True
        mopidy_files = mopidyconf.config_files() if self.mopidy.enabled else []
        if self.mopidy.enabled:
            uses_screen = [ext for ext in ("pidi", "raspberry-gpio")
                           if mopidyconf.extension_enabled(mopidy_files, ext) is not False]
            if uses_screen:
                self.unified = False
                log.info("Mopidy still runs its own screen/buttons (%s): sharing them by hand-off",
                         ", ".join(uses_screen))
            else:
                from .mopidy import MopidyRemote
                self.pi = MopidyRemote(cfg.get("mopidy", "rpc_url"), self.on_pi_state, self.on_pi_error)
                log.info("Running the screen and buttons for Bluetooth and the Pi's music")

        self.menu = Menu()
        self._swallow = None  # button whose press woke the screen
        self._repeats = {}  # button -> how many repeat events while held (menus scroll faster)
        music_dirs = [mopidyconf.get(mopidy_files, "local", "media_dir") or "~/Music"] if mopidy_files else ["~/Music"]
        self.art = ArtLoader(os.path.join(CACHE_DIR, "art"), self.on_art,
                             lookup_online=cfg.getboolean("bluetooth", "art_lookup"),
                             music_dirs=[d.replace("$XDG_MUSIC_DIR", "~/Music") for d in music_dirs])
        self.bt_art_key = None
        self.pi_art_key = None
        self.pi_track_uri = None

        self._last_used_file = os.path.join(STATE_DIR, "last-used.json")
        self.last_used = (read_json(self._last_used_file) or {}).get("source")
        self.source = "pi" if (self.pi and self.last_used == "mopidy") else "bluetooth"
        if (not self.unified and self._just_switched_on()
                and cfg.getboolean("bluetooth", "reconnect_on_start")
                and self.last_used in ("phone", None)):  # None: nothing recorded yet (first run)
            log.info("Switched on, and the phone played last: the Bluetooth screen has the buttons for now")
            self.boot_hold_until = time.monotonic() + cfg.getfloat("bluetooth", "boot_wait")
        elif self.unified:
            self._just_switched_on()  # keep the marker current
        if self.pi:
            log.info("Playing from: %s (used last)", "the Pi's music" if self.source == "pi" else "Bluetooth")

        if args.record:
            self.renderer = RecordRenderer()
        else:
            from .screen import Renderer  # PIL is only needed when drawing real frames
            self.renderer = Renderer()

        self.bt = BluetoothClient(
            self.on_bt, self.on_agent,
            bus=args.bus or cfg.get("bluetooth", "bus"),
            alias=cfg.get("bluetooth", "name"),
            reconnect_on_start=cfg.getboolean("bluetooth", "reconnect_on_start"),
            auto_pair_if_none=cfg.getboolean("bluetooth", "auto_pair_if_none"),
            pair_seconds=cfg.getint("bluetooth", "pair_seconds"),
            reconnect_seconds=cfg.getint("bluetooth", "reconnect_seconds"),
        )
        threading.Thread(target=self.volume.ensure_control, daemon=True).start()
        if self.mopidy.enabled:
            threading.Thread(target=self._watch_mopidy, daemon=True).start()
        GLib.timeout_add(TICK_MS, self._tick)
        self._update_ownership()

    # == what was used last, across reboots ========================================
    def _just_switched_on(self) -> bool:
        """First start since the Pi booted, and soon after it (not a restart of this program)."""
        marker = os.path.join(STATE_DIR, "boot-id")
        current = boot_id()
        try:
            with open(marker, encoding="utf-8") as fh:
                seen = fh.read().strip()
        except OSError:
            seen = None
        if seen == current:
            return False
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(current)
        except OSError:
            pass
        return uptime() < FRESH_BOOT_S

    def set_last_used(self, source):
        if source != self.last_used:
            log.info("Last used: %s", "phone" if source == "phone" else "the Pi's music (Mopidy)")
            self.last_used = source
            write_json(self._last_used_file, {"source": source})
        return False

    def _watch_mopidy(self):
        """Worker thread: keeps Mopidy's saved queue current (and puts it back after power-on), and
        notices when Mopidy is what's playing (hand-off mode)."""
        while not self._stop.wait(MOPIDY_WATCH_S):
            try:
                if self.mopidy.watch_step() == "playing" and not self.unified:
                    GLib.idle_add(self.set_last_used, "mopidy")
            except Exception:
                log.exception("Watching Mopidy failed")

    # == right after power-on (hand-off mode) ===========================================
    def boot_hold_active(self) -> bool:
        if not self.boot_hold_until:
            return False
        reason = None
        if time.monotonic() >= self.boot_hold_until:
            reason = ""
        elif self.bt_ready and not self.paired:
            reason = ""
        elif self.phone and self.np.playing:
            reason = ""  # the phone is playing: the usual rules take over from here
        elif self.boot_saw_phone and not self.phone:
            reason = ""  # it connected and went away again
        elif self.boot_saw_connecting and not self.connecting and not self.phone:
            reason = f"Couldn't find {self.paired[0]}" if self.paired else ""
        if reason is not None:
            self.end_boot_hold(reason)
            return False
        return True

    def end_boot_hold(self, message=""):
        if self.boot_hold_until:
            log.info("Power-on wait for the phone is over%s", f": {message}" if message else "")
            self.boot_hold_until = 0.0
            if message:
                self.toast(message)

    # == who owns the screen and buttons: us or Mopidy =============================
    def wants_hardware(self) -> bool:
        if self.unified or not self.mopidy.enabled:
            return True  # one screen for both, or no Mopidy to share with: always ours
        if self.pairing or self.confirm or time.monotonic() < self.hold_until:
            return True
        if self.boot_hold_active():
            return True
        if self.phone and self.np.playing:
            return True
        # Keep it a little while after the music pauses, so A can resume it.
        return bool(self.phone and self.last_playing_at
                    and time.monotonic() - self.last_playing_at < self.handback_after)

    def _update_ownership(self):
        want = self.wants_hardware()
        if want and not self.hw_owned and not self._taking:
            if self.unified or not self.mopidy.enabled:
                if not self._acquire_source:
                    self.acquire()
                return
            self._taking = True

            def worker():
                self.mopidy.take()  # stops Mopidy (frees LCD, buttons and DAC) if it's running
                GLib.idle_add(self._taken)

            threading.Thread(target=worker, daemon=True).start()
        elif not want and self.hw_owned:
            self.release()
            self.mopidy.give_back()

    def _taken(self):
        self._taking = False
        if self.wants_hardware():
            self.acquire()
        else:
            self.mopidy.give_back()
        return False

    def _make_display(self):
        from . import hardware
        if self.args.record:
            return hardware.RecordDisplay(self.args.record)
        if self.args.preview:
            return hardware.PreviewDisplay(self.args.preview)
        return hardware.St7789Display(rotation=self.cfg.getint("display", "rotation"),
                                      spi_mhz=self.cfg.getint("display", "spi_mhz"))

    def _make_buttons(self):
        from . import hardware
        if self.args.fifo:
            return hardware.FifoButtons(self._button_from_thread, self.args.fifo)
        if self.args.preview or self.args.record:
            return None
        return hardware.GpioButtons(self._button_from_thread,
                                    y_pin=self.cfg.getint("buttons", "y_pin"),
                                    hold_time=self.cfg.getfloat("buttons", "hold_time"))

    def acquire(self):
        self._acquire_source = 0
        if self.hw_owned or not self.wants_hardware():
            return False
        display = None
        try:
            display = self._make_display()
            buttons = self._make_buttons()
        except Exception as err:
            if display is not None:
                display.close()
            self._hw_attempts += 1
            if self._hw_attempts <= HW_RETRIES:
                log.warning("Display/buttons not available yet (%s); retrying", err)
                self._acquire_source = GLib.timeout_add(1000, self.acquire)
            else:
                log.error("Giving up on the display/buttons: %s", err)
            return False
        self._hw_attempts = 0
        self.display, self.buttons, self.hw_owned = display, buttons, True
        log.info("Display and buttons ready")
        self.wake()
        self._render()
        return False

    def release(self, handback_screen=True):
        if self._acquire_source:
            GLib.source_remove(self._acquire_source)
            self._acquire_source = 0
        if not self.hw_owned:
            return
        if self.buttons:
            self.buttons.close()
        if self.display:
            if self.mopidy.enabled and not self.unified and handback_screen:
                # Mopidy takes a few seconds to start; don't leave the old song frozen on screen.
                try:
                    if self.asleep:
                        self.display.backlight(True)
                    overlay = self.overlay if time.monotonic() < self.overlay_until else None
                    self.display.show(self.renderer.render(View(kind="handback", overlay=overlay)))
                except Exception:
                    log.exception("Drawing the hand-back screen failed")
            self.display.close()
        self.display = self.buttons = None
        self.hw_owned = False
        self.info_until = 0.0
        self.play_pending_until = 0.0  # A pressed on our screen doesn't carry over to Mopidy's
        log.info("Display and buttons handed back")

    # == switching between the phone and the Pi's music (one-screen mode) ================
    def switch_to(self, target, play=True, started_elsewhere=False):
        """Make `target` ("bluetooth" or "pi") the music that's on screen and on the buttons.

        Only one of them can use the sound card at a time, so the other one is paused first: the phone
        with a Bluetooth Pause, Mopidy by stopping it (a paused Mopidy keeps the sound card) and
        remembering the spot, so it carries on from there later."""
        if not self.pi:
            return
        changed = target != self.source
        self.source = target
        self.set_last_used("mopidy" if target == "pi" else "phone")
        if changed:
            log.info("Now playing from %s", "the Pi's music" if target == "pi" else "Bluetooth")
            self.wake()
        if target == "pi":
            self.play_pending_until = 0.0
            phone_playing = bool(self.phone and self.np.playing)
            if phone_playing:
                self.bt.command("pause")
            if started_elsewhere:
                if phone_playing:
                    # Mopidy was started from Iris while the phone held the sound card, so it may not
                    # have got it: once the phone lets go, make sure Mopidy is really playing.
                    self.pi.run(self._pi_after_dac, self.pi.resume)
            elif play:
                self.pi.run(self._pi_after_dac, self.pi.resume, on_done=self._pi_resumed)
            self.pi.fast = True
            self.pi.poke()
        else:
            self.pi.fast = False
            self.pi.run(self.pi.stop_and_remember)
            if play:
                if self.phone and self.has_player:
                    self.bt.command("play", on_error=lambda _m: self.toast("Your phone didn't respond"))
                else:
                    self.play_pause()
        self.request_render()

    def _pi_after_dac(self, fn, *args):
        """On the Mopidy worker: wait for the phone's audio to free the sound card, then run fn."""
        if not wait_dac_free(DAC_WAIT_S, self.volume.card):
            log.warning("The sound card is still busy; trying anyway")
        return fn(*args)

    def _pi_resumed(self, result):
        if result == "empty":
            self.toast("Nothing queued on the Pi. Hold A for the menu")

    # == Mopidy (one-screen mode) =======================================================
    def on_pi_state(self, state):
        before = self.pi_state
        self.pi_state = state
        if not state.get("available"):
            if before.get("available"):
                log.warning("Mopidy stopped answering")
            if self.source == "pi":
                self.request_render()
            return
        playing = state.get("state") == "playing"
        if not self.mopidy.restoring:
            if playing and self.source != "pi":
                log.info("Mopidy started playing (Iris?): switching to the Pi's music")
                self.switch_to("pi", started_elsewhere=True)
            elif state.get("state") == "paused" and self.source == "bluetooth":
                # A paused Mopidy keeps the sound card open, which would block the phone's audio.
                self.pi.run(self.pi.stop_and_remember)
        if playing:
            self.last_activity = time.monotonic()
        track = state.get("track")
        uri = track["uri"] if track else None
        if uri != self.pi_track_uri:
            self.pi_track_uri = uri
            self.pi_art_key = self.art.for_mopidy(uri, lambda: self.pi.images(uri)) if uri else None
            if self.source == "pi" and before.get("available"):
                self.wake()
        if self.menu.open and self.menu.stack[0].title == "Menu":
            self._refresh_root()
        self.request_render()

    def on_pi_error(self, message):
        self.toast(message if len(message) < 90 else "Mopidy didn't respond")

    def on_art(self, key, path):
        if key in (self.bt_art_key, self.pi_art_key):
            self.request_render()

    # == Bluetooth events =============================================================
    def on_bt(self, changes):
        now = time.monotonic()
        if "_present" in changes:
            self.present = bool(changes["_present"])
        if "Device" in changes:
            phone = changes["Device"] or ""
            if phone and phone != self.phone:
                log.info("%s connected", phone)
                self.wake()
                if self.boot_hold_until:
                    self.boot_saw_phone = True
            elif not phone and self.phone:
                log.info("%s disconnected", self.phone)
                self.np.clear()
                self.last_playing_at = 0.0
                self.toast(f"{self.phone} disconnected")
            self.phone = phone
        if "Player" in changes:
            self.has_player = bool(changes["Player"])
        if "Track" in changes and self.np.apply_track(changes["Track"]):
            log.info("Now playing: %s - %s", self.np.artist, self.np.title)
            self.bt_art_key = (self.art.for_phone(self.np.artist, self.np.album, self.np.title)
                               if self.np.has_track else None)
            self.wake()
        was_playing = self.np.playing
        if "Status" in changes:
            self.np.apply_status(changes["Status"], now)
            self.wake()
        if "Position" in changes:
            self.np.apply_position(changes["Position"], now)
        if self.np.playing:
            self.last_playing_at = now
            self.play_pending_until = 0.0
            self.set_last_used("phone")
            if self.pi and self.source != "bluetooth" and not was_playing:
                self.switch_to("bluetooth", play=False)  # music started on the phone: it takes over
        if "Connecting" in changes:
            self.connecting = bool(changes["Connecting"])
            if self.connecting and self.boot_hold_until:
                self.boot_saw_connecting = True
        if changes.get("Ready"):
            self.bt_ready = True
        if "Pairing" in changes:
            self.pairing = bool(changes["Pairing"])
            if self.pairing:
                self.wake()
            elif not self.confirm:
                self.info_until = 0.0
        if "Alias" in changes:
            self.alias = changes["Alias"] or ""
        if "Paired" in changes:
            self.paired = list(changes["Paired"] or [])
        if self.play_pending_until and self.phone and self.has_player:
            # A was pressed while the phone was still connecting: now it can be told to play.
            self.play_pending_until = 0.0
            self.bt.command("play", on_error=lambda _m: self.toast("Your phone didn't respond"))
        if self.menu.open and self.menu.stack[0].title == "Menu" and ("Device" in changes or "Paired" in changes):
            self._refresh_root()
        self._update_ownership()
        self.request_render()

    def on_agent(self, kind, phone, text):
        if kind in ("confirm", "display"):
            log.info("Pairing code for %s: %s", phone, text)
            self.confirm = {"phone": phone, "code": text}
            self.wake()
        elif kind == "cancel":
            self.confirm = None
            self.toast("Pairing cancelled")
        elif kind == "paired":
            self.confirm = None
            self.hold_until = time.monotonic() + self.handback_after
            self.toast(f"Paired with {phone}")
        elif kind == "failed":
            self.confirm = None
            self.toast(text or "Pairing didn't work")
        self._update_ownership()
        self.request_render()

    # == buttons =======================================================================
    def _button_from_thread(self, name, kind):
        GLib.idle_add(self.on_button, name, kind)

    def on_button(self, name, kind):
        log.debug("button %s %s", name, kind)
        if not self.hw_owned:
            return False
        if kind == "press":
            self._repeats[name] = 0
        elif kind == "repeat":
            self._repeats[name] = self._repeats.get(name, 0) + 1
        if self.asleep:
            self.wake()  # the first press only wakes the screen...
            # ...including the rest of that press (X's release, or B/Y/X held down)
            self._swallow = name if kind == "press" else None
            return False
        if self._swallow == name and kind != "press":
            if kind == "short":
                self._swallow = None
            return False
        self._swallow = None
        self.last_activity = time.monotonic()
        info_open = self.info_until > time.monotonic()

        if self.confirm:  # the pairing-code screen: A = pair, B = cancel
            if name == "A" and kind == "short":
                self.confirm = None
                self.bt.answer(True)
                self.toast("Now tap Pair on your phone")
            elif name == "B" and kind == "press":
                self.confirm = None
                self.bt.answer(False)
                self.toast("Pairing cancelled")
        elif info_open:
            if name == "A" and kind == "short":
                self.info_until = 0.0
            elif name == "X" and kind == "short":
                self.info_until = 0.0
                self.bt.start_pairing()
            elif name in ("B", "Y") and kind in ("press", "repeat"):
                self.change_volume(self.step if name == "Y" else -self.step)
        elif self.pairing and name == "B" and kind == "press":
            self.bt.stop_pairing()
            self.toast("Pairing cancelled")
        elif self.menu.open:
            self.menu_button(name, kind)
        elif name == "A" and kind == "short":
            self.play_pause()
        elif name == "A" and kind == "long":
            if self.pi or self.unified:
                self.open_menu()
            else:
                self.info_until = time.monotonic() + 10.0
        elif name == "X" and kind == "short" and self.boot_hold_until and not self.np.playing:
            self.end_boot_hold("Switching to the Pi's music…")
        elif name == "X" and kind in ("short", "long"):
            self.skip("next" if kind == "short" else "previous")
        elif name in ("B", "Y") and kind in ("press", "repeat"):
            self.change_volume(self.step if name == "Y" else -self.step)
        self._update_ownership()
        self.request_render()
        return False

    def play_pause(self):
        now = time.monotonic()
        if self.pi and self.source == "pi":
            if self.phone and self.np.playing:
                self.bt.command("pause")
            self.pi.run(self._pi_after_dac, self.pi.play_pause, on_done=self._pi_resumed)
            return
        if self.phone and self.has_player:
            self.bt.command("playpause", on_error=lambda _m: self.toast("Your phone didn't respond"))
        elif self.phone:  # connected a moment ago; its media controls show up a second or two later
            self.play_pending_until = now + PLAY_WAIT_S
            self.toast(f"Starting {self.phone}…")
        elif self.paired:
            if self.bt.start_reconnecting(PRESS_RECONNECT_S):
                self.play_pending_until = now + PRESS_RECONNECT_S + PLAY_WAIT_S
                self.toast(f"Connecting to {self.paired[0]}…")
            elif not self.present:
                self.toast("Bluetooth is off")
            else:
                self.toast("Pick it in your phone's Bluetooth list")
        else:
            self.toast("No phone paired yet: hold A, then pick Pair a new phone" if self.unified
                       else "No phone paired yet: hold A, then press X")

    def skip(self, action):
        if self.pi and self.source == "pi":
            self.pi.run(self.pi.next if action == "next" else self.pi.previous)
            return
        if not (self.phone and self.has_player):
            self.toast("Nothing is playing over Bluetooth")
            return
        self.bt.command(action, on_error=lambda _m: self.toast("Your phone didn't respond"))

    def change_volume(self, delta):
        if self.pi and self.source == "pi" and not self.info_until > time.monotonic():
            def shown(value):
                self.show_overlay(("volume", value, "Pi volume"), 1.6)
            self.pi.run(self.pi.change_volume, delta, on_done=shown)
            return
        value = self.volume.change(delta)
        if value is None:
            self.toast("Volume control isn't ready yet")
            threading.Thread(target=self.volume.ensure_control, daemon=True).start()
            return
        self.show_overlay(("volume", value, "Headphones"), 1.6)

    # == the music menu ===============================================================
    def _root_items(self):
        items = []
        if self.pi:
            if self.source == "pi":
                label = f"Switch to {self.phone or (self.paired[0] if self.paired else 'Bluetooth')}"
                items.append(Item(label, "action", action="to_bt"))
            else:
                items.append(Item("Switch to Pi music", "action", action="to_pi"))
            items += [
                Item("Artists", "dir", load=("browse", "local:directory?type=artist")),
                Item("Songs A–Z", "dir", load=("browse", "local:directory?type=track")),
                Item("Playlists", "dir", load=("playlists",)),
                Item("Folders", "dir", load=("folders",)),
                Item("Queue", "dir", load=("queue",)),
                Item("Repeat", "action", action="repeat", value=REPEAT_LABELS[_repeat_mode(self.pi_state)]),
                Item("Shuffle", "action", action="shuffle", value="On" if self.pi_state.get("random") else "Off"),
            ]
        items += [Item("Pair a new phone", "action", action="pair"), Item("Info", "action", action="info")]
        return items

    def open_menu(self):
        root = Page("Menu")
        root.set_items(self._root_items())
        self.menu.show(root)
        if self.pi:
            self.pi.fast = True
            self.pi.poke()
        self.request_render()

    def _refresh_root(self):
        root = self.menu.stack[0]
        root.set_items(self._root_items(), cursor=root.cursor)

    def menu_button(self, name, kind):
        page = self.menu.page
        if name == "X" and kind in ("press", "repeat"):
            page.move(-scroll_step(self._repeats.get("X", 0)) if kind == "repeat" else -1)
        elif name == "Y" and kind in ("press", "repeat"):
            page.move(scroll_step(self._repeats.get("Y", 0)) if kind == "repeat" else 1)
        elif name == "B" and kind == "press":
            if not self.menu.back():
                self.close_menu()
        elif name == "A" and kind == "short":
            self.menu_select(page.selected(), page)
        elif name == "A" and kind == "long":
            self.menu_add_to_queue(page.selected())

    def close_menu(self):
        self.menu.close()
        if self.pi:
            self.pi.fast = self.source == "pi"

    def menu_select(self, item, page):
        if item is None:
            return
        if item.kind == "dir":
            self.open_page(Page(item.label, load=item.load or ("browse", item.uri)))
        elif item.kind == "track":
            tracks = [i for i in page.items if i.kind == "track"]
            index = next(n for n, i in enumerate(tracks) if i is item)
            self.play_from_pi(self.pi.play_tracks, [t.uri for t in tracks], index)
        elif item.kind == "queued":
            self.play_from_pi(self.pi.play_tlid, item.tlid)
        elif item.action == "to_pi":
            self.close_menu()
            self.switch_to("pi")
        elif item.action == "to_bt":
            self.close_menu()
            self.switch_to("bluetooth")
        elif item.action == "repeat":
            def done(mode):
                item.value = REPEAT_LABELS[mode]
                self.request_render()
            self.pi.run(self.pi.cycle_repeat, on_done=done)
        elif item.action == "shuffle":
            def done(on):
                item.value = "On" if on else "Off"
                self.request_render()
            self.pi.run(self.pi.toggle_shuffle, on_done=done)
        elif item.action == "pair":
            self.close_menu()
            self.bt.start_pairing()
        elif item.action == "info":
            self.close_menu()
            self.info_until = time.monotonic() + 10.0

    def play_from_pi(self, fn, *args):
        """Start the Pi's music from the menu: pause the phone, wait for the sound card, then play."""
        self.close_menu()
        self.switch_to("pi", play=False)
        self.pi.run(self._pi_after_dac, fn, *args)

    def menu_add_to_queue(self, item):
        if item is None or item.kind not in ("track", "dir") or not self.pi:
            return

        def job():
            if item.kind == "track":
                uris = [item.uri]
            else:
                uris = [i.uri for i in self._load_items(item.load or ("browse", item.uri))[0] if i.kind == "track"]
            return self.pi.add_tracks(uris) if uris else 0

        def done(count):
            self.toast("Added to the queue" if count == 1 else f"Added {count} songs to the queue" if count
                       else "No songs in there to add")
        self.pi.run(job, on_done=done)

    def open_page(self, page):
        page.loading = True
        self.menu.push(page)

        def done(result):
            items, cursor = result
            page.set_items(items, cursor)
            if not items:
                page.error = "Nothing here yet"
            self.request_render()

        def failed(message):
            page.loading = False
            page.error = message
            self.request_render()

        self.pi.run(self._load_items, page.load, on_done=done, on_error=failed)

    def _load_items(self, load):
        """On the Mopidy worker: (items, cursor) for a page."""
        from .mopidy import MopidyError

        kind = load[0]
        if kind == "queue":
            current, tl_tracks = self.pi.queue()
            items, cursor = [], 0
            for n, tl in enumerate(tl_tracks):
                track = tl.get("track") or {}
                artists = ", ".join(a.get("name", "") for a in track.get("artists") or [])
                name = track.get("name") or track.get("uri", "").rsplit("/", 1)[-1]
                items.append(Item(f"{name} · {artists}" if artists else name, "queued", uri=track.get("uri", ""),
                                  tlid=tl["tlid"], current=tl["tlid"] == current))
                if tl["tlid"] == current:
                    cursor = n
            return items, cursor
        if kind == "playlists":
            refs = self.pi.playlists()
            return [Item(r.get("name", "?"), "dir", uri=r["uri"], load=("playlist", r["uri"])) for r in refs], 0
        if kind == "playlist":
            refs = self.pi.playlist_items(load[1])
        elif kind == "folders":
            roots = [r for r in self.pi.roots() if r.get("uri", "").startswith("file:")]
            if not roots:
                raise MopidyError("Mopidy's Files view is switched off")
            refs = self.pi.browse(roots[0]["uri"]) if len(roots) == 1 else roots
        else:
            uri = load[1]
            refs = self.pi.browse(uri)
            if not refs and uri.startswith("local:"):
                if not any(r.get("uri", "").startswith("local:") for r in self.pi.roots()):
                    raise MopidyError("Mopidy's music library is off. On the Pi, run: pirate-bluetooth library on")
        items = []
        for r in refs:
            name = r.get("name") or r.get("uri", "?").rsplit("/", 1)[-1]
            if r.get("type") == "track":
                items.append(Item(name, "track", uri=r["uri"]))
            elif r.get("type") == "playlist":
                items.append(Item(name, "dir", uri=r["uri"], load=("playlist", r["uri"])))
            else:
                items.append(Item(name, "dir", uri=r["uri"], load=("browse", r["uri"])))
        return items, 0

    # == overlays / sleep ===================================================================
    def show_overlay(self, overlay, seconds):
        self.overlay = overlay
        self.overlay_until = time.monotonic() + seconds
        self.request_render()

    def toast(self, text):
        log.info("toast: %s", text)
        self.show_overlay(("toast", text), 2.8)
        return False

    def wake(self):
        self.last_activity = time.monotonic()
        if self.asleep:
            self.asleep = False
            log.info("Screen on")
            if self.display:
                self.display.backlight(True)
        self.request_render()

    def music_playing(self) -> bool:
        return bool(self.np.playing or (self.pi_state.get("state") == "playing"))

    def _tick(self):
        now = time.monotonic()
        dirty = False
        if self.overlay and now >= self.overlay_until:
            self.overlay = None
            dirty = True
        if self.info_until and now >= self.info_until:
            self.info_until = 0.0
            dirty = True
        if self.play_pending_until and now >= self.play_pending_until:
            self.play_pending_until = 0.0
            if self.phone:
                self.toast("Start the music on your phone")
            elif self.paired:
                self.toast(f"Couldn't reach {self.paired[0]}. Is its Bluetooth on?")
        if self.np.playing:
            self.last_playing_at = now
            dirty = dirty or self.np.progress is not None
        if self.source == "pi" and self.pi_state.get("state") == "playing":
            dirty = True  # progress bar
        if self.pairing:
            dirty = True  # countdown
        if self.pi:  # look at Mopidy often only while its music is (or its menu is) on a lit screen
            self.pi.fast = (self.source == "pi" or self.menu.open) and not self.asleep
        # Stay lit while waiting for the first press after power-on: a press on a dark screen only wakes it.
        busy = self.music_playing() or self.pairing or self.confirm or self.boot_hold_until
        if (self.hw_owned and not self.asleep and self.sleep_after > 0 and not busy
                and now - self.last_activity > self.sleep_after):
            log.info("Screen off (idle)")
            self.asleep = True
            self.menu.close()
            self.display.backlight(False)
        self._update_ownership()
        if dirty:
            self.request_render()
        return True

    # == drawing ================================================================================
    def request_render(self):
        if not self._render_pending:
            self._render_pending = True
            GLib.idle_add(self._render)

    def build_view(self):
        now = time.monotonic()
        view = View(
            name=self.alias or self.cfg.get("bluetooth", "name"),
            phone=self.phone,
            last_phone=self.paired[0] if self.paired else "",
            overlay=self.overlay if (self.overlay and now < self.overlay_until) else None,
            connecting=self.connecting and not self.phone,
            choice=bool(self.boot_hold_until) and bool(self.mopidy.enabled) and not self.unified,
            source=self.source if self.unified else "",
        )
        if self.confirm:
            view.kind = "confirm"
            view.phone = self.confirm["phone"]
            view.code = self.confirm["code"]
        elif self.info_until > now:
            vol = self.volume.get()
            view.kind = "info"
            view.info = [
                ("Bluetooth name", view.name),
                ("Connected", self.phone or ("looking for " + self.paired[0] if self.connecting and self.paired
                                             else "nothing" if self.present else "Bluetooth is off")),
                ("Paired phones", ", ".join(self.paired) if self.paired else "none yet"),
                ("Headphone volume", f"{vol}%" if vol is not None else "not ready"),
                (("Add music at", f"{local_ip()}:{self.cfg.getint('upload', 'port')}")
                 if self.cfg.getboolean("upload", "enabled") else ("IP address", local_ip())),
            ]
        elif self.pairing:
            view.kind = "pairing"
            left = max(0, int(self.bt.pairing_until - now))
            view.countdown = format_time(left)
        elif self.menu.open:
            page = self.menu.page
            view.kind = "menu"
            view.title = page.title
            view.menu = [{"label": i.label, "kind": i.kind, "value": i.value, "current": i.current}
                         for i in page.visible()]
            view.menu_cursor = page.cursor - page.offset
            view.menu_offset, view.menu_total = page.offset, len(page.items)
            view.menu_note = "Loading…" if page.loading else page.error
        elif self.pi and self.source == "pi":
            self._pi_view(view, now)
        elif self.phone and self.np.has_track:
            view.kind = "playing"
            view.badge = self.phone
            view.title, view.artist, view.album = self.np.title, self.np.artist, self.np.album
            view.playing = self.np.playing
            view.art = self.art.known(self.bt_art_key) or "" if self.bt_art_key else ""
            p = self.np.progress
            if p is not None and p.duration > 0:
                pos = p.now(self.np.playing, now)
                view.progress = pos / p.duration
                view.elapsed_text = format_time(pos)
                view.remaining_text = "-" + format_time(p.duration - pos)
        elif self.phone:
            view.kind = "connected"
        else:
            view.kind = "idle"
        return view

    def _pi_view(self, view, now):
        st = self.pi_state
        track = st.get("track")
        if not st.get("available"):
            view.kind, view.title = "pi_idle", "Waiting for Mopidy…"
            return
        if not track:
            view.kind, view.title = "pi_idle", "Nothing queued"
            return
        view.kind = "playing"
        view.badge = "Pi music"
        view.title, view.artist, view.album = track["title"], track["artist"], track["album"]
        view.playing = st.get("state") == "playing"
        view.art = self.art.known(self.pi_art_key) or "" if self.pi_art_key else ""
        view.repeat = _repeat_mode(st)
        view.shuffle = bool(st.get("random"))
        length = (track.get("length") or 0) / 1000
        if length > 0:
            pos = st.get("position", 0) / 1000 + ((now - st.get("at", now)) if view.playing else 0)
            pos = max(0.0, min(pos, length))
            view.progress = pos / length
            view.elapsed_text = format_time(pos)
            view.remaining_text = "-" + format_time(length - pos)
        if st.get("state") == "stopped":
            view.playing = False

    def _render(self):
        self._render_pending = False
        if not self.hw_owned or self.asleep:
            return False
        try:
            self.display.show(self.renderer.render(self.build_view()))
        except Exception:
            log.exception("Drawing failed")
        return False

    # == shutdown ==================================================================================
    def quit(self, *_):
        log.info("Shutting down")
        self._stop.set()
        self.bt.close()
        if self.pi:
            self.pi.close()
        stopping = shutting_down()
        if not self.mopidy.stopped_by_us:
            self.mopidy.save_now()  # so the queue is back after the next power-on
        self.release(handback_screen=not stopping)
        if stopping:
            # Powering off or rebooting: don't start Mopidy now. It starts by itself at the next boot,
            # and its queue is put back then.
            log.info("The Pi is shutting down; Mopidy's queue is saved for next time")
        else:
            self.mopidy.give_back()
            self.mopidy.wait_restore(timeout=60)  # don't cut Mopidy's queue restore short
        self.loop.quit()
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pirate_bluetooth",
                                     description="Bluetooth now-playing screen + buttons for Pirate Audio")
    parser.add_argument("--config", default=os.path.expanduser("~/.config/pirate-bluetooth/settings.ini"))
    parser.add_argument("--bus", choices=("system", "session"), help="D-Bus bus BlueZ is on (testing)")
    parser.add_argument("--mopidy", choices=("auto", "on", "off"), help="share the screen with Mopidy")
    parser.add_argument("--preview", metavar="DIR", help="draw to DIR/frame.png instead of the LCD")
    parser.add_argument("--record", metavar="FILE", help="(testing) log views as JSON lines")
    parser.add_argument("--fifo", metavar="PATH", help="(testing) read button presses from a FIFO")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    log.info("pirate-bluetooth %s starting", __version__)

    loop = GLib.MainLoop()
    app = App(load_config(args.config), args, loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, app.quit)
    loop.run()
