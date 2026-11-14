#!/bin/bash
# Pirate Audio Bluetooth add-on: pair your phone with the Pi like a pair of headphones and listen
# through the Pirate Audio's headphone jack, with now-playing info on its screen and the four buttons
# as controls, plus a music menu for the Pi's own music (Mopidy) and a web page to add/download music.
#
# One script for everything: on a fresh Raspberry Pi OS Lite (Bookworm or Trixie) it also sets up the
# Pi's music player (Mopidy, its library and the Iris web page). On a Pi that already has Pimoroni's
# Pirate Audio software, it uses that.
#
#   bash install.sh --check     look at the system and say what would change, without changing anything
#   bash install.sh             install (asks before changing anything)
#   bash install.sh --name "My Pirate" --y-pin 20 -y
#   bash install.sh --latency low    (less delay; see "pirate-bluetooth latency" to change it later)
#   bash install.sh --keep-pimoroni-screen   (Mopidy keeps its own screen; no music menu on the device)
#
# Run it as your normal user (not with sudo). It asks for your password when it needs it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/pirate-bluetooth"
CONF_DIR="$HOME/.config/pirate-bluetooth"
USER_UNIT_DIR="$HOME/.config/systemd/user"
RECORD="$CONF_DIR/.install-record"
ROOT="${PIRATE_TEST_ROOT:-}"  # only set by the test suite, to run against a fake filesystem
MAIN_CONF="$ROOT/etc/bluetooth/main.conf"
ASOUND_CONF="$ROOT/etc/asound.conf"
LATENCY_FILE="$ROOT/etc/default/pirate-bluetooth-latency"
UNIT_DIR="$ROOT/etc/systemd/system"
BIN_DIR="$ROOT/usr/local/bin"
DATESTAMP="$(date "+%Y-%m-%d-%H-%M-%S")"
CLASS_MARK="# pirate-bluetooth: show up as headphones in phones' Bluetooth lists (uninstall.sh restores the line below)"
CLASS_LINE="Class = 0x000418"
EXP_MARK="# pirate-bluetooth: lets phones send album art over Bluetooth (uninstall.sh removes the line below)"
EXP_LINE="Experimental = true"
# The Pi's music player for fresh installs: the versions Pimoroni's installer used, which still work with
# the Iris web page (Iris doesn't support Mopidy 4 yet). setuptools<81 keeps pkg_resources, which they use.
MOPIDY_PIP=("Mopidy==3.4.2" "Mopidy-Local==3.3.0" "Mopidy-Iris==3.70.0" "setuptools<81")
MOPIDY_VENV="$HOME/.virtualenvs/mopidy"

NAME="Pirate Audio"
Y_PIN=24
UPLOAD=true
UPLOAD_PORT=6681
LATENCY=""  # empty: keep the current setting (or "normal" on a first install)
KEEP_PIMORONI_SCREEN=false
WANT_MOPIDY=true
PHONE_ART=true
HOTSPOT=true
USB_GADGET=true
ASSUME_YES=false
CHECK_ONLY=false
REBOOT_NEEDED=false

c() { tput "$@" 2>/dev/null || true; }
inform()  { echo -e "$(c setaf 6)$1$(c sgr0)"; }
success() { echo -e "$(c setaf 2)$1$(c sgr0)"; }
warning() { echo -e "$(c setaf 3)$1$(c sgr0)"; }
fatal()   { echo -e "$(c setaf 1)⚠ FATAL:$(c sgr0) $1"; exit 1; }
step()    { echo; echo -e "$(c bold)$(c setaf 4)==> $1$(c sgr0)"; }

usage() {
    cat <<EOF
Usage: bash install.sh [options]

  --check          Look at this Pi and list what the install would change. Changes nothing.
  --name "NAME"    Name phones see in their Bluetooth list (default: "Pirate Audio").
  --y-pin 20       Use BCM 20 for the Y button (Pirate Audio boards made before 23 Jan 2020).
  --upload-port N  Port for the music page (upload and download; default 6681).
  --no-upload      Don't install the music page.
  --latency LEVEL  How far the sound lags behind the phone: low (~0.1 s buffered on the Pi),
                   normal (~0.2 s, default) or safe (~0.5 s, BlueALSA's own default).
  --keep-pimoroni-screen
                   Leave Pimoroni's now-playing screen and buttons to Mopidy, and only borrow them while
                   the phone plays (the add-on's older way; no music menu on the device).
  --no-mopidy      On a Pi without Mopidy: don't install it (Bluetooth only, no music stored on the Pi).
  --no-phone-art   Don't set up album art from the phone over Bluetooth (Trixie). The online lookup
                   (settings.ini, art_lookup) is separate.
  --no-hotspot     No Wi-Fi hotspot (the Pi's own Wi-Fi, for when it can't join yours: from the menu, or by
                   itself after 3 minutes without Wi-Fi).
  --no-usb         Don't set up the network over a USB cable to a computer (Trixie, on a Pi Zero, 4 or 5).
  -y, --yes        Don't ask for confirmation.
  -h, --help       Show this help.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=true ;;
        --name) shift; NAME="${1:-}" ;;
        --name=*) NAME="${1#--name=}" ;;
        --y-pin) shift; Y_PIN="${1:-}" ;;
        --upload-port) shift; UPLOAD_PORT="${1:-}" ;;
        --no-upload) UPLOAD=false ;;
        --latency) shift; LATENCY="${1:-}" ;;
        --latency=*) LATENCY="${1#--latency=}" ;;
        --keep-pimoroni-screen) KEEP_PIMORONI_SCREEN=true ;;
        --no-mopidy) WANT_MOPIDY=false ;;
        --no-phone-art) PHONE_ART=false ;;
        --no-hotspot) HOTSPOT=false ;;
        --no-usb) USB_GADGET=false ;;
        -y|--yes) ASSUME_YES=true ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fatal "Unknown option: $1" ;;
    esac
    shift
done

# A line in the install record. List keys accumulate with += so re-running the installer never
# forgets what an earlier run did; uninstall.sh reads this file to undo exactly those changes.
record() { mkdir -p "$CONF_DIR"; printf '%s=%q\n' "$1" "$2" >> "$RECORD"; }
record_add() { mkdir -p "$CONF_DIR"; printf '%s+=%q\n' "$1" " $2" >> "$RECORD"; }
# Lines at the end of config.txt belong to its last [section]: make sure that's [all] (every Pi)
ensure_all_section() {
    local last
    last="$(grep -E '^[[:space:]]*\[[^]]+\]' "$CONFIG_TXT" | tail -n 1 | tr -d '[:space:]')"
    if [ -n "$last" ] && [ "$last" != "[all]" ]; then
        printf "%s\n" "[all]" | sudo tee -a "$CONFIG_TXT" >/dev/null
        record_add CONFIG_TXT_ADDED "[all]"
    fi
}
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
version_ge() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$2" ]; }  # is $1 >= $2 ?
# The BlueZ version that is (or would be) installed, e.g. "5.82"
bluez_version() {
    local v
    v="$(dpkg-query -W -f='${Version}' bluez 2>/dev/null || true)"
    [ -n "$v" ] || v="$(apt-cache policy bluez 2>/dev/null | awk '/Candidate:/ {print $2}')"
    echo "$v" | grep -oE '^[0-9]+\.[0-9]+' || true
}
# Playback buffer for bluealsa-aplay (microseconds). It starts playing once the buffer is full, so the
# buffer size is the delay added on the Pi. Smaller = less lag, but more chance of crackles when the
# radio is busy (e.g. big Wi-Fi transfers). Keep in step with LATENCY_LEVELS in pirate_bluetooth/cli.py.
latency_opts() {
    case "$1" in
        low) echo "--pcm-buffer-time=100000 --pcm-period-time=20000" ;;
        normal) echo "--pcm-buffer-time=200000 --pcm-period-time=40000" ;;
        safe) echo "--pcm-buffer-time=500000 --pcm-period-time=100000" ;;
    esac
}

