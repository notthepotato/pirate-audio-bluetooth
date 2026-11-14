"""pirate-bluetooth: status, pairing and housekeeping from the command line (e.g. over SSH).

Talks to BlueZ directly over D-Bus instead of using bluetoothctl, because bluetoothctl registers
its own pairing agent, which would take the code-confirmation step away from the Pirate Audio screen.
"""

import argparse
import configparser
import os
import re
import shutil
import socket
import subprocess
import sys

from gi.repository import Gio, GLib

from .system import MopidyHandoff
from .upload import _mopidy_execstart, mopidy_config_files, mopidy_library_enabled

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
SETTINGS = os.path.expanduser("~/.config/pirate-bluetooth/settings.ini")
# Read by the pirate-bluealsa-aplay service. PIRATE_TEST_ROOT is only set by the test suite.
LATENCY_FILE = os.environ.get("PIRATE_TEST_ROOT", "") + "/etc/default/pirate-bluetooth-latency"
APLAY_UNIT = "pirate-bluealsa-aplay"
# bluealsa-aplay's playback buffer (microseconds): it starts playing once the buffer is full, so this is
# the delay the Pi adds. Keep in step with latency_opts() in install.sh.
LATENCY_LEVELS = {  # buffer µs, period µs, delay on the Pi, note
    "low": (100000, 20000, "about 0.1 s on the Pi", "snappiest; may crackle while Wi-Fi is busy"),
    "normal": (200000, 40000, "about 0.2 s on the Pi", "the default"),
    "safe": (500000, 100000, "about 0.5 s on the Pi", "BlueALSA's own default; most robust"),
}


class Bluez:
    def __init__(self, bus="system"):
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION if bus == "session" else Gio.BusType.SYSTEM)
        reply = self.conn.call_sync(BLUEZ, "/", "org.freedesktop.DBus.ObjectManager",
                                    "GetManagedObjects", None, None, Gio.DBusCallFlags.NONE, 5000, None)
        self.objects = reply.unpack()[0]
        adapters = sorted(p for p, i in self.objects.items() if ADAPTER in i)
        if not adapters:
            sys.exit("No Bluetooth adapter found. Is Bluetooth turned on? (sudo rfkill unblock bluetooth)")
        self.adapter = adapters[0]

    def adapter_props(self):
        return self.objects[self.adapter][ADAPTER]

    def devices(self, paired_only=True):
        out = []
        for path, ifaces in sorted(self.objects.items()):
            dev = ifaces.get(DEVICE)
            if dev and (dev.get("Paired") or not paired_only):
                out.append((path, dev))
        return out

    def set(self, path, iface, prop, variant):
        self.conn.call_sync(BLUEZ, path, "org.freedesktop.DBus.Properties", "Set",
                            GLib.Variant("(ssv)", (iface, prop, variant)), None,
                            Gio.DBusCallFlags.NONE, 5000, None)

    def call(self, path, iface, method, args=None):
        self.conn.call_sync(BLUEZ, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 10000, None)


def upload_port():
    cfg = configparser.ConfigParser()
    cfg.read(SETTINGS)
    return cfg.getint("upload", "port", fallback=6681)


