"""`pirate-bluetooth screen-test`: draw a test picture on the Pirate Audio's screen and show button presses.

Runs with the add-on's own Python (it needs the screen libraries), while the screen program is stopped.
"""

import sys
import time
import traceback


def _say(text):
    print(text, flush=True)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    seconds = float(args[0]) if args else 15.0
    y_pin = int(args[1]) if len(args) > 1 else 24
    rotation = int(args[2]) if len(args) > 2 else 90
    from . import hardware
    _plain_error = hardware.plain_error

    ok = True
    try:
        from PIL import Image, ImageDraw
        from .screen import Fonts
    except Exception as err:
        _say(f"Screen drawing libraries: FAILED - {_plain_error(err)}")
        return 1
    fonts = Fonts()

    def picture(lines):
        img = Image.new("RGB", (240, 240), (11, 13, 18))
        d = ImageDraw.Draw(img)
        for i, colour in enumerate([(230, 60, 60), (60, 200, 90), (70, 120, 240), (240, 210, 70), (255, 255, 255)]):
            d.rectangle((i * 48, 0, i * 48 + 47, 60), fill=colour)
        for n, text in enumerate(lines):
            d.text((14, 80 + n * 30), text, font=fonts.get(20 if n == 0 else 17, bold=n == 0), fill=(255, 255, 255))
        return img

    display = None
    try:
        display = hardware.St7789Display(rotation=rotation)
        display.backlight(True)
        display.show(picture(["Screen test", "Colour bars above?", "Press the buttons"]))
        _say("Screen: OK - it should show colour bars and 'Screen test' now.")
    except Exception as err:
        ok = False
        _say(f"Screen: FAILED - {_plain_error(err)}")
        traceback.print_exc(limit=3)

    pressed = []

    def on_event(name, kind):
        if kind in ("press", "short", "long"):
            pressed.append(f"{name} {kind}")
            _say(f"  button {name} ({kind})")
            if display:
                try:
                    display.show(picture(["Screen test", f"Button {name}", "Press the buttons"]))
                except Exception:
                    pass

    buttons = None
    try:
        buttons = hardware.make_buttons(on_event, y_pin=y_pin)
        kind = "libgpiod" if isinstance(buttons, hardware.GpiodButtons) else "gpiozero"
        _say(f"Buttons: OK (read with {kind}). Press A, B, X and Y within {seconds:.0f} seconds...")
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(0.1)
        seen = sorted({p.split()[0] for p in pressed})
        _say(f"Buttons pressed: {', '.join(seen) if seen else 'none'}")
        if len(seen) < 4:
            _say("  (A button that never shows up may be wired to another pin: boards made before 2020 use 20 for Y,"
                 " set y_pin = 20 in settings.ini)")
    except Exception as err:
        ok = False
        _say(f"Buttons: FAILED - {_plain_error(err)}")
        traceback.print_exc(limit=3)
    finally:
        if buttons:
            buttons.close()
        if display:
            display.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
