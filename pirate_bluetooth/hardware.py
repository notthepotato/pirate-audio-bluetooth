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

    A and X: "short" on release, or "long" once when held.
    B and Y: "press" immediately, then "repeat" while held (volume ramps).
    """

    def __init__(self, on_event, y_pin: int = 24, hold_time: float = 0.7, repeat_time: float = 0.25):
        from gpiozero import Button

        pins = {"A": 5, "B": 6, "X": 16, "Y": y_pin}
        self._buttons = []
        self._held = {}
        try:
            for name, pin in pins.items():
                if name in ("B", "Y"):
                    b = Button(pin, pull_up=True, bounce_time=0.03, hold_time=repeat_time,
                               hold_repeat=True)
                    b.when_pressed = _callback(on_event, name, "press")
                    b.when_held = _callback(on_event, name, "repeat")
                else:
                    b = Button(pin, pull_up=True, bounce_time=0.03, hold_time=hold_time)
                    self._held[name] = False
                    b.when_pressed = _callback(self._pressed, name)
                    b.when_held = _callback(self._long, name, on_event)
                    b.when_released = _callback(self._released, name, on_event)
                self._buttons.append(b)
        except Exception:
            self.close()
            raise

    def _pressed(self, name):
        self._held[name] = False

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
