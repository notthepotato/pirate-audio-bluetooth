"""pirate-bluetooth: status, pairing and housekeeping from the command line (e.g. over SSH).

Talks to BlueZ directly over D-Bus instead of using bluetoothctl, because bluetoothctl registers
its own pairing agent, which would take the code-confirmation step away from the Pirate Audio screen.
"""

import argparse
import configparser
import os
import re
import socket
import subprocess
import sys

from gi.repository import Gio, GLib

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
SETTINGS = os.path.expanduser("~/.config/pirate-bluetooth/settings.ini")


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
    args = parser.parse_args(argv)

    try:
        bz = Bluez(args.bus)
        {"pair": cmd_pair, "stop": cmd_stop, "forget": cmd_forget, "name": cmd_name}.get(
            args.cmd, cmd_status)(bz, args)
    except GLib.Error as err:
        sys.exit(f"Bluetooth error: {err.message}")


if __name__ == "__main__":
    main()
