#!/bin/bash
# Pirate Audio Bluetooth add-on: pair your phone with the Pi like a pair of headphones and listen
# through the Pirate Audio's headphone jack, with now-playing info on its screen and the four buttons
# as controls. Made to sit next to Pimoroni's own Pirate Audio software (Mopidy) without changing it.
#
#   bash install.sh --check     look at the system and say what would change, without changing anything
#   bash install.sh             install (asks before changing anything)
#   bash install.sh --name "Kite's Pirate" --y-pin 20 -y
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

NAME="Pirate Audio"
Y_PIN=24
UPLOAD=true
UPLOAD_PORT=6681
LATENCY=""  # empty: keep the current setting (or "normal" on a first install)
KEEP_PIMORONI_SCREEN=false
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
  --upload-port N  Port for the music upload page (default 6681).
  --no-upload      Don't install the music upload page.
  --latency LEVEL  How far the sound lags behind the phone: low (~0.1 s buffered on the Pi),
                   normal (~0.2 s, default) or safe (~0.5 s, BlueALSA's own default).
  --keep-pimoroni-screen
                   Leave Pimoroni's now-playing screen and buttons to Mopidy, and only borrow them while
                   the phone plays (the add-on's older way; no music menu on the device).
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
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
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
$SPI_OFF && CHANGES+=("enable SPI for the Pirate Audio screen")

CARD="$(awk -F'[][]' 'tolower($0) ~ /hifiberry/ {gsub(/ /, "", $2); print $2; exit}' "$ROOT/proc/asound/cards" 2>/dev/null || true)"
if [ -z "$CARD" ]; then
    CARD="sndrpihifiberry"
    NOTES+=("The Pirate Audio DAC isn't active right now; assuming its usual name ($CARD). Check it plays after a reboot.")
fi

# Packages: only install what's missing, never upgrade what's there
WANTED=(bluez bluez-alsa-utils alsa-utils curl python3-venv python3-pip python3-dev python3-pil python3-numpy
        python3-spidev python3-gi python3-gpiozero python3-lgpio fonts-dejavu-core)
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

# Pimoroni's Mopidy setup
MOPIDY=false
if systemctl --user cat mopidy >/dev/null 2>&1; then
    MOPIDY=true
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
        for ext in pidi raspberry-gpio; do
            [ "$(mopidy_conf get "$ext" enabled 2>/dev/null | tr 'A-Z' 'a-z')" = "false" ] || SCREEN_EXTS_ON+=("$ext")
        done
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
        PROBLEMS+=("Port $UPLOAD_PORT is already used by another program. Pick another one for the upload page,
      e.g. bash install.sh --upload-port 8090 (or skip it with --no-upload).")
    fi
    CHANGES+=("add a drag-and-drop music upload page on port $UPLOAD_PORT (user service 'pirate-upload';
      saves into ~/Music, where Mopidy already looks, and refreshes Mopidy's library)")
fi

# ---- report ------------------------------------------------------------------------------------------
echo
success "Pirate Audio Bluetooth add-on"
echo "  Pi:            $MODEL ($CODENAME)"
echo "  Name:          $NAME"
echo "  Sound card:    $CARD"
if $MOPIDY && ! $KEEP_PIMORONI_SCREEN && [ -n "$MOPIDY_CONF_FILE" ] && [ -f "$MOPIDY_CONF_FILE" ]; then
    echo "  Mopidy:        found - keeps running. The add-on runs the screen and buttons for both your phone"
    echo "                 and the Pi's music, with a music menu (hold A)."
elif $MOPIDY; then
    echo "  Mopidy:        found - keeps its own screen. While your phone plays, the add-on borrows the"
    echo "                 screen and buttons, then gives them back with Mopidy's queue restored."
else
    echo "  Mopidy:        not installed - the add-on keeps the screen and buttons."
fi
echo
echo "Not changed: Mopidy's music, queue and other settings, the default sound output, other sound cards,"
echo "             your Wi-Fi, and anything already installed (no upgrades)."
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
    fi
else
    inform "Everything needed is already installed"
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
if [ -f "$CONF_DIR/settings.ini" ]; then
    inform "Keeping your existing $CONF_DIR/settings.ini"
else
    sed -e "s/@NAME@/$NAME/" -e "s/@Y_PIN@/$Y_PIN/" \
        -e "s/@UPLOAD_PORT@/$UPLOAD_PORT/" -e "s/@UPLOAD_ENABLED@/$($UPLOAD && echo yes || echo no)/" \
        -e "s#http://127.0.0.1:6680/mopidy/rpc#http://127.0.0.1:$MOPIDY_PORT/mopidy/rpc#" \
        "$SCRIPT_DIR/config/settings.ini" > "$CONF_DIR/settings.ini"
fi
if ! grep -q '^\[upload\]' "$CONF_DIR/settings.ini"; then
    # settings.ini from an earlier version: add the new [upload] section, keep everything else
    sed -n '/^\[upload\]/,$p' "$SCRIPT_DIR/config/settings.ini" \
        | sed -e "s/@UPLOAD_PORT@/$UPLOAD_PORT/" -e "s/@UPLOAD_ENABLED@/$($UPLOAD && echo yes || echo no)/" \
        | { echo; cat; } >> "$CONF_DIR/settings.ini"
fi
cp "$SCRIPT_DIR/systemd/pirate-bluetooth.service" "$SCRIPT_DIR/systemd/pirate-upload.service" "$USER_UNIT_DIR/"
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
if ! $REBOOT_NEEDED; then
    systemctl --user restart pirate-bluetooth
fi
if $UPLOAD; then
    systemctl --user enable pirate-upload
    systemctl --user restart pirate-upload  # needs no reboot
else
    systemctl --user disable --now pirate-upload 2>/dev/null || true
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
if $MOPIDY && [ "$(mopidy_conf get pidi enabled 2>/dev/null | tr 'A-Z' 'a-z')" = "false" ]; then
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
fi
echo "Useful:  pirate-bluetooth (status)   pirate-bluetooth logs -f   bash uninstall.sh"
if $REBOOT_NEEDED; then
    echo
    warning "A reboot is needed to finish (hardware settings or group changes)."
    if ! $ASSUME_YES; then
        read -r -p "Reboot now? [y/N] " answer
        [[ "$answer" =~ ^[Yy] ]] && sudo reboot
    fi
    echo "Run:  sudo reboot"
fi
