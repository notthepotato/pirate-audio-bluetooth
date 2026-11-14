"""A way in when the Pi can't join your Wi-Fi: its own Wi-Fi hotspot, and what the screen and the music page
show about Wi-Fi and the USB cable connection.

* The hotspot comes on from the menu (Menu > Wi-Fi hotspot), from `pirate-bluetooth hotspot on`, or by
  itself after a few minutes without Wi-Fi. Join it from a phone or computer, then SSH to 10.42.0.1 or open
  the music page at http://10.42.0.1:<port>, where you can also add the Wi-Fi the Pi should use.
* A hotspot that came on by itself looks for your Wi-Fi again every few minutes while nobody is connected
  to it, and goes off once the Pi is back on your Wi-Fi.

This runs as the user service pirate-network (python3 -m pirate_bluetooth.network) and talks to
NetworkManager with nmcli. install.sh gives your user the NetworkManager permissions it needs (a polkit
rule), since a background service can't type a password.

Everything else (the menu, the music page, the pirate-bluetooth command) only asks: it writes what it wants
into network-request.json, and reads network.json, which this service keeps up to date. One place decides
what the Wi-Fi does, so the menu and the automatic hotspot never fight over it.
"""

import argparse
import configparser
import hashlib
import logging
import os
import secrets
import subprocess
import time

from .system import boot_id, read_json, write_json

log = logging.getLogger("pirate_network")

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".local", "state", "pirate-bluetooth")
SETTINGS = os.path.join(HOME, ".config", "pirate-bluetooth", "settings.ini")
REQUEST_FILE = os.path.join(STATE_DIR, "network-request.json")
PASSWORD_FILE = os.path.join(STATE_DIR, "hotspot-password")
APPLIED_FILE = os.path.join(STATE_DIR, "hotspot-applied.json")  # which name/password the hotspot was started with
ROOT = os.environ.get("PIRATE_TEST_ROOT", "")  # tests only
NMCLI = os.environ.get("PIRATE_NMCLI", "nmcli")  # tests only
HOTSPOT_CON = "pirate-hotspot"  # the NetworkManager connection the add-on makes for its hotspot
HOTSPOT_IP = "10.42.0.1"
USB_IFACE = "usb0"  # Raspberry Pi's rpi-usb-gadget
USB_SHARED_IP = "10.12.194.1"  # its address for the computer when the computer doesn't share its internet
POLL_S = 5
TRY_WIFI_S = 75  # an automatic hotspot steps aside this long to look for your Wi-Fi
FAILED_RETRY_S = 60
PW_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: easy to read off the screen


def _status_path():
    """network.json lives in memory (/run/user/...) when it can: it changes often, the SD card needn't know."""
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if os.path.isdir(run) and os.access(run, os.W_OK):
        return os.path.join(run, "pirate-bluetooth", "network.json")
    return os.path.join(STATE_DIR, "network.json")


STATUS_FILE = os.environ.get("PIRATE_NETWORK_STATUS") or _status_path()


# == for the menu, the music page and the command line ===========================================
def read_status():
    """What the network service last saw, or None if it isn't running (or hasn't said yet)."""
    st = read_json(STATUS_FILE)
    if not isinstance(st, dict):
        return None
    if time.time() - st.get("at", 0) > 600:  # the service stopped a while ago
        st["stale"] = True
    return st


def request(**changes):
    """Ask the network service for something: hotspot=True/False, auto=True/False, join=<uuid>, scan=True."""
    req = read_json(REQUEST_FILE) or {}
    now = time.time()
    if "hotspot" in changes:
        req.update(hotspot="on" if changes["hotspot"] else "off", hotspot_at=now, boot=boot_id())
    if "auto" in changes:
        req["auto"] = bool(changes["auto"])
    if changes.get("join"):
        req.update(join=str(changes["join"]), join_at=now)
        if req.get("hotspot") == "on":  # joining a Wi-Fi ends a hotspot switched on by hand
            req.update(hotspot="off", hotspot_at=now)
    if changes.get("scan"):
        req["scan_at"] = now
    req["seq"] = int(req.get("seq", 0)) + 1
    return write_json(REQUEST_FILE, req)


