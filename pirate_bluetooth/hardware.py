"""Pirate Audio hardware: the 240x240 ST7789 LCD and the four buttons.

Pin numbers (BCM) are Pimoroni's: LCD on SPI0 CS1 with DC=9 and backlight=13;
buttons A=5, B=6, X=16, Y=24 (Y is 20 on boards made before 23 Jan 2020).
Everything can be released again so Mopidy (PiDi + raspberry-gpio) can take over.
"""

import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

LCD_PORT, LCD_CS, LCD_DC, LCD_BACKLIGHT = 0, 1, 9, 13


def plain_error(err):
    """An exception as a short line, with the usual causes spelled out."""
    text = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
    low = text.lower()
    if "spidev" in low or ("no such file" in low and "spi" in low):
        text += " - SPI is off or the Pi hasn't been rebooted since installing"
    elif "permission denied" in low:
        text += " - your user isn't in the spi/gpio groups yet (reboot after installing)"
    elif "busy" in low:
        text += " - another program is using the pins (Mopidy's PiDi or raspberry-gpio?)"
    elif isinstance(err, ImportError):
        text += " - a Python library is missing (run install.sh again)"
    return text



class St7789Display:
    def __init__(self, rotation: int = 90, spi_mhz: int = 80):
        try:
            from st7789 import ST7789  # Pimoroni st7789 >= 1.0 (gpiod, works on Pi 5)
        except ImportError:
            from ST7789 import ST7789  # older Pimoroni library (RPi.GPIO)
        self._dev = ST7789(
            port=LCD_PORT,
            cs=LCD_CS,
            dc=LCD_DC,
            backlight=LCD_BACKLIGHT,
            rotation=rotation,
            spi_speed_hz=spi_mhz * 1_000_000,
        )
        self._lit = True

    def show(self, image):
        self._dev.display(image)

    def backlight(self, on: bool):
        if on != self._lit:
            self._dev.set_backlight(on)
            self._lit = on

    def close(self):
        dev, self._dev = self._dev, None
        if dev is None:
            return
        for attr in ("_dc", "_bl", "_rst"):
            pin = getattr(dev, attr, None)
            if isinstance(pin, tuple) and pin and hasattr(pin[0], "release"):
                try:
                    pin[0].release()
                except Exception as err:
                    log.debug("releasing %s: %s", attr, err)
        try:
            dev._spi.close()
        except Exception as err:
            log.debug("closing SPI: %s", err)


class PreviewDisplay:
    """Writes frames to PNG files instead of the LCD (for trying the UI without hardware)."""

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self.count = 0
        self.lit = True

    def show(self, image):
        self.count += 1
        tmp = os.path.join(self.dir, ".frame.png")
        image.save(tmp)
        os.replace(tmp, os.path.join(self.dir, "frame.png"))

    def backlight(self, on: bool):
        self.lit = on

    def close(self):
        pass


class RecordDisplay:
    """Test sink: appends each View (not a picture) as a JSON line."""

    def __init__(self, path: str):
        self.path = path
        self.lit = True

    def show(self, view):
        rec = dict(view.__dict__)
        rec["lit"] = self.lit
        rec["t"] = round(time.monotonic(), 3)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def backlight(self, on: bool):
        if on != self.lit:
            self.lit = on
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"backlight": on, "t": round(time.monotonic(), 3)}) + "\n")

    def close(self):
        pass


def _callback(fn, *args):
    # gpiozero inspects callbacks; a zero-argument closure keeps it from passing the device in.
    return lambda: fn(*args)


class GpioButtons:
    """Emits on_event(name, kind) from gpiozero's thread.

    A:    "short" on release, or "long" once when held.
    X:    "press" at once, then "repeat" while held (menus scroll), plus "short" on a quick release or
          "long" once when held (next / previous on the now-playing screen).
    B, Y: "press" at once, then "repeat" while held (volume ramps, menus scroll).
    """

    def __init__(self, on_event, y_pin: int = 24, hold_time: float = 0.7, repeat_time: float = 0.25):
        from gpiozero import Button

        pins = {"A": 5, "B": 6, "X": 16, "Y": y_pin}
        self._buttons = []
        self._held = {}
        self._pressed_at = {}
        self._hold_time = hold_time
        try:
            for name, pin in pins.items():
                if name in ("B", "Y"):
                    b = Button(pin, pull_up=True, bounce_time=0.03, hold_time=repeat_time,
                               hold_repeat=True)
                    b.when_pressed = _callback(on_event, name, "press")
                    b.when_held = _callback(on_event, name, "repeat")
                elif name == "X":
                    b = Button(pin, pull_up=True, bounce_time=0.03, hold_time=repeat_time,
                               hold_repeat=True)
                    self._held[name] = False
                    b.when_pressed = _callback(self._pressed, name, on_event)
                    b.when_held = _callback(self._repeat, name, on_event)
                    b.when_released = _callback(self._released, name, on_event)
                else:
                    b = Button(pin, pull_up=True, bounce_time=0.03, hold_time=hold_time)
                    self._held[name] = False
                    b.when_pressed = _callback(self._pressed, name, None)
                    b.when_held = _callback(self._long, name, on_event)
                    b.when_released = _callback(self._released, name, on_event)
                self._buttons.append(b)
        except Exception:
            self.close()
            raise

    def _pressed(self, name, on_event):
        self._held[name] = False
        self._pressed_at[name] = time.monotonic()
        if on_event:
            on_event(name, "press")

    def _repeat(self, name, on_event):
        on_event(name, "repeat")
        if not self._held[name] and time.monotonic() - self._pressed_at.get(name, 0) >= self._hold_time:
            self._long(name, on_event)

    def _long(self, name, on_event):
        self._held[name] = True
        on_event(name, "long")

    def _released(self, name, on_event):
        if not self._held.get(name):
            on_event(name, "short")
        self._held[name] = False

    def close(self):
        for b in self._buttons:
            try:
                b.close()
            except Exception:
                pass
        self._buttons = []


