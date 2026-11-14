#!/bin/bash
# Removes the Pirate Audio Bluetooth add-on and undoes exactly what install.sh changed
# (it reads the install record that install.sh kept).
#
#   bash uninstall.sh           remove the add-on; keep your settings.ini
#   bash uninstall.sh --purge   also remove settings.ini and the extra packages the installer added
#
# Pimoroni's software (Mopidy etc.) is left exactly as it is. Phones you paired stay paired in the
# Pi's Bluetooth settings; run "pirate-bluetooth forget all" before uninstalling if you want them gone.
set -euo pipefail

PURGE=false
[ "${1:-}" = "--purge" ] && PURGE=true

ROOT="${PIRATE_TEST_ROOT:-}"  # only set by the test suite
APP_DIR="$HOME/.local/share/pirate-bluetooth"
CONF_DIR="$HOME/.config/pirate-bluetooth"
STATE_DIR="$HOME/.local/state/pirate-bluetooth"
RECORD="$CONF_DIR/.install-record"
USER_UNIT="$HOME/.config/systemd/user/pirate-bluetooth.service"
MAIN_CONF="$ROOT/etc/bluetooth/main.conf"
ASOUND_CONF="$ROOT/etc/asound.conf"
UNIT_DIR="$ROOT/etc/systemd/system"
BIN_DIR="$ROOT/usr/local/bin"
CLASS_MARK="# pirate-bluetooth: show up as headphones in phones' Bluetooth lists (uninstall.sh restores the line below)"

c() { tput "$@" 2>/dev/null || true; }
inform()  { echo -e "$(c setaf 6)$1$(c sgr0)"; }
success() { echo -e "$(c setaf 2)$1$(c sgr0)"; }

[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user: bash uninstall.sh"; exit 1; }

NEW_PACKAGES="" DISABLED_UNITS="" GROUPS_ADDED="" CONFIG_TXT_ADDED=""
CLASS_CHANGED="" CLASS_ORIG_LINE="" BT_ENABLED_BY_US="" RFKILL_UNBLOCKED="" ASOUND_CREATED=""
LINGER_ENABLED_BY_US="" MOPIDY_EXTS_DISABLED="" MOPIDY_CONF_FILE=""
MOPIDY_INSTALLED_BY_US="" MOPIDY_CONF_CREATED="" EXP_CHANGED=""
EXP_MARK="# pirate-bluetooth: lets phones send album art over Bluetooth (uninstall.sh removes the line below)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$RECORD" ]; then
    # shellcheck disable=SC1090
    source "$RECORD"
else
    inform "No install record found; removing the add-on's own files only."
fi
sudo -v

inform "Stopping the screen/buttons program (it gives the screen back to Mopidy) and the upload page"
systemctl --user disable --now pirate-bluetooth 2>/dev/null || true
systemctl --user disable --now pirate-upload 2>/dev/null || true
rm -f "$USER_UNIT" "$HOME/.config/systemd/user/pirate-upload.service"
systemctl --user daemon-reload || true
# Give Pimoroni's screen and buttons back to Mopidy, if the installer took them.
if [ -n "$MOPIDY_EXTS_DISABLED" ] && [ -n "$MOPIDY_CONF_FILE" ] && [ -f "$MOPIDY_CONF_FILE" ]; then
    inform "Switching Mopidy's own screen and buttons back on ($MOPIDY_EXTS_DISABLED )"
    for ext in $MOPIDY_EXTS_DISABLED; do
        PYTHONPATH="$SCRIPT_DIR" python3 -c 'import sys; from pirate_bluetooth import mopidyconf; mopidyconf.set_option(sys.argv[1], sys.argv[2], "enabled", "true")' \
            "$MOPIDY_CONF_FILE" "$ext" || true
    done
    # The copy the installer kept is no longer needed if the file is back exactly as it was.
    if cmp -s "$MOPIDY_CONF_FILE" "$MOPIDY_CONF_FILE.before-pirate-bluetooth"; then
        rm -f "$MOPIDY_CONF_FILE.before-pirate-bluetooth"
    fi
    PYTHONPATH="$SCRIPT_DIR" python3 -m pirate_bluetooth.system restart-mopidy 2>/dev/null \
        || systemctl --user restart mopidy 2>/dev/null || true
fi
# The music player, if the installer set it up (your music in ~/Music stays)
if [ "$MOPIDY_INSTALLED_BY_US" = "yes" ]; then
    inform "Removing the music player the installer set up (Mopidy and Iris); your music stays in ~/Music"
    systemctl --user disable --now mopidy 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/mopidy.service"
    systemctl --user daemon-reload || true
    rm -rf "$HOME/.virtualenvs/mopidy"
    if $PURGE; then
        [ "$MOPIDY_CONF_CREATED" = "yes" ] && rm -f "$HOME/.config/mopidy/mopidy.conf"
        rm -rf "$HOME/.local/share/mopidy" "$HOME/.cache/mopidy"
    fi
    rm -f "$STATE_DIR/mopidy-handoff.json"