def hotspot_settings(cfg=None):
    """(name, password) of the hotspot. The password comes from settings.ini, or is made up once and kept."""
    cfg = cfg or load_config()
    name = (cfg.get("wifi", "hotspot_name", fallback="").strip()
            or cfg.get("bluetooth", "name", fallback="").strip() or "Pirate Audio")
    name = name.encode("utf-8")[:32].decode("utf-8", "ignore")  # Wi-Fi names are 32 bytes at most
    password = cfg.get("wifi", "hotspot_password", fallback="").strip()
    if not 8 <= len(password) <= 63:
        try:
            with open(PASSWORD_FILE, encoding="utf-8") as fh:
                password = fh.read().strip()
        except OSError:
            password = ""
        if not 8 <= len(password) <= 63:
            password = "".join(secrets.choice(PW_ALPHABET) for _ in range(10))
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(password + "\n")
            except OSError as err:
                log.warning("Couldn't save the hotspot password: %s", err)
    return name, password


def _settings_key(name, password):
    return hashlib.sha256(f"{name}\0{password}".encode("utf-8")).hexdigest()


def load_config(path=SETTINGS):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({"wifi": {"hotspot_name": "", "hotspot_password": "", "hotspot_after_minutes": "3",
                            "retry_wifi_minutes": "5"},
                   "bluetooth": {"name": "Pirate Audio"}, "upload": {"port": "6681", "enabled": "yes"}})
    try:
        cfg.read(path)
    except configparser.Error as err:
        log.warning("Couldn't read %s: %s", path, err)
    return cfg


def usb_port_name():
    try:
        with open(f"{ROOT}/proc/device-tree/model", encoding="utf-8", errors="replace") as fh:
            model = fh.read().strip("\0\n ")
    except OSError:
        return "the Pi's USB port"
    if "Zero" in model:
        return "the USB port (not PWR IN)"
    return "the USB-C port"


def usb_enabled():
    """Raspberry Pi's rpi-usb-gadget is switched on (it loads g_ether at boot)."""
    return os.path.exists(f"{ROOT}/etc/modules-load.d/usb-gadget.conf")


# == NetworkManager ==================================================================================
class NmError(Exception):
    pass


def _split(line):
    """One line of `nmcli -t -e yes` output: fields split at ':', with '\\:' and '\\\\' unescaped."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _plain(err):
    """nmcli's error, in words that say what to do."""
    text = str(err).strip().splitlines()[-1] if str(err).strip() else "nmcli failed"
    text = text.removeprefix("Error: ")
    if "Not authorized" in text or "Insufficient privileges" in text:
        return "NetworkManager didn't allow it (run install.sh again: it sets up the permission)"
    if "dnsmasq" in text:
        return "dnsmasq is missing (sudo apt install dnsmasq-base)"
    if "Secrets were required" in text or "802-1x" in text:
        return "wrong password, or the network wants a different kind of sign-in"
    if "No network with SSID" in text:
        return "that Wi-Fi isn't in range"
    return text


