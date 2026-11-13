"""Main loop: Bluetooth state in, frames to the LCD and button presses out."""

import argparse
import configparser
import logging
import os
import signal
import threading
import time

from gi.repository import GLib

from . import __version__
from .bluetooth import BluetoothClient
from .state import NowPlaying, format_time
from .system import HeadphoneVolume, MopidyHandoff, local_ip
from .view import View

log = logging.getLogger("pirate_bluetooth")

DEFAULTS = {
    "bluetooth": {"name": "Pirate Audio", "pair_seconds": "180", "reconnect_on_start": "yes",
                  "auto_pair_if_none": "yes", "handback_after": "60", "bus": "system"},
    "display": {"rotation": "90", "spi_mhz": "80", "sleep_after": "120"},
    "buttons": {"y_pin": "24", "hold_time": "0.7"},
    "volume": {"step": "5", "card": "auto", "control": "Pirate Headphone", "device": "pirate_out"},
    "mopidy": {"handoff": "auto", "rpc_url": "http://127.0.0.1:6680/mopidy/rpc"},
    "upload": {"enabled": "yes", "port": "6681"},
}

STATE_DIR = os.path.join(os.path.expanduser("~"), ".local", "state", "pirate-bluetooth")
TICK_MS = 500
HW_RETRIES = 20


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
                                    state_file=os.path.join(STATE_DIR, "mopidy-handoff.json"))
        if self.mopidy.recover():
            self.mopidy.give_back()  # a previous run stopped Mopidy and didn't get to restart it

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
        )
        threading.Thread(target=self.volume.ensure_control, daemon=True).start()
        GLib.timeout_add(TICK_MS, self._tick)
        self._update_ownership()

    # == who owns the screen and buttons: us or Mopidy =============================
    def wants_hardware(self) -> bool:
        if not self.mopidy.enabled:
            return True  # no Mopidy to share with: always ours
        if self.pairing or self.confirm or time.monotonic() < self.hold_until:
            return True
        if self.phone and self.np.playing:
            return True
        # Keep it a little while after the music pauses, so A can resume it.
        return bool(self.phone and self.last_playing_at
                    and time.monotonic() - self.last_playing_at < self.handback_after)

    def _update_ownership(self):
        want = self.wants_hardware()
        if want and not self.hw_owned and not self._taking:
            if not self.mopidy.enabled:
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

    def release(self):
        if self._acquire_source:
            GLib.source_remove(self._acquire_source)
            self._acquire_source = 0
        if not self.hw_owned:
            return
        if self.buttons:
            self.buttons.close()
        if self.display:
            if self.mopidy.enabled:
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
        log.info("Display and buttons handed back")

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
            self.wake()
        if "Status" in changes:
            self.np.apply_status(changes["Status"], now)
            self.wake()
        if "Position" in changes:
            self.np.apply_position(changes["Position"], now)
        if self.np.playing:
            self.last_playing_at = now
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
        if self.asleep:
            self.wake()  # the first press only wakes the screen
            return False
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
        elif name == "A" and kind == "short":
            self.play_pause()
        elif name == "A" and kind == "long":
            self.info_until = time.monotonic() + 10.0
        elif name == "X" and kind in ("short", "long"):
            self.skip("next" if kind == "short" else "previous")
        elif name in ("B", "Y") and kind in ("press", "repeat"):
            self.change_volume(self.step if name == "Y" else -self.step)
        self._update_ownership()
        self.request_render()
        return False

    def play_pause(self):
        if self.phone and self.has_player:
            self.bt.command("playpause", on_error=lambda _m: self.toast("Your phone didn't respond"))
        elif self.phone:
            self.toast("Start the music on your phone")
        elif self.paired:
            self.toast(f"Connecting to {self.paired[0]}…")
            self.bt.reconnect()
        else:
            self.toast("No phone paired yet: hold A, then press X")

    def skip(self, action):
        if not (self.phone and self.has_player):
            self.toast("Nothing is playing over Bluetooth")
            return
        self.bt.command(action, on_error=lambda _m: self.toast("Your phone didn't respond"))

    def change_volume(self, delta):
        value = self.volume.change(delta)
        if value is None:
            self.toast("Volume control isn't ready yet")
            threading.Thread(target=self.volume.ensure_control, daemon=True).start()
            return
        self.show_overlay(("volume", value), 1.6)

    # == overlays / sleep ===================================================================
    def show_overlay(self, overlay, seconds):
        self.overlay = overlay
        self.overlay_until = time.monotonic() + seconds
        self.request_render()

    def toast(self, text):
        log.info("toast: %s", text)
        self.show_overlay(("toast", text), 2.8)

    def wake(self):
        self.last_activity = time.monotonic()
        if self.asleep:
            self.asleep = False
            if self.display:
                self.display.backlight(True)
        self.request_render()

    def _tick(self):
        now = time.monotonic()
        dirty = False
        if self.overlay and now >= self.overlay_until:
            self.overlay = None
            dirty = True
        if self.info_until and now >= self.info_until:
            self.info_until = 0.0
            dirty = True
        if self.np.playing:
            self.last_playing_at = now
            dirty = dirty or self.np.progress is not None
        if self.pairing:
            dirty = True  # countdown
        busy = self.np.playing or self.pairing or self.confirm
        if (self.hw_owned and not self.asleep and self.sleep_after > 0 and not busy
                and now - self.last_activity > self.sleep_after):
            log.info("Screen off (idle)")
            self.asleep = True
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
                ("Connected", self.phone or ("nothing" if self.present else "Bluetooth is off")),
                ("Paired phones", ", ".join(self.paired) if self.paired else "none yet"),
                ("Headphone volume", f"{vol}%" if vol is not None else "not ready"),
                (("Add music at", f"{local_ip()}:{self.cfg.getint('upload', 'port')}")
                 if self.cfg.getboolean("upload", "enabled") else ("IP address", local_ip())),
            ]
        elif self.pairing:
            view.kind = "pairing"
            left = max(0, int(self.bt.pairing_until - now))
            view.countdown = format_time(left)
        elif self.phone and self.np.has_track:
            view.kind = "playing"
            view.title, view.artist, view.album = self.np.title, self.np.artist, self.np.album
            view.playing = self.np.playing
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
        self.bt.close()
        self.release()
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