class _PressLogic:
    """The same short/long/press/repeat events as GpioButtons, from plain down/up edges plus a clock."""

    def __init__(self, on_event, hold_time, repeat_time):
        self._emit = on_event
        self._hold = hold_time
        self._repeat = repeat_time
        self._down = {}  # name -> {"at", "held", "next"}

    def down(self, name, now):
        if name in self._down:
            return
        self._down[name] = {"at": now, "held": False, "next": now + self._repeat}
        if name != "A":
            self._emit(name, "press")

    def up(self, name, now):
        state = self._down.pop(name, None)
        if state and name in ("A", "X") and not state["held"]:
            self._emit(name, "short")

    def tick(self, now):
        for name, state in list(self._down.items()):
            if name == "A":
                if not state["held"] and now - state["at"] >= self._hold:
                    state["held"] = True
                    self._emit(name, "long")
                continue
            while now >= state["next"]:
                state["next"] += self._repeat
                self._emit(name, "repeat")
                if name == "X" and not state["held"] and now - state["at"] >= self._hold:
                    state["held"] = True
                    self._emit(name, "long")


GPIO_CHIP_LABELS = ("pinctrl-rp1", "pinctrl-bcm2711", "pinctrl-bcm2835")  # Pi 5, Pi 4, older Pis


class GpiodButtons:
    """Buttons read with libgpiod directly (the same library the st7789 screen driver uses).

    Used when gpiozero can't get the button pins, so a gpiozero/lgpio problem doesn't leave the
    Pirate Audio without buttons."""

    def __init__(self, on_event, y_pin: int = 24, hold_time: float = 0.7, repeat_time: float = 0.25):
        import gpiod
        from datetime import timedelta
        from gpiod.line import Bias, Direction, Edge

        self._gpiod = gpiod
        self._names = {5: "A", 6: "B", 16: "X", y_pin: "Y"}
        chip = self._find_chip(gpiod)
        settings = gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP, edge_detection=Edge.BOTH,
                                      debounce_period=timedelta(milliseconds=20))
        try:
            self._request = chip.request_lines(consumer="pirate-bluetooth", config={tuple(self._names): settings})
        finally:
            chip.close()
        self._logic = _PressLogic(on_event, hold_time, repeat_time)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="buttons")
        self._thread.start()

    @staticmethod
    def _find_chip(gpiod):
        import glob
        found = {}
        for path in sorted(glob.glob("/dev/gpiochip*")):
            try:
                chip = gpiod.Chip(path)
                label = chip.get_info().label
            except OSError:
                continue
            if label in GPIO_CHIP_LABELS and label not in found:
                found[label] = chip
            else:
                chip.close()
        for label in GPIO_CHIP_LABELS:
            if label in found:
                for other, chip in found.items():
                    if other != label:
                        chip.close()
                return found[label]
        raise OSError("no Raspberry Pi GPIO chip found in /dev/gpiochip*")

    def _run(self):
        import select
        falling = self._gpiod.EdgeEvent.Type.FALLING_EDGE
        fd = self._request.fd
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.05)
                now = time.monotonic()
                if ready:
                    for ev in self._request.read_edge_events():
                        name = self._names.get(ev.line_offset)
                        if name:
                            (self._logic.down if ev.event_type == falling else self._logic.up)(name, now)
                self._logic.tick(now)
            except (OSError, ValueError) as err:
                if not self._stop.is_set():
                    log.error("Reading the buttons failed: %s", err)
                return

    def close(self):
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)
        try:
            self._request.release()
        except Exception:
            pass


def make_buttons(on_event, y_pin: int = 24, hold_time: float = 0.7):
    """gpiozero buttons, or libgpiod ones if gpiozero can't get the pins."""
    try:
        return GpioButtons(on_event, y_pin=y_pin, hold_time=hold_time)
    except Exception as first:
        try:
            buttons = GpiodButtons(on_event, y_pin=y_pin, hold_time=hold_time)
        except Exception as second:
            raise RuntimeError(f"gpiozero: {first}; libgpiod: {second}") from second
        log.info("Buttons read with libgpiod (gpiozero couldn't: %s)", first)
        return buttons


class FifoButtons:
    """Test/dev input: write lines like 'A short' or 'Y press' into a named pipe."""

    def __init__(self, on_event, path: str):
        self._path = path
        if not os.path.exists(path):
            os.mkfifo(path)
        self._stop = False
        self._thread = threading.Thread(target=self._run, args=(on_event,), daemon=True)
        self._thread.start()

    def _run(self, on_event):
        while not self._stop:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) == 2 and parts[0] in "ABXY":
                        on_event(parts[0], parts[1])

    def close(self):
        self._stop = True