# ============================================================================================
#  1. Look before touching anything
# ============================================================================================
PROBLEMS=()
CHANGES=()
NOTES=()

[ "$(id -u)" -ne 0 ] || fatal "Run this as your normal user, not root:  bash install.sh"
command -v apt-get >/dev/null || fatal "This installer is for Raspberry Pi OS."
[[ "$NAME" =~ ^[A-Za-z0-9\ ._\(\)\'-]{1,40}$ ]] || fatal "--name must be 1-40 letters, numbers, spaces or . _ ( ) ' -"
[[ "$Y_PIN" =~ ^(20|24)$ ]] || fatal "--y-pin must be 20 or 24"
[[ -z "$LATENCY" || "$LATENCY" =~ ^(low|normal|safe)$ ]] || fatal "--latency must be low, normal or safe"
[[ "$UPLOAD_PORT" =~ ^[0-9]+$ ]] && [ "$UPLOAD_PORT" -ge 1024 ] && [ "$UPLOAD_PORT" -le 65535 ] \
    || fatal "--upload-port must be a number from 1024 to 65535"
systemctl --user show-environment >/dev/null 2>&1 \
    || fatal "Can't reach your user services. Run this from a normal SSH or console login (not via sudo/su)."

MODEL="$(tr -d '\0' < "$ROOT/proc/device-tree/model" 2>/dev/null || echo "unknown")"
CODENAME="$(. "$ROOT/etc/os-release" && echo "${VERSION_CODENAME:-unknown}")"
case "$CODENAME" in bookworm|trixie) ;; *) NOTES+=("Tested on Raspberry Pi OS Bookworm and Trixie; this is '$CODENAME'.") ;; esac
BLUEZ_VER="$(bluez_version)"
# Phones can send album art over Bluetooth (AVRCP cover art) to BlueZ 5.79 and newer - Trixie, not Bookworm.
BT_ART=false
if $PHONE_ART && [ -n "$BLUEZ_VER" ] && version_ge "$BLUEZ_VER" "5.79"; then
    BT_ART=true
fi

if [ -f "$ROOT/boot/firmware/config.txt" ]; then CONFIG_TXT="$ROOT/boot/firmware/config.txt"
elif [ -f "$ROOT/boot/config.txt" ]; then CONFIG_TXT="$ROOT/boot/config.txt"
else fatal "Can't find config.txt in /boot/firmware or /boot."; fi

ALREADY_OURS=false
[ -f "$UNIT_DIR/pirate-bluealsa.service" ] && ALREADY_OURS=true

# Desktop sound systems handle Bluetooth audio themselves; a second Bluetooth audio service would fight them.
if pgrep -x pipewire >/dev/null 2>&1 || pgrep -x pulseaudio >/dev/null 2>&1; then
    PROBLEMS+=("A desktop sound system (PipeWire/PulseAudio) is running. This add-on is for Raspberry Pi OS Lite;
      on a desktop it would clash with the desktop's own Bluetooth audio, so nothing was changed.")
fi

# Bluetooth hardware and service
if grep -qE '^[[:space:]]*dtoverlay=(pi3-)?disable-bt' "$CONFIG_TXT"; then
    PROBLEMS+=("Bluetooth is switched off in $CONFIG_TXT (a 'dtoverlay=disable-bt' line). Remove that line and reboot
      if you want Bluetooth; the installer won't override your choice.")
elif [ ! -d "$ROOT/sys/class/bluetooth/hci0" ]; then
    PROBLEMS+=("No Bluetooth adapter found (/sys/class/bluetooth/hci0). On a Pi with built-in Bluetooth, check that the
      'hciuart' service is running: systemctl status hciuart")
fi
BT_STATE="$(systemctl is-enabled bluetooth 2>/dev/null || true)"
if [ "$BT_STATE" = "masked" ]; then
    PROBLEMS+=("The Bluetooth service is masked (switched off on purpose). Unmask it first: sudo systemctl unmask bluetooth")
elif [ "$BT_STATE" != "enabled" ] && [ "$BT_STATE" != "static" ]; then
    CHANGES+=("switch on the Bluetooth service (it is '$BT_STATE')")
fi
if command -v rfkill >/dev/null && rfkill list bluetooth 2>/dev/null | grep -q "Soft blocked: yes"; then
    CHANGES+=("unblock Bluetooth (rfkill)")
fi

# Someone else's Bluetooth audio setup?
if installed bluez-alsa-utils && ! $ALREADY_OURS; then
    PROBLEMS+=("BlueALSA (bluez-alsa-utils) is already installed, so it may be part of your own setup.
      The installer won't reconfigure it.")
fi

# Pirate Audio hardware (Pimoroni's installer normally added these already)
CONFIG_ADD=()
for line in "dtoverlay=hifiberry-dac" "gpio=25=op,dh"; do
    grep -qE "^[[:space:]]*${line}[[:space:]]*$" "$CONFIG_TXT" || CONFIG_ADD+=("$line")
done
SPI_OFF=false
if [ ! -e "$ROOT/dev/spidev0.1" ] && ! grep -qE '^[[:space:]]*dtparam=spi=on' "$CONFIG_TXT"; then
    SPI_OFF=true
fi
[ ${#CONFIG_ADD[@]} -gt 0 ] && CHANGES+=("add to $CONFIG_TXT: ${CONFIG_ADD[*]} (needed for the Pirate Audio, normally already there)")
# Trixie's kernel keeps GPIO9 for SPI (it's SPI's input pin, which the screen doesn't use), but the screen
# needs GPIO9 as its data/command pin. Raspberry Pi's spi0-2cs overlay with no_miso leaves GPIO9 free.
NO_MISO="dtoverlay=spi0-2cs,no_miso"
if [ "$CODENAME" = trixie ]; then
    if grep -qE '^[[:space:]]*dtoverlay=spi0-[12]cs.*no_miso' "$CONFIG_TXT"; then
        :
    elif grep -qE '^[[:space:]]*dtoverlay=spi0-[12]cs' "$CONFIG_TXT"; then
        NOTES+=("$CONFIG_TXT already has its own spi0 overlay; if the screen stays blank, add ',no_miso' to it.")
    else
        CONFIG_ADD+=("$NO_MISO")
        CHANGES+=("add '$NO_MISO' to $CONFIG_TXT: on Trixie the SPI driver otherwise holds GPIO9, the
      screen's data/command pin, and the screen stays blank (the screen doesn't use SPI's input pin)")
    fi
fi
$SPI_OFF && CHANGES+=("enable SPI for the Pirate Audio screen")

CARD="$(awk -F'[][]' 'tolower($0) ~ /hifiberry/ {gsub(/ /, "", $2); print $2; exit}' "$ROOT/proc/asound/cards" 2>/dev/null || true)"
if [ -z "$CARD" ]; then
    CARD="sndrpihifiberry"
    NOTES+=("The Pirate Audio DAC isn't active right now; assuming its usual name ($CARD). Check it plays after a reboot.")
fi

# Packages: only install what's missing, never upgrade what's there
WANTED=(bluez bluez-alsa-utils alsa-utils curl python3-venv python3-pip python3-dev python3-pil python3-numpy
        python3-spidev python3-gi python3-gpiozero python3-lgpio fonts-dejavu-core)
# Trixie packages libgpiod's Python bindings in the version the screen driver needs (2.x), so pip doesn't
# have to build them; Bookworm's are too old (1.x) and pip brings its own into the add-on's environment.
[ "$CODENAME" = trixie ] && WANTED+=(python3-libgpiod)
# Pimoroni's Mopidy setup, or a fresh install of it?
MOPIDY=false
systemctl --user cat mopidy >/dev/null 2>&1 && MOPIDY=true
INSTALL_MOPIDY=false
if ! $MOPIDY && $WANT_MOPIDY; then
    INSTALL_MOPIDY=true
    WANTED+=(python3-gst-1.0 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good
             gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-alsa
             gstreamer1.0-tools python3-setuptools python3-requests python3-tornado)
    TRIXIE_FIX=""
    [ "$CODENAME" = trixie ] && TRIXIE_FIX=", plus the fix Mopidy 3.4 needs on Trixie"
    CHANGES+=("install the Pi's music player: Mopidy 3.4.2, its music library (Mopidy-Local 3.3.0) and the Iris
      web page (port 6680), in $MOPIDY_VENV as user service 'mopidy' - the same layout as Pimoroni's
      installer$TRIXIE_FIX. Music goes in ~/Music")
fi
if $BT_ART; then
    WANTED+=(bluez-obexd dbus-user-session)
fi
# A way in when the Pi can't join your Wi-Fi (or you're away from it): its own Wi-Fi hotspot, from the menu
# or by itself after a few minutes without Wi-Fi, and on a Pi that can do it, a network over a USB cable.
POLKIT_RULE="$ROOT/etc/polkit-1/rules.d/50-pirate-bluetooth-hotspot.rules"
HOTSPOT_DNSMASQ="$ROOT/etc/NetworkManager/dnsmasq-shared.d/50-pirate-hotspot.conf"
NETPLAN_CHECK="$ROOT/usr/local/lib/pirate-bluetooth/netplan-check"
HOTSPOT_ACTION="none"
if $HOTSPOT; then
    if ! command -v nmcli >/dev/null 2>&1 || [ ! -d "$ROOT/etc/NetworkManager" ]; then
        NOTES+=("This Pi doesn't use NetworkManager, so there's no Wi-Fi hotspot.")
    else
        # NetworkManager asks polkit who may switch connections; dnsmasq hands out addresses on the hotspot;
        # iw counts who's connected to it
        WANTED+=(polkitd dnsmasq-base iw)
        HOTSPOT_ACTION="set"
        if [ -f "$POLKIT_RULE" ] && grep -qF "subject.user == \"$USER\"" "$POLKIT_RULE"; then
            HOTSPOT_ACTION="update"
        else
            CHANGES+=("add a Wi-Fi hotspot (user service 'pirate-network'): the Pi's own Wi-Fi, so you can always reach
      it (SSH, the music page, where you can add a Wi-Fi). Switch it on in the menu (hold A > Wi-Fi hotspot);
      it also comes on by itself after 3 minutes without Wi-Fi and goes back to your Wi-Fi when it's there.
      It needs a NetworkManager permission for $USER ($POLKIT_RULE) and gives phones on it no
      internet route ($HOTSPOT_DNSMASQ). Your saved Wi-Fi networks aren't changed")
        fi
    fi
fi
USB_ACTION="none"
USB_PORT_NAME=""
USB_GADGET_CONF="$ROOT/etc/modules-load.d/usb-gadget.conf"
CMDLINE_TXT="$(dirname "$CONFIG_TXT")/cmdline.txt"
# The Pis Raspberry Pi's rpi-usb-gadget supports that take a Pirate Audio and work with a normal cable
case "$(echo "$MODEL" | sed -E 's/[[:space:]]+Rev[[:space:]].*$//')" in
    "Raspberry Pi Zero"|"Raspberry Pi Zero W"|"Raspberry Pi Zero 2 W") USB_PORT_NAME="the micro-USB port marked USB (not PWR IN)" ;;
    "Raspberry Pi 4 Model B"|"Raspberry Pi 5 Model B") USB_PORT_NAME="the USB-C power port" ;;
esac
if $USB_GADGET; then
    if [ -f "$USB_GADGET_CONF" ]; then
        USB_ACTION="already"
    elif awk '/^[[:space:]]*\[/ { sec = $0 } /^[[:space:]]*dtoverlay=dwc2/ && sec !~ /^[[:space:]]*\[cm/ { found = 1 }
              END { exit !found }' "$CONFIG_TXT" || grep -q 'g_ether' "$CMDLINE_TXT" 2>/dev/null; then
        # (Raspberry Pi OS's own config.txt has a dwc2 line for Compute Module 5 boards only: that's not it)
        NOTES+=("The Pi's USB port is already set up by hand (dwc2/g_ether in config.txt or cmdline.txt), so the
      installer leaves the USB connection as it is.")
    elif [ -z "$USB_PORT_NAME" ]; then
        NOTES+=("A network over a USB cable isn't possible on this Pi ($MODEL); the Wi-Fi hotspot is the way in.")
    elif [ "$CODENAME" != trixie ]; then
        NOTES+=("A network over a USB cable needs Raspberry Pi OS Trixie (Raspberry Pi's rpi-usb-gadget); on
      ${CODENAME^}, the Wi-Fi hotspot is the way in.")
    else
        USB_ACTION="set"
        WANTED+=(rpi-usb-gadget)
        CHANGES+=("let a computer reach the Pi over a USB cable, from the next boot (Raspberry Pi's
      'rpi-usb-gadget on'): plug the computer into $USB_PORT_NAME and SSH in. That port then
      can't take USB devices like a keyboard or memory stick (uninstall.sh switches it back; --no-usb skips it)")
    fi
fi

MISSING=()
for pkg in "${WANTED[@]}"; do installed "$pkg" || MISSING+=("$pkg"); done
[ ${#MISSING[@]} -gt 0 ] && CHANGES+=("install packages: ${MISSING[*]}")

# BlueZ device class, so iPhones list the Pi as headphones
CLASS_ACTION="none"
if [ ! -f "$MAIN_CONF" ]; then
    NOTES+=("$MAIN_CONF doesn't exist; phones may not list the Pi as an audio device.")
elif grep -qF "$CLASS_MARK" "$MAIN_CONF"; then
    CLASS_ACTION="already"
elif grep -qE '^[[:space:]]*Class[[:space:]]*=' "$MAIN_CONF"; then
    NOTES+=("$MAIN_CONF already sets a Bluetooth device class; leaving it alone.")
else
    CLASS_ACTION="set"
    CHANGES+=("set the Bluetooth device class to 'headphones' in $MAIN_CONF (backed up; restores on uninstall)")
fi

# BlueZ's experimental features (needed for album art over Bluetooth)
EXP_ACTION="none"
if $BT_ART && [ -f "$MAIN_CONF" ]; then
    if grep -qF "$EXP_MARK" "$MAIN_CONF" || grep -qiE '^[[:space:]]*Experimental[[:space:]]*=[[:space:]]*true' "$MAIN_CONF"; then
        EXP_ACTION="already"
    elif grep -qE '^[[:space:]]*Experimental[[:space:]]*=' "$MAIN_CONF"; then
        BT_ART=false
        NOTES+=("$MAIN_CONF switches BlueZ's experimental features off on purpose, so album art comes from the
      online lookup instead of over Bluetooth.")
    else
        EXP_ACTION="set"
        CHANGES+=("switch on BlueZ's experimental features in $MAIN_CONF, needed for album art from your phone over
      Bluetooth (backed up; uninstall.sh removes the line)")
    fi
fi

# Pimoroni's Mopidy setup
if $MOPIDY; then
    MOPIDY_PORT="$(awk -F'=' '/^\[/{s=($0=="[http]")} s && $1 ~ /^[[:space:]]*port[[:space:]]*$/ {gsub(/[[:space:]]/,"",$2); print $2}' \
        "$HOME/.config/mopidy/mopidy.conf" 2>/dev/null | tail -n1)"
    MOPIDY_PORT="${MOPIDY_PORT:-6680}"
    # Pimoroni's installer freezes Mopidy's settings at install time; if Mopidy-Local had a hiccup then,
    # it stays switched off and uploaded songs never reach Mopidy's library.
    MOPIDY_EXEC="$(systemctl --user cat mopidy 2>/dev/null | sed -n 's/^ExecStart=//p' | tail -n1)"
    if python3 - "$MOPIDY_EXEC" "$HOME" 2>/dev/null <<'PY'
import configparser, os, shlex, sys
argv = shlex.split(sys.argv[1]) if sys.argv[1] else []
files = []
for i, arg in enumerate(argv):
    if arg == "--config" and i + 1 < len(argv):
        files += argv[i + 1].split(":")
    elif arg.startswith("--config="):
        files += arg.split("=", 1)[1].split(":")
files = [os.path.expanduser(f) for f in files if f] or [os.path.join(sys.argv[2], ".config/mopidy/mopidy.conf")]
cfg = configparser.RawConfigParser(inline_comment_prefixes=(";",), strict=False)
cfg.read(files)
sys.exit(0 if cfg.get("local", "enabled", fallback="true").strip().lower() in ("false", "no", "off", "0") else 1)
PY
    then
        NOTES+=("Mopidy's music library (Mopidy-Local) is switched off in Mopidy's own settings, so songs you upload
      only show in Iris under Browse > Files. After installing, 'pirate-bluetooth library on' switches it back on.")
    fi
    if command -v curl >/dev/null && systemctl --user is-active mopidy >/dev/null 2>&1 && ! curl -s -m 3 -H 'Content-Type: application/json' \
            -d '{"jsonrpc":"2.0","id":1,"method":"core.get_version"}' "http://127.0.0.1:$MOPIDY_PORT/mopidy/rpc" \
            | grep -q '"result"'; then
        NOTES+=("Mopidy is running but its web API on port $MOPIDY_PORT didn't answer, so its queue may not be
      saved/restored around Bluetooth sessions (everything else still works).")
    fi
fi
MOPIDY_PORT="${MOPIDY_PORT:-6680}"

# One screen for both: Mopidy's own screen (PiDi) and button (raspberry-gpio) extensions get switched off
# in its config, and the add-on runs the screen and buttons for the phone and the Pi's music.
mopidy_conf() { PYTHONPATH="$SCRIPT_DIR" python3 -m pirate_bluetooth.mopidyconf "$@"; }
SCREEN_EXTS_ON=()
MOPIDY_CONF_FILE=""
# What an earlier install switched off (so --keep-pimoroni-screen can switch it back on)
PREV_EXTS="$( (MOPIDY_EXTS_DISABLED=""; source "$RECORD" 2>/dev/null; echo $MOPIDY_EXTS_DISABLED) )"
PREV_CONF="$( (MOPIDY_CONF_FILE=""; source "$RECORD" 2>/dev/null; echo "$MOPIDY_CONF_FILE") )"
GIVE_BACK_EXTS=false
if $KEEP_PIMORONI_SCREEN && [ -n "$PREV_EXTS" ] && [ -f "$PREV_CONF" ]; then
    GIVE_BACK_EXTS=true
    CHANGES+=("switch Mopidy's own screen and buttons ($PREV_EXTS) back on in $PREV_CONF")
fi
if $MOPIDY && ! $KEEP_PIMORONI_SCREEN; then
    MOPIDY_CONF_FILE="$(PYTHONPATH="$SCRIPT_DIR" python3 -c 'from pirate_bluetooth import mopidyconf; print(mopidyconf.config_files()[-1])' 2>/dev/null || true)"
    if [ -n "$MOPIDY_CONF_FILE" ] && [ -f "$MOPIDY_CONF_FILE" ]; then
        # PiDi / raspberry-gpio that are switched on and installed
        while IFS= read -r ext; do
            [ -n "$ext" ] && SCREEN_EXTS_ON+=("$ext")
        done < <(mopidy_conf own-screen 2>/dev/null || true)
        if [ ${#SCREEN_EXTS_ON[@]} -gt 0 ]; then
            CHANGES+=("switch off Mopidy's own screen and button extensions (${SCREEN_EXTS_ON[*]}) in $MOPIDY_CONF_FILE,
      so the add-on can run the screen and buttons for both the phone and the Pi's music (with a music menu).
      Mopidy, Iris and your music stay. uninstall.sh switches them back on (or use --keep-pimoroni-screen)")
        fi
    else
        NOTES+=("Couldn't find Mopidy's config file, so Mopidy keeps its own screen and buttons and the add-on
      borrows them while the phone plays.")
    fi
fi

# User services (Mopidy, and the screen program) normally start only when you log in. "Linger" starts
# them at power-on instead, which Pimoroni suggests for Mopidy too; without it, pressing play and the
# phone reconnecting by itself wouldn't work until someone logs in.
LINGER="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo unknown)"
LINGER_ACTION="none"
if [ "$LINGER" = "no" ]; then
    LINGER_ACTION="enable"
    CHANGES+=("start your user services (Mopidy and this add-on) at power-on instead of at login
      ('loginctl enable-linger', as Pimoroni suggests for Mopidy; uninstall.sh turns it back off)")
elif [ "$LINGER" != "yes" ]; then
    NOTES+=("Couldn't check whether your user services start at power-on (loginctl). If the screen program
      only starts after you log in, run: sudo loginctl enable-linger $USER")
fi

CHANGES+=("add BlueALSA services 'pirate-bluealsa' and 'pirate-bluealsa-aplay' (receive phone audio, play it on the Pirate Audio)")
CHANGES+=("add a 'Pirate Headphone' volume control to $ASOUND_CONF (a marked block; backed up)")
if [ -n "$LATENCY" ] || [ ! -f "$LATENCY_FILE" ]; then
    CHANGES+=("set the Bluetooth audio delay to '${LATENCY:-normal}' in $LATENCY_FILE")
fi
CHANGES+=("install the screen/buttons program to $APP_DIR (its own Python environment) as user service 'pirate-bluetooth'")
CHANGES+=("add the 'pirate-bluetooth' command to $BIN_DIR")
if $UPLOAD; then
    # Is the port free (or already ours)?
    if ! systemctl --user is-active pirate-upload >/dev/null 2>&1 && ! python3 - "$UPLOAD_PORT" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", int(sys.argv[1])))  # fails if another program already listens on the port
PY
    then
        PROBLEMS+=("Port $UPLOAD_PORT is already used by another program. Pick another one for the music page,
      e.g. bash install.sh --upload-port 8090 (or skip it with --no-upload).")
    fi
    CHANGES+=("add a music page on port $UPLOAD_PORT (user service 'pirate-upload'): drag and drop songs into
      ~/Music, where Mopidy already looks (it refreshes Mopidy's library), and download them back to a computer")
fi

# ---- report ------------------------------------------------------------------------------------------
# Wi-Fi power saving can make a Pi drop off Wi-Fi and not come back (worse on a Pi Zero 2 W, whose Wi-Fi
# and Bluetooth share one radio). NetworkManager's per-connection default can be overridden globally.
NM_POWERSAVE="$ROOT/etc/NetworkManager/conf.d/pirate-wifi-powersave.conf"
PS_ACTION="none"
if [ -d "$ROOT/etc/NetworkManager" ] && [ ! -f "$NM_POWERSAVE" ]; then
    PS_ACTION="set"
    CHANGES+=("turn off Wi-Fi power saving ($NM_POWERSAVE), which can make the Pi drop off Wi-Fi
      and not come back (from the next boot; uninstall.sh removes it)")
fi
# A power cut while the Pi saves a Wi-Fi setting can leave an empty file in /etc/netplan (Trixie), and then
# the Pi may not join any Wi-Fi after the next boot.
NETPLAN_ACTION="none"
if [ -d "$ROOT/etc/netplan" ] && [ ! -f "$UNIT_DIR/pirate-netplan-check.service" ]; then
    NETPLAN_ACTION="set"
    CHANGES+=("at every boot, move empty network settings files out of /etc/netplan before the Wi-Fi starts
      (a power cut while saving one can stop the Pi from joining any Wi-Fi): service 'pirate-netplan-check'")
fi

echo
success "Pirate Audio Bluetooth add-on"
echo "  Pi:            $MODEL"
echo "  System:        Raspberry Pi OS ${CODENAME^}, BlueZ ${BLUEZ_VER:-unknown}"
echo "  Name:          $NAME"
echo "  Sound card:    $CARD"
if $BT_ART; then
    echo "  Album art:     from your phone over Bluetooth, else looked up online"
elif ! $PHONE_ART; then
    echo "  Album art:     looked up online by artist and album (--no-phone-art)"
elif [ "$EXP_ACTION" = "none" ] && [ -n "$BLUEZ_VER" ] && version_ge "$BLUEZ_VER" "5.79"; then
    echo "  Album art:     looked up online by artist and album (see Notes)"
else
    echo "  Album art:     looked up online by artist and album (album art over Bluetooth needs BlueZ 5.79+, i.e. Trixie)"
fi
if $INSTALL_MOPIDY; then
    echo "  Mopidy:        not installed yet - the installer sets it up (with Iris), and the add-on runs the"
    echo "                 screen and buttons for both your phone and the Pi's music, with a music menu."
elif ! $MOPIDY && ! $WANT_MOPIDY; then
    echo "  Mopidy:        not installed, and --no-mopidy: Bluetooth only."
elif $MOPIDY && ! $KEEP_PIMORONI_SCREEN && [ -n "$MOPIDY_CONF_FILE" ] && [ -f "$MOPIDY_CONF_FILE" ]; then
    echo "  Mopidy:        found - keeps running. The add-on runs the screen and buttons for both your phone"
    echo "                 and the Pi's music, with a music menu (hold A)."
elif $MOPIDY; then
    echo "  Mopidy:        found - keeps its own screen. While your phone plays, the add-on borrows the"
    echo "                 screen and buttons, then gives them back with Mopidy's queue restored."
fi
echo
if $INSTALL_MOPIDY; then
    echo "Not changed: the default sound output, other sound cards, your saved Wi-Fi networks, and anything"
    echo "             already installed (no upgrades)."
else
    echo "Not changed: Mopidy's music, queue and other settings, the default sound output, other sound cards,"
    echo "             your saved Wi-Fi networks, and anything already installed (no upgrades)."
fi
echo
echo "Will change:"
for item in "${CHANGES[@]}"; do echo "  - $item"; done
if [ ${#NOTES[@]} -gt 0 ]; then
    echo
    echo "Notes:"
    for item in "${NOTES[@]}"; do echo "  - $item"; done
fi
if [ ${#PROBLEMS[@]} -gt 0 ]; then
    echo
    warning "Can't install yet:"
    for item in "${PROBLEMS[@]}"; do echo "  - $item"; done
    exit 1
fi
if $CHECK_ONLY; then
    echo
    success "Check finished - nothing was changed. Run 'bash install.sh' to install."
    exit 0
fi
echo
if ! $ASSUME_YES; then
    read -r -p "Go ahead? [y/N] " answer
    [[ "$answer" =~ ^[Yy] ]] || { echo "Nothing changed."; exit 0; }
fi
sudo -v || fatal "Need sudo rights to install."

# ============================================================================================
#  2. Install
# ============================================================================================
mkdir -p "$CONF_DIR"
[ -f "$RECORD" ] || echo "# What the Pirate Audio Bluetooth installer changed (read by uninstall.sh)" > "$RECORD"

if [ ${#CONFIG_ADD[@]} -gt 0 ] || $SPI_OFF; then
    step "Pirate Audio hardware settings"
    sudo cp "$CONFIG_TXT" "$CONFIG_TXT.backup-$DATESTAMP"
    ensure_all_section
    for line in "${CONFIG_ADD[@]}"; do
        printf "%s\n" "$line" | sudo tee -a "$CONFIG_TXT" >/dev/null
        record_add CONFIG_TXT_ADDED "$line"
    done
    if $SPI_OFF; then
        printf "%s\n" "dtparam=spi=on" | sudo tee -a "$CONFIG_TXT" >/dev/null
        record_add CONFIG_TXT_ADDED "dtparam=spi=on"
    fi
    REBOOT_NEEDED=true
fi

step "Packages"
if [ ${#MISSING[@]} -gt 0 ]; then
    sudo apt-get update
    AVAILABLE=()
    for pkg in "${MISSING[@]}"; do
        if apt-cache show "$pkg" >/dev/null 2>&1; then AVAILABLE+=("$pkg"); else warning "Package $pkg isn't available; skipping"; fi
    done
    if [ ${#AVAILABLE[@]} -gt 0 ]; then
        sudo apt-get install -y --no-install-recommends --no-upgrade "${AVAILABLE[@]}"
        for pkg in "${AVAILABLE[@]}"; do installed "$pkg" && record_add NEW_PACKAGES "$pkg"; done
        # The per-user message bus (for album art over Bluetooth) only starts with a fresh login
        for pkg in "${AVAILABLE[@]}"; do [ "$pkg" = dbus-user-session ] && REBOOT_NEEDED=true; done
    fi
else
    inform "Everything needed is already installed"
fi
if $INSTALL_MOPIDY; then
    step "Music player (Mopidy, Mopidy-Local, Iris)"
    mkdir -p "$HOME/.virtualenvs" "$HOME/Music/Playlists" "$HOME/.config/mopidy" "$USER_UNIT_DIR"
    [ -x "$MOPIDY_VENV/bin/python" ] || python3 -m venv --system-site-packages "$MOPIDY_VENV"
    "$MOPIDY_VENV/bin/pip" install --upgrade "${MOPIDY_PIP[@]}" \
        || fatal "Couldn't install Mopidy with pip (is the Pi online?). Run install.sh again to retry."
    PYTHONPATH="$SCRIPT_DIR" "$MOPIDY_VENV/bin/python" -m pirate_bluetooth.mopidyfix \
        || warning "Couldn't apply the Trixie fix to Mopidy's scanner; scanning odd files may fail"
    "$MOPIDY_VENV/bin/mopidy" --version >/dev/null || fatal "Mopidy didn't install properly."
    if [ -f "$HOME/.config/mopidy/mopidy.conf" ]; then
        inform "Keeping your existing ~/.config/mopidy/mopidy.conf"
    else
        sed -e "s#@MUSIC_DIR@#$HOME/Music#g" -e "s#@CARD@#$CARD#g" "$SCRIPT_DIR/config/mopidy.conf" \
            > "$HOME/.config/mopidy/mopidy.conf"
        record MOPIDY_CONF_CREATED yes
    fi
    sed "s#@HOME@#$HOME#g" "$SCRIPT_DIR/systemd/mopidy.service" > "$USER_UNIT_DIR/mopidy.service"
    systemctl --user daemon-reload
    systemctl --user enable mopidy
    record MOPIDY_INSTALLED_BY_US yes
    MOPIDY=true
    MOPIDY_PORT=6680
    $REBOOT_NEEDED || systemctl --user restart mopidy
fi

BLUEALSA_BIN="$(command -v bluealsa || command -v bluealsad || true)"
APLAY_BIN="$(command -v bluealsa-aplay || true)"
[ -n "$BLUEALSA_BIN" ] && [ -n "$APLAY_BIN" ] || fatal "BlueALSA didn't install properly (bluealsa / bluealsa-aplay not found)."

step "Bluetooth"
if [ "$BT_STATE" != "enabled" ] && [ "$BT_STATE" != "static" ]; then
    sudo systemctl enable bluetooth
    record BT_ENABLED_BY_US yes
fi
if command -v rfkill >/dev/null && rfkill list bluetooth 2>/dev/null | grep -q "Soft blocked: yes"; then
    sudo rfkill unblock bluetooth
    record RFKILL_UNBLOCKED yes
fi
if [ "$CLASS_ACTION" = "set" ]; then
    sudo cp "$MAIN_CONF" "$MAIN_CONF.backup-$DATESTAMP"
    ORIG_LINE="$(grep -m1 -E '^#[[:space:]]*Class[[:space:]]*=' "$MAIN_CONF" || true)"
    TMP="$(mktemp)"
    if [ -n "$ORIG_LINE" ]; then
        awk -v mark="$CLASS_MARK" -v cls="$CLASS_LINE" -v orig="$ORIG_LINE" \
            '!done && $0 == orig {print mark; print cls; done = 1; next} {print}' "$MAIN_CONF" > "$TMP"
    else
        awk -v mark="$CLASS_MARK" -v cls="$CLASS_LINE" \
            '{print} !done && $0 ~ /^\[General\]/ {print mark; print cls; done = 1}' "$MAIN_CONF" > "$TMP"
    fi
    sudo install -m 0644 "$TMP" "$MAIN_CONF"
    rm -f "$TMP"
    record CLASS_ORIG_LINE "$ORIG_LINE"
    record CLASS_CHANGED yes
fi
if [ "$EXP_ACTION" = "set" ]; then
    [ "$CLASS_ACTION" = "set" ] || sudo cp "$MAIN_CONF" "$MAIN_CONF.backup-$DATESTAMP"
    TMP="$(mktemp)"
    awk -v mark="$EXP_MARK" -v line="$EXP_LINE" \
        '{print} !done && $0 ~ /^\[General\]/ {print mark; print line; done = 1}' "$MAIN_CONF" > "$TMP"
    sudo install -m 0644 "$TMP" "$MAIN_CONF"
    rm -f "$TMP"
    record EXP_CHANGED yes
fi
if [ "$PS_ACTION" = "set" ]; then
    sudo mkdir -p "$(dirname "$NM_POWERSAVE")"
    printf '%s\n' "# Written by the Pirate Audio Bluetooth add-on (uninstall.sh removes it)." \
        "# Wi-Fi power saving off, so the Pi doesn't drop off Wi-Fi. 2 = off." "[connection]" "wifi.powersave = 2" \
        | sudo tee "$NM_POWERSAVE" >/dev/null
    record NM_POWERSAVE_ADDED yes
fi
if [ "$HOTSPOT_ACTION" != "none" ]; then
    # Lets the add-on (which runs in the background, where nobody could type a password) switch the hotspot
    # on and off and save the Wi-Fi networks you add on the music page. polkitd picks the rule up by itself.
    TMP="$(mktemp)"
    cat > "$TMP" <<EOF
// Written by the Pirate Audio Bluetooth add-on (uninstall.sh removes it).
// Lets $USER switch the Pi's own Wi-Fi hotspot on and off (Pirate Audio menu, music page) and save Wi-Fi networks.
polkit.addRule(function(action, subject) {
    if (subject.user == "$USER" && (
            action.id == "org.freedesktop.NetworkManager.network-control" ||
            action.id == "org.freedesktop.NetworkManager.settings.modify.system" ||
            action.id == "org.freedesktop.NetworkManager.wifi.share.protected" ||
            action.id == "org.freedesktop.NetworkManager.wifi.scan" ||
            action.id == "org.freedesktop.NetworkManager.enable-disable-wifi")) {
        return polkit.Result.YES;
    }
});
EOF
    [ -f "$POLKIT_RULE" ] || record POLKIT_RULE_ADDED yes
    sudo mkdir -p "$(dirname "$POLKIT_RULE")"
    sudo install -m 0644 "$TMP" "$POLKIT_RULE"
    rm -f "$TMP"
    # Phones and computers on the hotspot get an address but no route to the internet through the Pi (it has
    # none then), so they keep using their mobile data or other Wi-Fi for the internet.
    [ -f "$HOTSPOT_DNSMASQ" ] || record HOTSPOT_DNSMASQ_ADDED yes
    sudo mkdir -p "$(dirname "$HOTSPOT_DNSMASQ")"
    printf '%s\n' "# Written by the Pirate Audio Bluetooth add-on (uninstall.sh removes it)." \
        "# On the Pi's own hotspot (wlan0), don't offer a way to the internet or a name server: phones that join it" \
        "# keep their mobile data for the internet and reach the Pi at 10.42.0.1." \
        "dhcp-option=tag:wlan0,3" "dhcp-option=tag:wlan0,6" | sudo tee "$HOTSPOT_DNSMASQ" >/dev/null
fi
if [ "$NETPLAN_ACTION" = "set" ]; then
    sudo mkdir -p "$(dirname "$NETPLAN_CHECK")"
    sudo install -m 0755 "$SCRIPT_DIR/tools/netplan-check" "$NETPLAN_CHECK"
    sudo install -m 0644 "$SCRIPT_DIR/systemd/pirate-netplan-check.service" "$UNIT_DIR/pirate-netplan-check.service"
    sudo systemctl daemon-reload
    sudo systemctl enable pirate-netplan-check
    record NETPLAN_CHECK_ADDED yes
fi
if [ "$USB_ACTION" = "set" ]; then
    if command -v rpi-usb-gadget >/dev/null 2>&1; then
        inform "Switching on the USB cable connection (rpi-usb-gadget)"
        [ -f "$CONFIG_TXT.backup-$DATESTAMP" ] || sudo cp "$CONFIG_TXT" "$CONFIG_TXT.backup-$DATESTAMP"
        ensure_all_section  # it adds its line at the end of config.txt
        # --force only skips its "is this model supported?" question: it is (checked above)
        if sudo rpi-usb-gadget on --force; then
            record USB_GADGET_ON_BY_US yes
            REBOOT_NEEDED=true
        else
            USB_ACTION="none"
            warning "Couldn't switch on the USB cable connection; the Wi-Fi hotspot still works"
        fi
    else
        USB_ACTION="none"
        warning "Raspberry Pi's rpi-usb-gadget isn't available, so no USB cable connection (the Wi-Fi hotspot still works)"
    fi
fi
# Package-provided BlueALSA units (if any) would compete with ours for the same D-Bus name.
for unit in bluealsa.service bluealsa-aplay.service; do
    if systemctl list-unit-files "$unit" >/dev/null 2>&1 && [ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" = "enabled" ]; then
        sudo systemctl disable --now "$unit"
        record_add DISABLED_UNITS "$unit"
    fi
done

step "Pirate Headphone volume control"
if [ -f "$ASOUND_CONF" ]; then
    sudo cp "$ASOUND_CONF" "$ASOUND_CONF.backup-$DATESTAMP"
    sudo sed -i '/^# >>> pirate-bluetooth >>>$/,/^# <<< pirate-bluetooth <<<$/d' "$ASOUND_CONF"
else
    record ASOUND_CREATED yes
fi
sed "s/@CARD@/$CARD/g" "$SCRIPT_DIR/config/asound-pirate.conf" | sudo tee -a "$ASOUND_CONF" >/dev/null

step "Bluetooth audio services"
if [ -n "$LATENCY" ] || [ ! -f "$LATENCY_FILE" ]; then
    LEVEL="${LATENCY:-normal}"
    sudo mkdir -p "$(dirname "$LATENCY_FILE")"
    printf '%s\n' "# Written by the Pirate Audio Bluetooth add-on. Change with: pirate-bluetooth latency low|normal|safe" \
        "PIRATE_LATENCY=$LEVEL" "PIRATE_LATENCY_OPTS=\"$(latency_opts "$LEVEL")\"" \
        | sudo tee "$LATENCY_FILE" >/dev/null
fi
for unit in pirate-bluealsa pirate-bluealsa-aplay; do
    sed -e "s#@BLUEALSA@#$BLUEALSA_BIN#" -e "s#@APLAY@#$APLAY_BIN#" -e "s#@CARD@#$CARD#" \
        "$SCRIPT_DIR/systemd/$unit.service" | sudo tee "$UNIT_DIR/$unit.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable pirate-bluealsa pirate-bluealsa-aplay
if ! $REBOOT_NEEDED; then
    sudo systemctl restart bluetooth
    sudo systemctl restart pirate-bluealsa pirate-bluealsa-aplay
fi

step "Screen, buttons and pairing program"
mkdir -p "$APP_DIR" "$USER_UNIT_DIR"
rm -rf "$APP_DIR/pirate_bluetooth"
cp -r "$SCRIPT_DIR/pirate_bluetooth" "$APP_DIR/"
cp "$SCRIPT_DIR/README.md" "$APP_DIR/" 2>/dev/null || true
find "$APP_DIR/pirate_bluetooth" -name '__pycache__' -prune -exec rm -rf {} +
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    python3 -m venv --system-site-packages "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade "st7789>=1.0.1,<2" \
    || fatal "Couldn't install Pimoroni's st7789 display library with pip."
"$APP_DIR/venv/bin/python" -c "import st7789, gpiozero, PIL, gi" \
    || fatal "The screen program's Python libraries didn't import."
# The hotspot's password: made up here, shown on the Pirate Audio's screen (Info) while the hotspot is on
HOTSPOT_PW=""
$HOTSPOT && HOTSPOT_PW="$(python3 -c 'import secrets; print("".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10)))')"
fill_settings() {
    sed -e "s/@NAME@/$NAME/" -e "s/@Y_PIN@/$Y_PIN/" \
        -e "s/@UPLOAD_PORT@/$UPLOAD_PORT/" -e "s/@UPLOAD_ENABLED@/$($UPLOAD && echo yes || echo no)/" \
        -e "s/@HOTSPOT_PASSWORD@/$HOTSPOT_PW/" \
        -e "s#http://127.0.0.1:6680/mopidy/rpc#http://127.0.0.1:$MOPIDY_PORT/mopidy/rpc#"
}
if [ -f "$CONF_DIR/settings.ini" ]; then
    inform "Keeping your existing $CONF_DIR/settings.ini"
else
    fill_settings < "$SCRIPT_DIR/config/settings.ini" > "$CONF_DIR/settings.ini"
fi
# settings.ini from an earlier version: add the sections it doesn't have yet, keep everything else
for section in upload wifi; do
    if ! grep -q "^\[$section\]" "$CONF_DIR/settings.ini"; then
        awk -v want="[$section]" '/^\[/ { on = ($0 == want) } on' "$SCRIPT_DIR/config/settings.ini" \
            | fill_settings | { echo; cat; } >> "$CONF_DIR/settings.ini"
    fi
done
cp "$SCRIPT_DIR/systemd/pirate-bluetooth.service" "$SCRIPT_DIR/systemd/pirate-upload.service" \
    "$SCRIPT_DIR/systemd/pirate-network.service" "$USER_UNIT_DIR/"
sed "s#@APP_DIR@#$APP_DIR#" "$SCRIPT_DIR/tools/pirate-bluetooth" | sudo tee "$BIN_DIR/pirate-bluetooth" >/dev/null
sudo chmod 0755 "$BIN_DIR/pirate-bluetooth"

MY_GROUPS=" $(id -nG "$USER") "
for group in spi gpio audio; do
    if getent group "$group" >/dev/null && [[ "$MY_GROUPS" != *" $group "* ]]; then
        sudo usermod -aG "$group" "$USER"
        record_add GROUPS_ADDED "$group"
        REBOOT_NEEDED=true
    fi
done
if $GIVE_BACK_EXTS; then
    inform "Giving Pimoroni's screen and buttons back to Mopidy"
    for ext in $PREV_EXTS; do
        PYTHONPATH="$SCRIPT_DIR" python3 -c 'import sys; from pirate_bluetooth import mopidyconf; mopidyconf.set_option(sys.argv[1], sys.argv[2], "enabled", "true")' \
            "$PREV_CONF" "$ext"
    done
    if cmp -s "$PREV_CONF" "$PREV_CONF.before-pirate-bluetooth"; then rm -f "$PREV_CONF.before-pirate-bluetooth"; fi
    record MOPIDY_EXTS_DISABLED ""
    systemctl --user stop pirate-bluetooth 2>/dev/null || true
    PYTHONPATH="$SCRIPT_DIR" python3 -m pirate_bluetooth.system restart-mopidy \
        "http://127.0.0.1:$MOPIDY_PORT/mopidy/rpc" || systemctl --user restart mopidy || true
fi
if [ ${#SCREEN_EXTS_ON[@]} -gt 0 ]; then
    inform "Handing Pimoroni's screen and buttons to the add-on"
    for ext in "${SCREEN_EXTS_ON[@]}"; do
        mopidy_conf set "$ext" enabled false >/dev/null
        record_add MOPIDY_EXTS_DISABLED "$ext"
    done
    record MOPIDY_CONF_FILE "$MOPIDY_CONF_FILE"
    # Stop the screen program first (it may have Mopidy paused), then restart Mopidy without them,
    # keeping its queue.
    systemctl --user stop pirate-bluetooth 2>/dev/null || true
    PYTHONPATH="$SCRIPT_DIR" python3 -m pirate_bluetooth.system restart-mopidy \
        "http://127.0.0.1:$MOPIDY_PORT/mopidy/rpc" || systemctl --user restart mopidy || true
fi
systemctl --user daemon-reload
systemctl --user enable pirate-bluetooth
if [ "$LINGER_ACTION" = "enable" ]; then
    inform "Starting your user services (Mopidy and the add-on) at power-on"
    sudo loginctl enable-linger "$USER"
    record LINGER_ENABLED_BY_US yes
fi
HW_STATE="$HOME/.local/state/pirate-bluetooth/hardware.json"
SCREEN_CHECK=""
if ! $REBOOT_NEEDED; then
    rm -f "$HW_STATE"
    systemctl --user restart pirate-bluetooth
    # The screen program says whether it got the screen and buttons (it keeps trying if not)
    for _ in $(seq 1 20); do [ -s "$HW_STATE" ] && break; sleep 1; done
    if [ -s "$HW_STATE" ]; then
        SCREEN_CHECK="$(python3 -c 'import json, sys
s = json.load(open(sys.argv[1]))
bad = [f"{n}: {s[k]}" for k, n in (("display", "Screen"), ("buttons", "Buttons")) if s.get(k) not in ("ok", "not in use")]
print("\n".join(bad) if bad else "ok")' "$HW_STATE" 2>/dev/null || true)"
    else
        SCREEN_CHECK="The screen program didn't start (see: pirate-bluetooth logs screen)"
    fi
fi
if $UPLOAD; then
    systemctl --user enable pirate-upload
    systemctl --user restart pirate-upload  # needs no reboot
else
    systemctl --user disable --now pirate-upload 2>/dev/null || true
fi
if [ "$HOTSPOT_ACTION" != "none" ]; then
    systemctl --user enable pirate-network
    systemctl --user restart pirate-network  # on your Wi-Fi, it only watches
else
    systemctl --user disable --now pirate-network 2>/dev/null || true
fi

# ============================================================================================
#  3. Done
# ============================================================================================
echo
success "All done!"
echo
echo "Pair your iPhone (first time only):"
echo "  1. The Pirate Audio screen shows 'Pairing mode' (if not: hold A and pick 'Pair a new phone' - or run: pirate-bluetooth pair)."
echo "  2. On the iPhone: Settings > Bluetooth, tap \"$NAME\"."
echo "  3. When both screens show the same 6-digit code, press A on the Pirate Audio and tap Pair on the iPhone."
echo
echo "After that it works like headphones: it connects to your iPhone by itself when you switch it on"
echo "(or pick \"$NAME\" in Control Center), and plays anything, including songs downloaded for offline"
echo "listening. Right after power-on, A plays whatever you used last: your phone or the Pi's own music."
echo
if $MOPIDY && [ -z "$(mopidy_conf own-screen 2>/dev/null)" ]; then
    echo "Buttons:  A play/pause (hold: music menu)   X next (hold: previous)   B/Y volume"
    echo "In the menu:  X/Y move   A open/play (hold: add to queue)   B back"
else
    echo "Buttons while your phone plays:  A play/pause (hold: info)   X next (hold: previous)   B/Y volume"
fi
if $UPLOAD; then
    PI_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo
    echo "Add music from your PC or phone: open http://$(hostname).local:$UPLOAD_PORT"
    [ -n "$PI_IP" ] && echo "  (or http://$PI_IP:$UPLOAD_PORT) and drag songs or whole album folders onto the page."
    echo "  The same page downloads music from the Pi back to your computer, and gets songs from SoundCloud"
    echo "  links (the ones whose artist offers a download)."
fi
if $INSTALL_MOPIDY; then
    echo
    echo "The Pi's music player: Iris at http://$(hostname).local:6680/iris (add music on the music page)."
fi
if [ "$HOTSPOT_ACTION" != "none" ] || [ "$USB_ACTION" != "none" ]; then
    echo
    echo "If the Pi can't get on your Wi-Fi (or you're away from it):"
fi
if [ "$HOTSPOT_ACTION" != "none" ]; then
    # The hotspot's name and password, one per line (the same as 'pirate-bluetooth hotspot' shows)
    HS="$(python3 - "$CONF_DIR/settings.ini" 2>/dev/null <<'PY' || true
import configparser, sys
cfg = configparser.ConfigParser(interpolation=None)
cfg.read(sys.argv[1])
pw = cfg.get("wifi", "hotspot_password", fallback="").strip()
name = cfg.get("wifi", "hotspot_name", fallback="").strip() or cfg.get("bluetooth", "name", fallback="").strip()
if len(pw) >= 8:
    print(name or "Pirate Audio")
    print(pw)
PY
)"
    if [ -n "$HS" ]; then
        HS_NAME="$(sed -n 1p <<< "$HS")"
        HS_PW="$(sed -n 2p <<< "$HS")"
        echo "  The Pi makes its own Wi-Fi: hold A > Wi-Fi hotspot > Hotspot (it also comes on by itself after"
        echo "  3 minutes without Wi-Fi). Join \"$HS_NAME\" (password: $HS_PW) on your phone or computer,"
        echo "  then ssh $USER@10.42.0.1$($UPLOAD && echo " or open http://10.42.0.1:$UPLOAD_PORT, where you can add the Wi-Fi")."
        echo "  The screen shows all this too, with a QR code your phone's camera can join: Wi-Fi hotspot > How to connect."
    else
        echo "  The Wi-Fi hotspot needs a password of 8 or more characters: hotspot_password in $CONF_DIR/settings.ini"
    fi
fi
if [ "$USB_ACTION" != "none" ]; then
    [ -n "$USB_PORT_NAME" ] || USB_PORT_NAME="the Pi's USB data port"
    echo "  Or with a USB cable (after the reboot): plug your computer into $USB_PORT_NAME, wait a minute,"
    echo "  then ssh $USER@$(hostname).local (or $USER@10.12.194.1$($UPLOAD && echo ", music page http://10.12.194.1:$UPLOAD_PORT"))."
    echo "  On Windows, install Raspberry Pi's driver once first: rpi-usb-gadget-driver-setup.exe from"
    echo "  https://github.com/raspberrypi/rpi-usb-gadget/releases"
fi
echo
echo "Useful:  pirate-bluetooth (status)   pirate-bluetooth logs -f   bash uninstall.sh"
if [ -n "$SCREEN_CHECK" ] && [ "$SCREEN_CHECK" != "ok" ]; then
    echo
    warning "The screen or buttons aren't working yet:"
    echo "$SCREEN_CHECK" | sed 's/^/  /'
    echo "  Run 'pirate-bluetooth doctor' to see why and what to do (the screen program keeps trying)."
fi
if $REBOOT_NEEDED; then
    echo
    warning "A reboot is needed to finish (hardware settings or group changes)."
    echo "If the screen stays blank after the reboot, run: pirate-bluetooth doctor"
    if ! $ASSUME_YES; then
        read -r -p "Reboot now? [y/N] " answer
        [[ "$answer" =~ ^[Yy] ]] && sudo reboot
    fi
    echo "Run:  sudo reboot"
fi
