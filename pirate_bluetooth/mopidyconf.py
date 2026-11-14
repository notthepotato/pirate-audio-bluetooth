"""Reading (and, only when asked, changing) Pimoroni's Mopidy setup: its user service and config files.

Only uses the standard library, so the installer can run it with the system Python.

    python3 -m pirate_bluetooth.mopidyconf get pidi enabled
    python3 -m pirate_bluetooth.mopidyconf set pidi enabled false   (prints the file it changed)
"""

import configparser
import os
import re
import shlex
import shutil
import subprocess
import sys


def service_command():
    """The Mopidy user service's command line, e.g. ['/home/pi/.virtualenvs/mopidy/bin/mopidy', '--config', 'a:b']."""
    try:
        res = subprocess.run(["systemctl", "--user", "cat", "mopidy"], capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("ExecStart=") and line != "ExecStart=":
            argv = shlex.split(line[len("ExecStart="):])
            if argv:
                argv[0] = argv[0].lstrip("-@:+!")
                return argv
    return None


def scan_command(argv=None):
    """'mopidy --config <same files as the service> local scan', run at low priority."""
    argv = argv or service_command()
    return ["nice", "-n", "10", *argv, "local", "scan"] if argv else None


def config_files(argv=None):
    """Mopidy's config files in the order the service loads them (later ones win)."""
    argv = argv or service_command() or []
    files = []
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            files += argv[i + 1].split(":")
        elif arg.startswith("--config="):
            files += arg.split("=", 1)[1].split(":")
    return [os.path.expanduser(f) for f in files if f] or [os.path.expanduser("~/.config/mopidy/mopidy.conf")]


def read(files):
    cfg = configparser.RawConfigParser(inline_comment_prefixes=(";",), strict=False)
    try:
        cfg.read(files, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        return None
    return cfg


def get(files, section, key, default=None):
    cfg = read(files)
    if cfg is None:
        return default
    return cfg.get(section, key, fallback=default)


def extension_enabled(files, name):
    """Is a Mopidy extension switched on in the config files? None if they can't be read.

    (An extension that isn't installed counts as on here; Mopidy simply ignores its section.)"""
    cfg = read(files)
    if cfg is None:
        return None
    return cfg.get(name, "enabled", fallback="true").strip().lower() not in ("false", "no", "off", "0")


def set_option(path, section, key, value, backup_suffix=".before-pirate-bluetooth"):
    """Set `key = value` in [section] of one config file, keeping a copy of the original once.

    The line goes right under the section header and any older `key = ...` lines in that section are
    dropped. A second [section] header is never added: Mopidy refuses to start with duplicate sections."""
    backup = path + backup_suffix
    if os.path.exists(path) and not os.path.exists(backup):
        shutil.copy2(path, backup)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except FileNotFoundError:
        lines = []
    out, current, added = [], None, False
    key_re = re.compile(rf"\s*{re.escape(key)}\s*=")
    for line in lines:
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            current = m.group(1).strip()
            out.append(line)
            if current == section and not added:
                out.append(f"{key} = {value}")
                added = True
            continue
        if current == section and key_re.match(line):
            continue
        out.append(line)
    if not added:
        out += ["", f"[{section}]", f"{key} = {value}", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return backup


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    files = config_files()
    if len(args) == 3 and args[0] == "get":
        value = get(files, args[1], args[2])
        print("" if value is None else value)
        return 0
    if len(args) == 4 and args[0] == "set":
        target = files[-1]  # the last file Mopidy reads has the final say
        set_option(target, args[1], args[2], args[3])
        print(target)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
