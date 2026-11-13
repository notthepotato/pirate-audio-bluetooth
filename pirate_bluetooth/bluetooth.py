"""Bluetooth receiver control through BlueZ (org.bluez) on the system D-Bus.

BlueALSA does the audio (A2DP sink -> the Pirate Audio DAC). This module does the rest:
  * tracks which phone is connected and its AVRCP media player (track, artist, status, position)
  * sends play/pause/next/previous to the phone
  * pairing: makes the Pi discoverable on request, and acts as the BlueZ pairing agent so the
    6-digit code is shown on the Pirate Audio screen and confirmed with a button
  * reconnects to the last paired phone at start-up, like a pair of headphones does
"""

import logging
import time

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
IFACE_ADAPTER = "org.bluez.Adapter1"
IFACE_DEVICE = "org.bluez.Device1"
IFACE_PLAYER = "org.bluez.MediaPlayer1"
IFACE_AGENT_MANAGER = "org.bluez.AgentManager1"
PROPS = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"  # the phone's "I send audio" profile
AGENT_PATH = "/org/pirateaudio/agent"
CONFIRM_TIMEOUT_S = 30
OWN_WRITE_GRACE_S = 2.0
PLAYING_STATES = ("playing", "forward-seek", "reverse-seek")

AGENT_XML = """
<node>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
    <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
    <method name="DisplayPasskey">
      <arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/>
    </method>
    <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
    <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
    <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    <method name="Cancel"/>
  </interface>
</node>
"""

_MISSING = object()


