"""The music page's Settings card: reads and changes settings.ini, keeping its comments and layout.

Each setting the page offers is listed in FIELDS with what it accepts. Changing one rewrites just its line
(or adds it to its section), saves the file in one go (a power cut never leaves half a file), and says which
part of the add-on has to pick it up: the screen program and the hotspot service are restarted, the music
page applies its own settings straight away.
"""

import configparser
import os
import re

SETTINGS = os.path.expanduser("~/.config/pirate-bluetooth/settings.ini")

# group, section, key, kind, label, help, extra
FIELDS = [
    ("Bluetooth", "bluetooth", "name", "text", "Name phones see", "", {"pattern": r"[A-Za-z0-9 ._()'-]{1,40}",
                                                                         "default": "Pirate Audio"}),
    ("Bluetooth", "bluetooth", "reconnect_on_start", "bool", "Connect to my phone when the Pi switches on", "",
     {"default": "yes"}),
    ("Bluetooth", "bluetooth", "reconnect_seconds", "int", "Keep looking for the phone after power-on (seconds)", "",
     {"min": 30, "max": 1800, "default": "180"}),
    ("Bluetooth", "bluetooth", "auto_pair_if_none", "bool", "Pairing mode by itself when no phone is paired", "",
     {"default": "yes"}),
    ("Bluetooth", "bluetooth", "phone_art", "bool", "Album art from the phone over Bluetooth (Trixie)", "",
     {"default": "yes"}),
    ("Bluetooth", "bluetooth", "art_lookup", "bool", "Look album art up online when the phone sends none",
     "Only the artist and album names are sent, to Apple's iTunes catalogue.", {"default": "yes"}),
    ("Screen and buttons", "display", "sleep_after", "choice", "Screen turns off when nothing plays", "",
     {"choices": [("30", "after 30 s"), ("60", "after 1 min"), ("120", "after 2 min"), ("300", "after 5 min"),
                  ("600", "after 10 min"), ("0", "never")], "default": "120"}),
    ("Screen and buttons", "display", "rotation", "choice", "Picture", "",
     {"choices": [("90", "normal"), ("270", "upside down")], "default": "90"}),
    ("Screen and buttons", "buttons", "y_pin", "choice", "Y button", "",
     {"choices": [("24", "BCM 24 (2020 on)"), ("20", "BCM 20 (older boards)")], "default": "24"}),
    ("Screen and buttons", "volume", "step", "int", "Volume step per press (%)", "", {"min": 1, "max": 20, "default": "5"}),
    ("Music page", "upload", "sync_hours", "float", "Check synced playlists against Apple Music every (hours)", "",
     {"min": 0.5, "max": 168, "default": "6"}),
    ("Music page", "upload", "password", "password", "Password for this page",
     "For shared Wi-Fi (campus, café…). Your browser asks for it once (any user name). Empty = no password.",
     {"default": ""}),
    ("Wi-Fi hotspot", "wifi", "hotspot_name", "text", "Hotspot name", "Empty = the Bluetooth name.",
     {"pattern": r"[^\x00-\x1f]{0,32}", "bytes": 32, "default": ""}),
    ("Wi-Fi hotspot", "wifi", "hotspot_password", "secret", "Hotspot password", "8 to 63 characters.",
     {"min_len": 8, "max_len": 63, "default": ""}),
    ("Wi-Fi hotspot", "wifi", "hotspot_after_minutes", "int", "Comes on by itself after this long without Wi-Fi (minutes)",
     "0 = only from the menu.", {"min": 0, "max": 120, "default": "3"}),
    ("Wi-Fi hotspot", "wifi", "retry_wifi_minutes", "int", "With nobody on it, look for Wi-Fi again every (minutes)", "",
     {"min": 1, "max": 120, "default": "5"}),
]
GROUP_NOTES = {
    "Bluetooth": "The screen program restarts to use these (a few seconds; the music doesn't stop).",
    "Screen and buttons": "The screen program restarts to use these (a few seconds; the music doesn't stop).",
    "Wi-Fi hotspot": "",
}
SERVICE_FOR = {"bluetooth": "pirate-bluetooth", "display": "pirate-bluetooth", "buttons": "pirate-bluetooth",
               "volume": "pirate-bluetooth", "wifi": "pirate-network"}
TRUE = ("yes", "true", "on", "1")


class SettingsError(Exception):
    pass


def _key(section, key):
    return f"{section}.{key}"


def read(path=SETTINGS):
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cfg.read(path, encoding="utf-8")
    except configparser.Error:
        pass
    return {_key(s, k): cfg.get(s, k, fallback=extra.get("default", "")).strip()
            for _g, s, k, _kind, _l, _h, extra in FIELDS}