def current_latency():
    try:
        with open(LATENCY_FILE, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PIRATE_LATENCY="):
                    level = line.split("=", 1)[1].strip()
                    if level in LATENCY_LEVELS:
                        return level
    except OSError:
        pass
    return "safe"  # no setting: bluealsa-aplay uses its built-in 0.5 s buffer


def cmd_latency(args):
    if not args.level:
        now = current_latency()
        print(f"Bluetooth delay: {now} ({LATENCY_LEVELS[now][2]})")
        print()
        print("Change it with:  pirate-bluetooth latency low|normal|safe")
        for level, (_b, _p, delay, note) in LATENCY_LEVELS.items():
            print(f"  {level:<7} {delay}: {note}")
        print("Your phone and the Bluetooth link add roughly another 0.1-0.2 s that the Pi can't change.")
        return
    buffer_us, period_us, delay, _note = LATENCY_LEVELS[args.level]
    content = ("# Written by the Pirate Audio Bluetooth add-on. Change with: pirate-bluetooth latency low|normal|safe\n"
               f"PIRATE_LATENCY={args.level}\n"
               f'PIRATE_LATENCY_OPTS="--pcm-buffer-time={buffer_us} --pcm-period-time={period_us}"\n')
    print("Changing the Bluetooth audio delay needs your password (sudo).")
    res = subprocess.run(["sudo", "tee", LATENCY_FILE], input=content, text=True, stdout=subprocess.DEVNULL)
    if res.returncode != 0:
        sys.exit(f"Couldn't write {LATENCY_FILE}")
    res = subprocess.run(["sudo", "systemctl", "restart", APLAY_UNIT])
    if res.returncode != 0:
        sys.exit(f"Saved, but restarting {APLAY_UNIT} failed. Try: sudo systemctl restart {APLAY_UNIT}")
    print(f"Bluetooth delay is now {args.level} ({delay}).")
    if args.level == "low":
        print("If you hear crackles or dropouts, go back with:  pirate-bluetooth latency normal")


# == logs ==================================================================================
# Raspberry Pi OS keeps its journal in memory, where "journalctl --user -u ..." finds nothing; the user
# services' lines are in the system journal instead, under _SYSTEMD_USER_UNIT.
LOG_PARTS = {
    "screen": [("_SYSTEMD_USER_UNIT", "pirate-bluetooth.service")],
    "upload": [("_SYSTEMD_USER_UNIT", "pirate-upload.service")],
    "audio": [("_SYSTEMD_UNIT", "pirate-bluealsa.service"), ("_SYSTEMD_UNIT", "pirate-bluealsa-aplay.service")],
    "mopidy": [("_SYSTEMD_USER_UNIT", "mopidy.service")],
}


def cmd_logs(args):
    parts = [args.part] if args.part else ["screen", "upload", "audio"]
    matches = []
    for part in parts:
        for field, unit in LOG_PARTS[part]:
            matches += (["+"] if matches else []) + [f"{field}={unit}"]
    cmd = ["journalctl", "--no-pager", *(["-f"] if args.follow else ["-n", str(args.lines)]), *matches]
    if os.geteuid() != 0:
        cmd.insert(0, "sudo")  # reading the system journal needs it unless you're in the adm group
    os.execvp(cmd[0], cmd)


# == Mopidy's music library (Mopidy-Local) ===================================================
def switch_on_local(path):
    """Put 'enabled = true' in the [local] section of one Mopidy config file (backed up first)."""
    backup = path + ".before-library-on"
    if os.path.exists(path) and not os.path.exists(backup):
        shutil.copy2(path, backup)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except FileNotFoundError:
        lines = []
    out, section, added = [], None, False
    for line in lines:
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            section = m.group(1).strip()
            out.append(line)
            if section == "local" and not added:
                out.append("enabled = true")
                added = True
            continue
        if section == "local" and re.match(r"\s*enabled\s*=", line):
            continue  # an old enabled = ... line in the same section
        out.append(line)
    if not added:  # a second [local] section would stop Mopidy from starting, so only add one if none
        out += ["", "[local]", "enabled = true", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return backup


def cmd_library(args):
    argv = _mopidy_execstart()
    if not argv:
        sys.exit("Mopidy isn't set up as a user service on this Pi.")
    files = mopidy_config_files(argv)
    state = mopidy_library_enabled(files)
    if not args.action:
        print(f"Mopidy's music library (Mopidy-Local): {({True: 'on', False: 'off'}).get(state, 'unknown')}")
        if state is False:
            print("Songs only show in Iris under Browse > Files. Switch the library on with:  pirate-bluetooth library on")
        return

    if state:
        print("Mopidy-Local is already switched on in Mopidy's settings.")
    else:
        target = files[-1]  # Mopidy reads its files in order, so the last one has the final say
        backup = switch_on_local(target)
        print(f"Switched Mopidy-Local on in {target} (your old file: {backup})")

    running = subprocess.run(["systemctl", "--user", "is-active", "mopidy"], capture_output=True,
                             text=True).stdout.strip() == "active"
    if running:
        cfg = configparser.ConfigParser()
        cfg.read(SETTINGS)
        mopidy = MopidyHandoff(mode="on", rpc_url=cfg.get("mopidy", "rpc_url",
                                                         fallback="http://127.0.0.1:6680/mopidy/rpc"))
        snap = mopidy.snapshot(quiet=True)  # a restart empties Mopidy's queue; put it back afterwards
        print("Restarting Mopidy…")
        subprocess.run(["systemctl", "--user", "restart", "mopidy"])
        mopidy.restore(snap)
    else:
        print("Mopidy isn't running right now (Bluetooth may have it paused); the change applies when it starts.")

    print("Adding your music to the library (big collections take a while)…")
    res = subprocess.run(["nice", "-n", "10", *argv, "local", "scan"], capture_output=True, text=True)
    output = f"{res.stdout}\n{res.stderr}"
    if "disabled extension local" in output:
        reasons = [l.strip() for l in output.splitlines() if "Disabled extension local" in l
                   or "configuration errors" in l or l.strip().startswith("local/")]
        print("Mopidy still keeps Mopidy-Local switched off" + (": " + " ".join(reasons) if reasons else
              ". Run 'pirate-bluetooth logs mopidy' to see why."))
        sys.exit(1)
    for line in output.splitlines():
        if any(word in line for word in ("Failed scanning", "Done scanning", "Scanned", "Found")):
            print("  " + line.strip())
    print("Done. In Iris, your songs are under Browse > Local media (WAVs without tags are listed by file name)."
          if res.returncode == 0 else f"The library scan didn't finish (exit code {res.returncode}).")


def name_of(dev):
    return dev.get("Alias") or dev.get("Name") or dev.get("Address", "?")


def cmd_status(bz, _args):
    a = bz.adapter_props()
    print(f"Bluetooth name:  {a.get('Alias', '?')}")
    print(f"Powered:         {'yes' if a.get('Powered') else 'no'}")
    print(f"Pairing mode:    {'on' if a.get('Discoverable') else 'off'}")
    paired = bz.devices()
    if not paired:
        print("Paired phones:   none (run: pirate-bluetooth pair)")
    for i, (_path, dev) in enumerate(paired):
        label = "Paired phones:  " if i == 0 else "                "
        state = "connected" if dev.get("Connected") else "not connected"
        print(f"{label} {name_of(dev)} ({state})")
    level = current_latency()
    print(f"{'Bluetooth delay:':<17}{level} ({LATENCY_LEVELS[level][2]})")
    for unit in ("pirate-bluealsa", "pirate-bluealsa-aplay"):
        res = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
        print(f"{unit + ':':<23}{res.stdout.strip() or 'unknown'}")
    res = subprocess.run(["systemctl", "--user", "is-active", "pirate-upload"], capture_output=True, text=True)
    state = res.stdout.strip() or "unknown"
    port = upload_port()
    where = f" - http://{socket.gethostname()}.local:{port}" if state == "active" else ""
    print(f"{'Music upload page:':<23}{state}{where}")


def cmd_pair(bz, args):
    seconds = GLib.Variant("u", args.seconds)
    bz.set(bz.adapter, ADAPTER, "PairableTimeout", seconds)
    bz.set(bz.adapter, ADAPTER, "DiscoverableTimeout", seconds)
    bz.set(bz.adapter, ADAPTER, "Pairable", GLib.Variant("b", True))
    bz.set(bz.adapter, ADAPTER, "Discoverable", GLib.Variant("b", True))
    name = bz.adapter_props().get("Alias", "Pirate Audio")
    print(f"Pairing mode on for {args.seconds // 60} min {args.seconds % 60} s.")
    print(f"On your phone: Settings > Bluetooth > tap \"{name}\", then press A on the Pirate Audio")
    print("when it shows the same 6-digit code as your phone.")


def cmd_stop(bz, _args):
    bz.set(bz.adapter, ADAPTER, "Discoverable", GLib.Variant("b", False))
    bz.set(bz.adapter, ADAPTER, "Pairable", GLib.Variant("b", False))
    print("Pairing mode off.")


def cmd_forget(bz, args):
    targets = [(p, d) for p, d in bz.devices()
               if args.name == "all" or name_of(d).lower() == args.name.lower()]
    if not targets:
        names = ", ".join(name_of(d) for _p, d in bz.devices()) or "none"
        sys.exit(f"No paired phone called \"{args.name}\". Paired: {names}")
    for path, dev in targets:
        bz.call(bz.adapter, ADAPTER, "RemoveDevice", GLib.Variant("(o)", (path,)))
        print(f"Forgot {name_of(dev)}. (Also remove the Pirate Audio from that phone's Bluetooth list.)")


def cmd_name(bz, args):
    new = args.new_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9 ._()'-]{1,40}", new):
        sys.exit("Pick a name of 1-40 letters, numbers, spaces or . _ ( ) ' -")
    bz.set(bz.adapter, ADAPTER, "Alias", GLib.Variant("s", new))
    if os.path.exists(SETTINGS):  # keep the screen program from changing it back on restart
        with open(SETTINGS, encoding="utf-8") as fh:
            text = fh.read()
        text, n = re.subn(r"(?m)^name\s*=.*$", f"name = {new}", text, count=1)
        if n:
            with open(SETTINGS, "w", encoding="utf-8") as fh:
                fh.write(text)
    print(f"Renamed to \"{new}\". Phones that already paired may show the new name after reconnecting.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pirate-bluetooth",
                                     description="Bluetooth controls for the Pirate Audio add-on")
    parser.add_argument("--bus", choices=("system", "session"), default="system", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="show name, pairing mode and paired phones (default)")
    p = sub.add_parser("pair", help="turn on pairing mode so a new phone can find it")
    p.add_argument("seconds", nargs="?", type=int, default=180)
    sub.add_parser("stop", help="turn pairing mode off")
    f = sub.add_parser("forget", help='forget a paired phone ("all" forgets every phone)')
    f.add_argument("name")
    n = sub.add_parser("name", help="change the name phones see")
    n.add_argument("new_name")
    lat = sub.add_parser("latency", help="show or change how far the sound lags behind the phone")
    lat.add_argument("level", nargs="?", choices=list(LATENCY_LEVELS))
    lg = sub.add_parser("logs", help="show the add-on's log (screen, upload page, Bluetooth audio)")
    lg.add_argument("part", nargs="?", choices=list(LOG_PARTS))
    lg.add_argument("-f", "--follow", action="store_true", help="keep showing new lines")
    lg.add_argument("-n", "--lines", type=int, default=60)
    lib = sub.add_parser("library", help="show or switch on Mopidy's music library (Mopidy-Local)")
    lib.add_argument("action", nargs="?", choices=["on"])
    args = parser.parse_args(argv)

    # These don't need Bluetooth itself
    if args.cmd in ("latency", "logs", "library"):
        {"latency": cmd_latency, "logs": cmd_logs, "library": cmd_library}[args.cmd](args)
        return

    try:
        bz = Bluez(args.bus)
        {"pair": cmd_pair, "stop": cmd_stop, "forget": cmd_forget, "name": cmd_name}.get(
            args.cmd, cmd_status)(bz, args)
    except GLib.Error as err:
        sys.exit(f"Bluetooth error: {err.message}")


if __name__ == "__main__":
    main()
