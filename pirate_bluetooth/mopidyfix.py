"""Fixes for running Mopidy 3.4.2 on Raspberry Pi OS Trixie, applied to the add-on's own Mopidy install.

GStreamer 1.25.0 to 1.26.2 (Trixie ships 1.26.2) changed how a caps structure's name is read, which makes
Mopidy 3.4.2's scanner crash when it meets a file it can't play, instead of skipping it. Mopidy 4 has the
fix (mopidy/mopidy pull request #2200); this applies the same few lines to 3.4.2.

Run with the Python of Mopidy's virtualenv:  python -m pirate_bluetooth.mopidyfix
"""

import importlib.util
import os
import sys

OLD = """                caps = missing_message.get_structure().get_value("detail")
                mime = caps.get_structure(0).get_name()
"""
NEW = """                caps = missing_message.get_structure().get_value("detail")
                struct = caps.get_structure(0)
                try:
                    mime = struct.get_name()
                except AttributeError:  # GStreamer 1.25.0-1.26.2 (pirate-bluetooth, Mopidy PR #2200)
                    with struct as _struct:
                        mime = _struct.get_name()
"""


def patch_file(path) -> str:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if NEW in text:
        return "already fixed"
    if OLD not in text:
        return "not needed (this Mopidy version is different)"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(OLD, NEW, 1))
    return "fixed"


def main():
    spec = importlib.util.find_spec("mopidy")  # finds it without importing it
    if spec is None or not spec.origin:
        print("Mopidy isn't installed for this Python", file=sys.stderr)
        return 1
    scan = os.path.join(os.path.dirname(spec.origin), "audio", "scan.py")
    if not os.path.exists(scan):
        print("Mopidy's scanner wasn't found; nothing to fix")
        return 0
    print(f"Mopidy scanner ({scan}): {patch_file(scan)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