class Nm:
    """The few nmcli calls the add-on needs (always in English: LC_ALL=C)."""

    def __init__(self, nmcli=NMCLI):
        self.nmcli = nmcli
        self._env = dict(os.environ, LC_ALL="C")

    def run(self, *args, timeout=20, check=True):
        try:
            res = subprocess.run([self.nmcli, *args], capture_output=True, text=True, timeout=timeout,
                                 env=self._env)
        except FileNotFoundError:
            raise NmError("NetworkManager (nmcli) isn't installed")
        except subprocess.TimeoutExpired:
            raise NmError("NetworkManager didn't answer in time")
        if check and res.returncode != 0:
            raise NmError(_plain(res.stderr or res.stdout))
        return res.stdout

    def rows(self, fields, *args, **kw):
        names = fields.split(",")
        out = []
        for line in self.run("-t", "-e", "yes", "-f", fields, *args, **kw).splitlines():
            if line:
                out.append(dict(zip(names, _split(line))))
        return out

    def details(self, fields, *args):
        """`nmcli -t -f a,b connection show <x>` style output (one "name:value" per line) as a dict."""
        out = {}
        for line in self.run("-t", "-e", "yes", "-f", fields, *args).splitlines():
            parts = _split(line)
            if len(parts) >= 2:
                key = parts[0].split("[", 1)[0]
                out.setdefault(key, ":".join(parts[1:]))
        return out

    # -- reading --
    def wifi_device(self):
        """The Wi-Fi device: {"device", "state", "connection"}, or None."""
        for row in self.rows("DEVICE,TYPE,STATE,CONNECTION", "device", "status"):
            if row.get("TYPE") == "wifi":
                return {"device": row["DEVICE"], "state": row.get("STATE", ""), "connection": row.get("CONNECTION", "")}
        return None

    def active_on(self, device):
        """The connection active on a device: {"name", "uuid"} or None."""
        for row in self.rows("NAME,UUID,TYPE,DEVICE", "connection", "show", "--active"):
            if row.get("DEVICE") == device:
                return {"name": row.get("NAME", ""), "uuid": row.get("UUID", "")}
        return None

    def connection(self, uuid):
        d = self.details("connection.id,802-11-wireless.ssid,802-11-wireless.mode", "connection", "show", "uuid", uuid)
        return {"name": d.get("connection.id", ""), "ssid": d.get("802-11-wireless.ssid", ""),
                "mode": d.get("802-11-wireless.mode", "")}

    def device_ip(self, device):
        """("192.168.1.50", 24) for a device, or ("", 0)."""
        try:
            d = self.details("IP4.ADDRESS", "device", "show", device)
        except NmError:
            return "", 0
        addr = d.get("IP4.ADDRESS", "")
        if "/" in addr:
            ip, _, prefix = addr.partition("/")
            return ip, int(prefix) if prefix.isdigit() else 0
        return addr, 0

    def wifi_list(self, rescan="no"):
        out = []
        for row in self.rows("IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", rescan, timeout=30):
            if row.get("SSID"):
                out.append({"ssid": row["SSID"], "signal": int(row["SIGNAL"]) if row.get("SIGNAL", "").isdigit() else 0,
                            "security": row.get("SECURITY", "").strip(), "in_use": row.get("IN-USE", "").strip() == "*"})
        return out

    def saved_wifi(self):
        """Saved Wi-Fi networks (not hotspots): [{"name", "uuid", "ssid", "autoconnect"}]."""
        out = []
        for row in self.rows("NAME,UUID,TYPE,AUTOCONNECT", "connection", "show"):
            if row.get("TYPE") not in ("802-11-wireless", "wifi") or row.get("NAME") == HOTSPOT_CON:
                continue
            try:
                info = self.connection(row["UUID"])
            except NmError:
                continue
            if info["mode"] == "ap":
                continue
            out.append({"name": row["NAME"], "uuid": row["UUID"], "ssid": info["ssid"] or row["NAME"],
                        "autoconnect": row.get("AUTOCONNECT") == "yes"})
        return out

    def radio_on(self):
        return self.run("radio", "wifi").strip() == "enabled"

    # -- changing --
    def ensure_hotspot(self, device, name, password):
        """Make (or update) the add-on's hotspot connection: WPA2, 2.4 GHz, the Pi at 10.42.0.1."""
        settings = ["ifname", device, "ssid", name, "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                    "ipv4.method", "shared", "ipv4.addresses", f"{HOTSPOT_IP}/24", "ipv6.method", "disabled",
                    "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password, "wifi-sec.proto", "rsn",
                    "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable",
                    "connection.autoconnect", "no"]
        names = [r.get("NAME") for r in self.rows("NAME", "connection", "show")]
        if HOTSPOT_CON in names:
            self.run("connection", "modify", HOTSPOT_CON, *settings)
        else:
            self.run("connection", "add", "type", "wifi", "con-name", HOTSPOT_CON, *settings)

    def up(self, ident, wait=30):
        args = ["uuid", ident] if len(ident) == 36 and ident.count("-") == 4 else ["id", ident]
        self.run("--wait", str(wait), "connection", "up", *args, timeout=wait + 15)

    def down(self, name):
        self.run("connection", "down", "id", name, check=False)

    def rescan(self):
        self.run("device", "wifi", "rescan", check=False, timeout=20)

    def add_wifi(self, ssid, password, device="", security="", hidden=False):
        """Save a Wi-Fi network (updates it if one with that name is saved). Returns its uuid."""
        existing = {w["ssid"]: w for w in self.saved_wifi()}
        args = ["ssid", ssid, "connection.autoconnect", "yes", "802-11-wireless.hidden", "yes" if hidden else "no"]
        if password:
            mgmt = "sae" if "WPA3" in security and not any(s in security for s in ("WPA1", "WPA2")) else "wpa-psk"
            args += ["wifi-sec.key-mgmt", mgmt, "wifi-sec.psk", password]
        if ssid in existing:
            uuid = existing[ssid]["uuid"]
            self.run("connection", "modify", "uuid", uuid, *args)
            return uuid
        out = self.run("connection", "add", "type", "wifi", "con-name", ssid,
                       *(["ifname", device] if device else []), *args)
        for w in self.saved_wifi():
            if w["ssid"] == ssid:
                return w["uuid"]
        return out.strip()

    def forget(self, uuid):
        self.run("connection", "delete", "uuid", uuid)