fi
# Belt and braces: if a Bluetooth session had Mopidy stopped, make sure it's running again.
if [ -f "$STATE_DIR/mopidy-handoff.json" ] && systemctl --user is-enabled mopidy >/dev/null 2>&1; then
    systemctl --user start mopidy || true
fi

inform "Removing the Bluetooth audio services"
sudo systemctl disable --now pirate-bluealsa-aplay pirate-bluealsa 2>/dev/null || true
sudo rm -f "$UNIT_DIR/pirate-bluealsa.service" "$UNIT_DIR/pirate-bluealsa-aplay.service" \
    "$ROOT/etc/default/pirate-bluetooth-latency"
sudo systemctl daemon-reload

REMOVE_PKGS=()
for pkg in $NEW_PACKAGES; do
    if [ "$pkg" = "bluez-alsa-utils" ] || $PURGE; then REMOVE_PKGS+=("$pkg"); fi
done
if [ ${#REMOVE_PKGS[@]} -gt 0 ]; then
    inform "Removing packages the installer added: ${REMOVE_PKGS[*]}"
    sudo apt-get remove -y "${REMOVE_PKGS[@]}" || true
fi
for unit in $DISABLED_UNITS; do  # units from a package we didn't remove go back to how they were
    if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
        sudo systemctl enable "$unit" || true
    fi
done

if [ -f "$ASOUND_CONF" ]; then
    inform "Removing the Pirate Headphone volume control from $ASOUND_CONF"
    sudo sed -i '/^# >>> pirate-bluetooth >>>$/,/^# <<< pirate-bluetooth <<<$/d' "$ASOUND_CONF"
    if [ "$ASOUND_CREATED" = "yes" ] && ! grep -q '[^[:space:]]' "$ASOUND_CONF"; then
        sudo rm -f "$ASOUND_CONF"
    fi
fi

if [ -f "$MAIN_CONF" ] && grep -qF "$EXP_MARK" "$MAIN_CONF"; then
    inform "Switching BlueZ's experimental features back off"
    TMP="$(mktemp)"
    awk -v mark="$EXP_MARK" 'skip { skip = 0; next } $0 == mark { skip = 1; next } { print }' "$MAIN_CONF" > "$TMP"
    sudo install -m 0644 "$TMP" "$MAIN_CONF"
    rm -f "$TMP"
fi
if [ -f "$MAIN_CONF" ] && grep -qF "$CLASS_MARK" "$MAIN_CONF"; then
    inform "Putting $MAIN_CONF back"
    TMP="$(mktemp)"
    awk -v mark="$CLASS_MARK" -v orig="$CLASS_ORIG_LINE" '
        skip { skip = 0; if (orig != "") print orig; next }
        $0 == mark { skip = 1; next }
        { print }' "$MAIN_CONF" > "$TMP"
    sudo install -m 0644 "$TMP" "$MAIN_CONF"
    rm -f "$TMP"
fi

# The add-on renamed the Bluetooth adapter; an empty alias puts BlueZ's default name back.
busctl --system set-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 Alias s "" 2>/dev/null || true
if [ "$BT_ENABLED_BY_US" = "yes" ]; then
    sudo systemctl disable --now bluetooth || true
else
    sudo systemctl restart bluetooth 2>/dev/null || true
fi
if [ "$RFKILL_UNBLOCKED" = "yes" ] && command -v rfkill >/dev/null; then
    sudo rfkill block bluetooth || true
fi

if [ "$LINGER_ENABLED_BY_US" = "yes" ]; then
    inform "Your user services start at login again (as before the install)"
    sudo loginctl disable-linger "$USER" || true
fi

inform "Removing the program and the pirate-bluetooth command"
sudo rm -f "$BIN_DIR/pirate-bluetooth"
rm -rf "$APP_DIR" "$STATE_DIR"
rm -f "$RECORD"
if $PURGE; then
    rm -rf "$CONF_DIR"
fi

echo
success "Removed."
[ -n "$CONFIG_TXT_ADDED" ] && echo "Left in config.txt (the Pirate Audio and Mopidy need them):$CONFIG_TXT_ADDED"
[ -n "$GROUPS_ADDED" ] && echo "Your user stays in these groups (harmless):$GROUPS_ADDED"
$PURGE || echo "Your settings are still in $CONF_DIR (use --purge to remove them)."
echo "Music you uploaded stays in ~/Music."
exit 0