class BluetoothClient:
    """Calls on_update(changes) (only the keys that changed) on the GLib main loop:

      _present  bool   BlueZ + an adapter are available
      Device    str    name of the connected phone, or None
      Player    bool   the phone exposes media controls
      Status    str    "playing" | "paused" | "stopped" | ...
      Track     dict   Title, Artist, Album, Duration (ms)
      Position  int    ms into the track (sent whenever Track/Status change, too)
      Pairing   bool   pairing mode on/off
      Alias     str    the name phones see
      Paired    list   names of paired devices

    and on_agent(kind, device_name, text) for pairing:
      "confirm" (text = 6-digit code), "cancel", "paired", "display" (text = code), "failed".
    """

    def __init__(self, on_update, on_agent, bus="system", alias="Pirate Audio",
                 reconnect_on_start=True, auto_pair_if_none=True, pair_seconds=180):
        self._on_update = on_update
        self._on_agent = on_agent
        self.alias = alias
        self.reconnect_on_start = reconnect_on_start
        self.auto_pair_if_none = auto_pair_if_none
        self.pair_seconds = int(pair_seconds)

        self.objects = {}  # path -> {interface: {property: value}}
        self.present = False
        self.pairing = False
        self.pairing_until = 0.0
        self.device_path = None
        self._pending = None  # (invocation, device_path, timeout_source_id)
        self._pairing_source = 0
        self._own_writes = []  # [(value, time)] Discoverable writes not yet confirmed by BlueZ
        self._last = {}
        self._subs = []
        self._synced_once = False

        bus_type = Gio.BusType.SESSION if bus == "session" else Gio.BusType.SYSTEM
        self._conn = Gio.bus_get_sync(bus_type, None)
        agent_info = Gio.DBusNodeInfo.new_for_xml(AGENT_XML).interfaces[0]
        self._agent_reg = self._conn.register_object(AGENT_PATH, agent_info, self._agent_call, None, None)
        self._watch = Gio.bus_watch_name_on_connection(
            self._conn, BLUEZ, Gio.BusNameWatcherFlags.NONE, self._appeared, self._vanished)

    # == BlueZ appearing / disappearing ==========================================
    def _appeared(self, conn, name, owner):
        log.info("BlueZ is on the bus (%s)", owner)
        self.present = True
        if not self._subs:
            for iface, member, handler in (
                (OBJECT_MANAGER, "InterfacesAdded", self._ifaces_added),
                (OBJECT_MANAGER, "InterfacesRemoved", self._ifaces_removed),
                (PROPS, "PropertiesChanged", self._props_changed),
            ):
                self._subs.append(conn.signal_subscribe(
                    BLUEZ, iface, member, None, None, Gio.DBusSignalFlags.NONE, handler))
        conn.call(BLUEZ, "/", OBJECT_MANAGER, "GetManagedObjects", None,
                  GLib.VariantType.new("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 10000, None,
                  self._got_objects)

    def _vanished(self, conn, name):
        if self.present:
            log.warning("BlueZ left the bus")
        self.present = False
        self.objects = {}
        self.pairing = False
        self._drop_pending(reject=False)
        self._emit()

    def _got_objects(self, conn, result):
        try:
            self.objects = conn.call_finish(result).unpack()[0]
        except GLib.Error as err:
            log.error("Could not read Bluetooth state: %s", err.message)
            return
        self._register_agent()
        self._setup_adapter()
        self._emit()
        if not self._synced_once:
            self._synced_once = True
            if self.auto_pair_if_none and not self._paired_devices():
                log.info("No phones paired yet: pairing mode on")
                self.start_pairing()
            elif self.reconnect_on_start:
                self.reconnect()

    # == signals ==================================================================
    def _ifaces_added(self, conn, sender, path, iface, member, params, *_):
        obj_path, interfaces = params.unpack()
        self.objects.setdefault(obj_path, {}).update(interfaces)
        if IFACE_ADAPTER in interfaces:
            self._setup_adapter()
        self._emit()

    def _ifaces_removed(self, conn, sender, path, iface, member, params, *_):
        obj_path, interfaces = params.unpack()
        obj = self.objects.get(obj_path, {})
        for name in interfaces:
            obj.pop(name, None)
        if not obj:
            self.objects.pop(obj_path, None)
        self._emit()

    def _props_changed(self, conn, sender, path, iface, member, params, *_):
        changed_iface, changed, invalidated = params.unpack()
        obj = self.objects.get(path)
        if obj is None or changed_iface not in obj:
            return
        before_paired = obj[changed_iface].get("Paired")
        obj[changed_iface].update(changed)
        for prop in invalidated:
            obj[changed_iface].pop(prop, None)
        if changed_iface == IFACE_DEVICE and changed.get("Paired") and not before_paired and self.pairing:
            name = self._device_name(path)
            log.info("Paired with %s", name)
            self._set(path, IFACE_DEVICE, "Trusted", GLib.Variant("b", True))
            self._on_agent("paired", name, "")
            self.stop_pairing()
        if (changed_iface == IFACE_ADAPTER and "Discoverable" in changed
                and not self._is_own_echo(bool(changed["Discoverable"]))):
            if changed["Discoverable"] and not self.pairing:
                # Someone ran `pirate-bluetooth pair` (or similar) - follow along on the screen.
                log.info("Discoverable switched on from outside: pairing mode on")
                self.start_pairing()
            elif not changed["Discoverable"] and self.pairing:
                log.info("BlueZ ended discoverable mode")
                self.stop_pairing()
        self._emit()

    # == derived state ===============================================================
    def _adapter_path(self):
        adapters = sorted(p for p, ifs in self.objects.items() if IFACE_ADAPTER in ifs)
        return adapters[0] if adapters else None

    def _device_name(self, path):
        props = self.objects.get(path, {}).get(IFACE_DEVICE, {})
        return props.get("Alias") or props.get("Name") or props.get("Address") or "phone"

    def _paired_devices(self):
        return sorted(p for p, ifs in self.objects.items() if ifs.get(IFACE_DEVICE, {}).get("Paired"))

    def _current_device(self):
        candidates = []
        for path, ifs in sorted(self.objects.items()):
            dev = ifs.get(IFACE_DEVICE)
            if not dev or not dev.get("Connected"):
                continue
            uuids = [str(u).lower() for u in dev.get("UUIDs", [])]
            if A2DP_SOURCE_UUID in uuids or any(
                    IFACE_PLAYER in o and o[IFACE_PLAYER].get("Device") == path
                    for o in self.objects.values()):
                candidates.append(path)
        if self.device_path not in candidates:
            self.device_path = candidates[0] if candidates else None
        return self.device_path

    def _current_player(self, device):
        if not device:
            return None
        players = sorted(p for p, ifs in self.objects.items()
                         if IFACE_PLAYER in ifs and (ifs[IFACE_PLAYER].get("Device") == device
                                                     or p.startswith(device + "/")))
        if not players:
            return None
        playing = [p for p in players
                   if self.objects[p][IFACE_PLAYER].get("Status") in PLAYING_STATES]
        return (playing or players)[-1]

    def summary(self):
        adapter = self._adapter_path()
        aprops = self.objects.get(adapter, {}).get(IFACE_ADAPTER, {}) if adapter else {}
        device = self._current_device()
        player = self._current_player(device)
        pprops = self.objects.get(player, {}).get(IFACE_PLAYER, {}) if player else {}
        return {
            "_present": bool(self.present and adapter),
            "Device": self._device_name(device) if device else None,
            "Player": bool(player),
            "Status": pprops.get("Status", ""),
            "Track": dict(pprops.get("Track", {})),
            "Position": pprops.get("Position"),
            "Pairing": self.pairing,
            "Alias": aprops.get("Alias", ""),
            "Paired": [self._device_name(p) for p in self._paired_devices()],
        }

    def _emit(self):
        now = self.summary()
        changes = {k: v for k, v in now.items() if self._last.get(k, _MISSING) != v}
        if "Track" in changes or "Status" in changes:
            changes["Position"] = now["Position"]  # re-anchor the progress bar
        self._last = now
        if changes:
            self._on_update(changes)

    # == adapter setup =================================================================
    def _is_own_echo(self, value: bool) -> bool:
        """Is this Discoverable change just BlueZ confirming one of our own (asynchronous) writes?

        Those confirmations can arrive late and out of step with self.pairing, so acting on them as if
        someone else had changed it makes pairing mode flip on and off forever.
        """
        now = time.monotonic()
        self._own_writes = [(v, t) for v, t in self._own_writes if now - t < OWN_WRITE_GRACE_S]
        for i, (v, _t) in enumerate(self._own_writes):
            if v == value:
                del self._own_writes[i]
                return True
        return False

    def _set(self, path, iface, prop, variant, on_done=None):
        if iface == IFACE_ADAPTER and prop == "Discoverable":
            self._own_writes.append((bool(variant.unpack()), time.monotonic()))

        def done(conn, result):
            try:
                conn.call_finish(result)
            except GLib.Error as err:
                log.warning("Setting %s.%s failed: %s", iface, prop, err.message)
            if on_done:
                on_done()

        self._conn.call(BLUEZ, path, PROPS, "Set", GLib.Variant("(ssv)", (iface, prop, variant)),
                        None, Gio.DBusCallFlags.NONE, 5000, None, done)

    def _call(self, path, iface, method, args=None, on_ok=None, on_error=None, timeout=10000):
        def done(conn, result):
            try:
                conn.call_finish(result)
            except GLib.Error as err:
                log.info("%s.%s on %s: %s", iface, method, path, err.message)
                if on_error:
                    on_error(err.message)
                return
            if on_ok:
                on_ok()

        self._conn.call(BLUEZ, path, iface, method, args, None, Gio.DBusCallFlags.NONE, timeout,
                        None, done)

    def _register_agent(self):
        def made_default():
            log.info("Pairing agent registered")

        def registered():
            self._call("/org/bluez", IFACE_AGENT_MANAGER, "RequestDefaultAgent",
                       GLib.Variant("(o)", (AGENT_PATH,)), on_ok=made_default)

        self._call("/org/bluez", IFACE_AGENT_MANAGER, "RegisterAgent",
                   GLib.Variant("(os)", (AGENT_PATH, "DisplayYesNo")),
                   on_ok=registered, on_error=lambda msg: registered())  # AlreadyExists is fine

    def _setup_adapter(self):
        adapter = self._adapter_path()
        if not adapter:
            log.warning("No Bluetooth adapter found")
            return
        props = self.objects[adapter].get(IFACE_ADAPTER, {})
        if not props.get("Powered"):
            self._set(adapter, IFACE_ADAPTER, "Powered", GLib.Variant("b", True))
        if self.alias and props.get("Alias") != self.alias:
            self._set(adapter, IFACE_ADAPTER, "Alias", GLib.Variant("s", self.alias))
        if not self.pairing:
            # Only accept new phones while pairing mode is on; paired phones can always connect.
            if props.get("Discoverable"):
                self._set(adapter, IFACE_ADAPTER, "Discoverable", GLib.Variant("b", False))
            if props.get("Pairable"):
                self._set(adapter, IFACE_ADAPTER, "Pairable", GLib.Variant("b", False))

    # == pairing ==========================================================================
    def start_pairing(self):
        adapter = self._adapter_path()
        if not adapter:
            self._on_agent("failed", "", "Bluetooth isn't available")
            return False
        self.pairing = True
        self.pairing_until = time.monotonic() + self.pair_seconds
        seconds = GLib.Variant("u", self.pair_seconds)
        self._set(adapter, IFACE_ADAPTER, "PairableTimeout", seconds)
        self._set(adapter, IFACE_ADAPTER, "DiscoverableTimeout", seconds)
        self._set(adapter, IFACE_ADAPTER, "Pairable", GLib.Variant("b", True))
        self._set(adapter, IFACE_ADAPTER, "Discoverable", GLib.Variant("b", True))
        if self._pairing_source:
            GLib.source_remove(self._pairing_source)
        self._pairing_source = GLib.timeout_add_seconds(self.pair_seconds, self._pairing_timeout)
        log.info("Pairing mode on for %d s", self.pair_seconds)
        self._emit()
        return True

    def _pairing_timeout(self):
        self._pairing_source = 0
        log.info("Pairing mode timed out")
        self.stop_pairing()
        return False

    def stop_pairing(self):
        if self._pairing_source:
            GLib.source_remove(self._pairing_source)
            self._pairing_source = 0
        was = self.pairing
        self.pairing = False
        self._drop_pending(reject=True)
        adapter = self._adapter_path()
        if adapter:
            self._set(adapter, IFACE_ADAPTER, "Discoverable", GLib.Variant("b", False))
            self._set(adapter, IFACE_ADAPTER, "Pairable", GLib.Variant("b", False))
        if was:
            self._emit()

    def answer(self, accept: bool):
        """The user pressed A (accept) or B (reject) on the confirm-code screen."""
        if not self._pending:
            return
        invocation, device, source = self._pending
        self._pending = None
        GLib.source_remove(source)
        if accept:
            log.info("Pairing with %s accepted on the device", self._device_name(device))
            invocation.return_value(None)
        else:
            invocation.return_dbus_error("org.bluez.Error.Rejected", "Rejected on the Pirate Audio")

    def _drop_pending(self, reject: bool):
        if not self._pending:
            return
        invocation, _device, source = self._pending
        self._pending = None
        GLib.source_remove(source)
        if reject:
            invocation.return_dbus_error("org.bluez.Error.Rejected", "Pairing mode ended")

    def _confirm_timeout(self):
        if self._pending:
            invocation, _device, _source = self._pending
            self._pending = None
            invocation.return_dbus_error("org.bluez.Error.Rejected", "No answer on the Pirate Audio")
            self._on_agent("cancel", "", "")
        return False

    def _agent_call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        device = args[0] if args else None
        name = self._device_name(device) if device else ""
        reject = lambda why: invocation.return_dbus_error("org.bluez.Error.Rejected", why)  # noqa: E731
        log.debug("agent %s %s", method, args)

        if method in ("Release",):
            invocation.return_value(None)
        elif method in ("RequestPinCode", "RequestPasskey"):
            reject("This device can only pair by confirming a code")
        elif method == "DisplayPinCode":
            self._on_agent("display", name, str(args[1]))
            invocation.return_value(None)
        elif method == "DisplayPasskey":
            self._on_agent("display", name, f"{args[1]:06d}")
            invocation.return_value(None)
        elif method == "RequestConfirmation":
            if not self.pairing:
                reject("Not in pairing mode")
                return
            self._drop_pending(reject=True)
            source = GLib.timeout_add_seconds(CONFIRM_TIMEOUT_S, self._confirm_timeout)
            self._pending = (invocation, device, source)
            self._on_agent("confirm", name, f"{args[1]:06d}")
        elif method == "RequestAuthorization":
            if self.pairing:
                invocation.return_value(None)
            else:
                reject("Not in pairing mode")
        elif method == "AuthorizeService":
            paired = self.objects.get(device, {}).get(IFACE_DEVICE, {}).get("Paired")
            if self.pairing or paired:
                invocation.return_value(None)
                self._set(device, IFACE_DEVICE, "Trusted", GLib.Variant("b", True))
            else:
                reject("Unknown device")
        elif method == "Cancel":
            if self._pending:
                _inv, _dev, source = self._pending
                GLib.source_remove(source)
                self._pending = None
                self._on_agent("cancel", "", "")
            invocation.return_value(None)
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    # == playback commands ===================================================================
    def command(self, action, on_error=None) -> bool:
        """action: "playpause" | "next" | "previous". Returns False if there's no player."""
        player = self._current_player(self.device_path)
        if not player:
            return False
        if action == "playpause":
            status = self.objects[player][IFACE_PLAYER].get("Status")
            method = "Pause" if status in PLAYING_STATES else "Play"
        else:
            method = {"next": "Next", "previous": "Previous"}[action]
        self._call(player, IFACE_PLAYER, method, on_error=on_error)
        return True

    def reconnect(self):
        """Ask paired phones (one at a time) to connect, like headphones do when switched on."""
        if self._current_device():
            return
        devices = [p for p in self._paired_devices()
                   if self.objects[p][IFACE_DEVICE].get("Trusted")]

        def attempt(remaining):
            if not remaining or self._current_device():
                return
            path = remaining[0]
            log.info("Trying to reconnect to %s", self._device_name(path))
            self._call(path, IFACE_DEVICE, "Connect", on_error=lambda _m: attempt(remaining[1:]),
                       timeout=30000)

        attempt(devices)

    def disconnect(self):
        if self.device_path:
            self._call(self.device_path, IFACE_DEVICE, "Disconnect")

    def close(self):
        self._drop_pending(reject=True)
        if self._watch:
            Gio.bus_unwatch_name(self._watch)
            self._watch = 0
        for sub in self._subs:
            self._conn.signal_unsubscribe(sub)
        self._subs = []
        if self._agent_reg:
            self._conn.unregister_object(self._agent_reg)
            self._agent_reg = 0