def count_clients(device):
    """Phones and computers connected to the hotspot right now."""
    try:
        res = subprocess.run(["iw", "dev", device, "station", "dump"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            return sum(1 for line in res.stdout.splitlines() if line.startswith("Station "))
    except (OSError, subprocess.TimeoutExpired):
        pass
    leases = f"{ROOT}/var/lib/NetworkManager/dnsmasq-{device}.leases"  # no iw: recent DHCP leases
    try:
        now = time.time()
        with open(leases, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.split()[:1] and int(line.split()[0]) > now)
    except (OSError, ValueError):
        return 0


# == the service ==================================================================================
class Watcher:
    def __init__(self, cfg, nm=None, clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.nm = nm or Nm()
        self.clock = clock
        self.sleep = sleep
        self.started = clock()
        self.after = max(0.0, cfg.getfloat("wifi", "hotspot_after_minutes", fallback=3) * 60)
        self.retry_every = max(60.0, cfg.getfloat("wifi", "retry_wifi_minutes", fallback=5) * 60)
        self.no_wifi_since = None
        self.hs_reason = ""  # "manual" | "auto": why our hotspot is on
        self.hs_since = 0.0
        self.last_client = 0.0
        self.trying_until = 0.0  # an automatic hotspot stepped aside to look for Wi-Fi until then
        self.failed_until = 0.0  # switching the hotspot on failed: wait before trying again
        self.error = ""
        self.join = None  # the last "join this Wi-Fi" and how it went
        self.scan = []
        self.scan_at = 0.0
        req = read_json(REQUEST_FILE) or {}
        # Requests from before this start are done with (except a hotspot switched on by hand this boot)
        self.seen = {k: req.get(k, 0) for k in ("hotspot_at", "join_at", "scan_at")}
        self._last_written = None
        self._written_at = 0.0
        self.boot = boot_id()

    # -- what's wanted --
    def wants(self):
        req = read_json(REQUEST_FILE) or {}
        manual_on = req.get("hotspot") == "on" and req.get("boot") == self.boot
        auto = req.get("auto")
        if auto is None:
            auto = self.after > 0
        return req, manual_on, bool(auto)

    def after_s(self):
        return self.after if self.after > 0 else 180.0  # "on by itself" from the menu, with none set

    # -- one look at the Wi-Fi, and whatever needs doing --
    def step(self):
        now = self.clock()
        req, manual_on, auto = self.wants()
        dev = self.nm.wifi_device()
        if dev is None:
            self.write({"wifi": {"state": "missing"}, "hotspot": self._hs_info(False, auto)})
            return
        device = dev["device"]
        active = self.nm.active_on(device) if dev["state"].startswith("connected") else None
        conn = self.nm.connection(active["uuid"]) if active else None
        ours = bool(active and active["name"] == HOTSPOT_CON)
        other_ap = bool(conn and conn["mode"] == "ap" and not ours)
        on_wifi = bool(active and not ours and not other_ap)

        # Requests from the menu, the music page or the command line
        if req.get("scan_at", 0) > self.seen["scan_at"]:
            self.seen["scan_at"] = req["scan_at"]
            if not ours:
                self._scan(rescan=True)
        if req.get("join_at", 0) > self.seen["join_at"]:
            self.seen["join_at"] = req["join_at"]
            self.seen["hotspot_at"] = max(self.seen["hotspot_at"], req.get("hotspot_at", 0))  # (part of the join)
            self._join(device, req.get("join", ""), ours)
            return  # look again next time: everything may have changed
        if req.get("hotspot_at", 0) > self.seen["hotspot_at"]:
            self.seen["hotspot_at"] = req["hotspot_at"]
            self.failed_until = 0.0
            if req.get("hotspot") == "off" and ours:
                log.info("Hotspot off (asked for): looking for your Wi-Fi")
                self._down(device)
                self.no_wifi_since, self.trying_until = now, 0.0
                return  # the next look says where the Wi-Fi went
            elif req.get("hotspot") == "on" and ours:
                self.hs_reason = "manual"

        if ours:
            if not self.hs_reason:
                self.hs_reason, self.hs_since = "auto", now  # it was on already when this service started
            applied = (read_json(APPLIED_FILE) or {}).get("key")
            if applied and applied != _settings_key(*hotspot_settings(self.cfg)):
                log.info("The hotspot's name or password changed: starting it again with the new one")
                self._up(dev, "manual" if manual_on else self.hs_reason)
                return
            clients = count_clients(device)
            if clients:
                self.last_client = now
            if manual_on:
                self.hs_reason = "manual"
            elif self.hs_reason == "manual":
                self.hs_reason, self.hs_since = "auto", now  # switched on by hand before a reboot? now automatic
            if self.hs_reason == "auto":
                quiet_for = now - max(self.hs_since, self.last_client)
                if not auto:
                    log.info("Hotspot off: it's no longer allowed to come on by itself")
                    self._down(device)
                    self.no_wifi_since = now
                    return  # the next look says where the Wi-Fi went
                elif quiet_for >= self.retry_every:
                    log.info("Nobody on the hotspot for %d min: looking for your Wi-Fi", quiet_for // 60)
                    self._down(device)
                    self.trying_until = now + TRY_WIFI_S
                    self.nm.rescan()
                    return
            self.write(self._status(dev, conn, ours, clients, auto))
            return

        if on_wifi or other_ap:
            if self.no_wifi_since is not None:
                log.info("On Wi-Fi: %s", conn["ssid"] if conn else dev["connection"])
            self.no_wifi_since = None
            self.trying_until = 0.0
        elif self.no_wifi_since is None:
            self.no_wifi_since = now if now - self.started > 5 else self.started

        busy = dev["state"].startswith("connecting") or dev["state"] == "deactivating"
        want_on = manual_on or (auto and not on_wifi and not other_ap and self.no_wifi_since is not None
                                and now - self.no_wifi_since >= self.after_s() and now >= self.trying_until)
        if want_on and not other_ap and not (busy and not manual_on) and now >= self.failed_until:
            self._up(dev, "manual" if manual_on else "auto")
            return
        self.write(self._status(dev, conn, False, 0, auto))

    def _scan(self, rescan=False):
        try:
            if rescan:
                self.nm.rescan()
                self.sleep(4)  # the results take a moment
            self.scan = [{k: w[k] for k in ("ssid", "signal", "security")} for w in self.nm.wifi_list()]
            self.scan_at = self.clock()
        except NmError as err:
            log.info("Wi-Fi scan didn't work: %s", err)

    def _up(self, dev, reason):
        device = dev["device"]
        name, password = hotspot_settings(self.cfg)
        self._scan(rescan=True)  # what's around, for the music page's "add a Wi-Fi" (it can't scan from the hotspot)
        log.info("Hotspot on (%s): \"%s\"", "asked for" if reason == "manual" else
                 f"no Wi-Fi for {int((self.clock() - (self.no_wifi_since or self.clock())) // 60)} min", name)
        try:
            if not self.nm.radio_on():
                self.nm.run("radio", "wifi", "on")
            self.nm.ensure_hotspot(device, name, password)
            self.nm.up(HOTSPOT_CON)
        except NmError as err:
            error = f"The hotspot couldn't start: {err}"
            if error != self.error:
                log.warning(error)
            self.error = error
            self.failed_until = self.clock() + FAILED_RETRY_S
            self.write(self._status(dev, None, False, 0, self.wants()[2]))
            return
        self.error = ""
        write_json(APPLIED_FILE, {"key": _settings_key(name, password)})
        self.hs_reason, self.hs_since, self.last_client = reason, self.clock(), 0.0
        self.write(self._status(dev, None, True, 0, self.wants()[2]))

    def _down(self, device):
        self.nm.down(HOTSPOT_CON)
        self.hs_reason = ""

    def _join(self, device, uuid, ours):
        name = next((w["name"] for w in self.nm.saved_wifi() if w["uuid"] == uuid), uuid)
        log.info("Joining the Wi-Fi %s (asked for)", name)
        if ours:
            self._down(device)
        self.join = {"name": name, "state": "joining", "at": self.clock()}
        self.write(self._status({"device": device, "state": "connecting"}, None, False, 0, self.wants()[2]))
        try:
            self.nm.up(uuid, wait=45)
            self.join.update(state="ok")
            self.no_wifi_since = None
            log.info("On Wi-Fi: %s", name)
        except NmError as err:
            self.join.update(state="failed", error=str(err))
            log.warning("Couldn't join %s: %s", name, err)
            # Without Wi-Fi, an automatic hotspot comes straight back (so you can fix the password)
            self.trying_until = 0.0
            self.no_wifi_since = min(self.no_wifi_since or self.clock(), self.clock() - self.after_s())

    # -- telling the others --
    def _hs_info(self, active, auto, clients=0):
        name, password = hotspot_settings(self.cfg)
        return {"active": active, "reason": self.hs_reason if active else "", "name": name, "password": password,
                "ip": HOTSPOT_IP, "clients": clients, "auto": auto, "after": int(self.after_s()),
                "since": self.hs_since if active else 0, "error": self.error}

    def _status(self, dev, conn, ours, clients, auto):
        wifi = {"device": dev["device"], "state": "hotspot" if ours else dev["state"].split(" ")[0]}
        if conn and not ours:
            wifi.update(ssid=conn["ssid"], connection=conn["name"], mode=conn["mode"])
        if dev["state"].startswith("connected") and not ours:
            wifi["ip"] = self.nm.device_ip(dev["device"])[0]
            if conn:
                signal = next((w["signal"] for w in self._cached_list() if w["in_use"]), 0)
                wifi["signal"] = int(round(signal, -1))  # steps of 10, so the file doesn't change all the time
        usb = {"enabled": usb_enabled(), "port": usb_port_name(), "connected": False, "ip": "", "prefix": 0}
        try:
            with open(f"{ROOT}/sys/class/net/{USB_IFACE}/carrier", encoding="utf-8") as fh:
                usb["connected"] = fh.read().strip() == "1"
        except OSError:
            pass
        if usb["connected"]:
            usb["ip"], usb["prefix"] = self.nm.device_ip(USB_IFACE)
        return {"wifi": wifi, "hotspot": self._hs_info(ours, auto, clients), "usb": usb,
                "no_wifi_since": self.no_wifi_since or 0, "trying_until": self.trying_until,
                "scan": self.scan, "scan_at": self.scan_at, "join": self.join}

    def _cached_list(self):
        try:
            return self.nm.wifi_list()
        except NmError:
            return []

    def write(self, status):
        status.setdefault("wifi", {})
        status.setdefault("hotspot", {})
        status.setdefault("usb", {"enabled": usb_enabled(), "port": usb_port_name(), "connected": False})
        status["hostname"] = os.uname().nodename
        same = status == self._last_written
        if same and self.clock() - self._written_at < 120:
            return
        self._last_written = dict(status)
        self._written_at = self.clock()
        write_json(STATUS_FILE, {**status, "at": int(self.clock()), "pid": os.getpid()})

    def run(self):
        log.info("Watching Wi-Fi: the hotspot comes on by itself after %s without Wi-Fi",
                 f"{self.after / 60:g} min" if self.after else "(never; only from the menu)")
        last_req = None
        next_poll = 0.0
        while True:
            try:
                req_m = os.stat(REQUEST_FILE).st_mtime
            except OSError:
                req_m = None
            now = self.clock()
            if now >= next_poll or req_m != last_req:
                last_req = req_m
                try:
                    self.step()
                except NmError as err:
                    if err.args and err.args[0] != self.error:
                        log.warning("NetworkManager: %s", err)
                    self.error = str(err)
                    self.write({"wifi": {"state": "unknown"}, "hotspot": self._hs_info(False, self.wants()[2]),
                                "problem": str(err)})
                except Exception:
                    log.exception("Watching Wi-Fi failed")
                next_poll = self.clock() + POLL_S
            time.sleep(1)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pirate_bluetooth.network", description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=SETTINGS)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    Watcher(load_config(args.config)).run()


if __name__ == "__main__":
    main()