def describe(path=SETTINGS, may_change_wifi=True):
    """What the page shows: every field with its current value (passwords: only whether one is set)."""
    values = read(path)
    out = []
    for group, section, key, kind, label, help_text, extra in FIELDS:
        value = values[_key(section, key)]
        field = {"id": _key(section, key), "group": group, "kind": kind, "label": label, "help": help_text,
                 "locked": section == "wifi" and not may_change_wifi}
        if kind == "password":
            field["is_set"] = bool(value)
        elif kind == "secret" and field["locked"]:
            field["value"] = ""
        elif kind == "bool":
            field["value"] = value.lower() in TRUE
        else:
            field["value"] = value
        for k in ("choices", "min", "max", "min_len", "max_len"):
            if k in extra:
                field[k] = extra[k]
        out.append(field)
    return {"fields": out, "groups": list(dict.fromkeys(f["group"] for f in out)), "notes": GROUP_NOTES}


def validate(changes, may_change_wifi=True):
    """{"section.key": value} from the page -> {(section, key): text for the file}. Raises SettingsError."""
    spec = {_key(s, k): (s, k, kind, label, extra) for _g, s, k, kind, label, _h, extra in FIELDS}
    out = {}
    for fid, value in (changes or {}).items():
        if fid not in spec:
            raise SettingsError(f"Unknown setting: {fid}")
        section, key, kind, label, extra = spec[fid]
        if section == "wifi" and not may_change_wifi:
            raise SettingsError("To change the hotspot from here, connect through its Wi-Fi or the USB cable, or "
                                "give this page a password first.")
        text = "" if value is None else str(value)
        if "\n" in text or "\r" in text:
            raise SettingsError(f"{label}: one line only.")
        if kind == "bool":
            text = "yes" if value in (True, "yes", "true", "on", 1, "1") else "no"
        elif kind in ("int", "float"):
            try:
                num = int(text) if kind == "int" else float(text)
            except ValueError:
                raise SettingsError(f"{label}: a number, please.")
            if not extra["min"] <= num <= extra["max"]:
                raise SettingsError(f"{label}: from {extra['min']:g} to {extra['max']:g}.")
            text = str(num) if kind == "int" else f"{num:g}"
        elif kind == "choice":
            if text not in [c for c, _l in extra["choices"]]:
                raise SettingsError(f"{label}: not one of the choices.")
        elif kind == "text":
            text = text.strip()
            if not re.fullmatch(extra["pattern"], text):
                raise SettingsError(f"{label}: 1-40 letters, numbers, spaces or . _ ( ) ' -" if key == "name"
                                    else f"{label}: that isn't allowed here.")
            if extra.get("bytes") and len(text.encode("utf-8")) > extra["bytes"]:
                raise SettingsError(f"{label}: {extra['bytes']} bytes at most.")
        elif kind == "secret":
            if not extra["min_len"] <= len(text) <= extra["max_len"]:
                raise SettingsError(f"{label}: {extra['min_len']} to {extra['max_len']} characters.")
        elif kind == "password":
            if text != text.strip() or len(text) > 200:
                raise SettingsError(f"{label}: no spaces at the start or end, 200 characters at most.")
        if text.startswith(("#", ";")):
            raise SettingsError(f"{label}: can't start with # or ;.")
        out[(section, key)] = text
    return out


def write(changes, path=SETTINGS):
    """Put the new values into settings.ini, line by line, keeping everything else as it was."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []
    pending = dict(changes)
    section = None
    ends = {}  # section -> index after its last non-empty line
    for i, line in enumerate(lines):
        m = re.match(r"\s*\[([^\]]+)\]\s*$", line)
        if m:
            section = m.group(1).strip()
            ends[section] = i + 1
            continue
        if section is None:
            continue
        if line.strip():
            ends[section] = i + 1
        m = re.match(r"(\s*)([A-Za-z0-9_]+)(\s*[=:]\s*)(.*)$", line)
        if m and not line.lstrip().startswith(("#", ";")) and (section, m.group(2)) in pending:
            lines[i] = f"{m.group(1)}{m.group(2)} = {pending.pop((section, m.group(2)))}"
    for (sec, key), value in list(pending.items()):  # not in the file yet: add to its section (or a new one)
        if sec in ends:
            lines.insert(ends[sec], f"{key} = {value}")
            for s in ends:
                if ends[s] >= ends[sec] and s != sec:
                    ends[s] += 1
            ends[sec] += 1
        else:
            lines += ["", f"[{sec}]", f"{key} = {value}"]
            ends[sec] = len(lines)
        pending.pop((sec, key))
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return {SERVICE_FOR[sec] for sec, _key2 in changes if sec in SERVICE_FOR} | (
        {"pirate-network"} if ("bluetooth", "name") in changes else set())
