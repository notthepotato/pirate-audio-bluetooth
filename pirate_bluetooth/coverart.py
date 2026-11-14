"""Album art from the phone over Bluetooth (AVRCP cover art, sent over OBEX "BIP").

Needs BlueZ 5.79 or newer with its experimental features on, plus obexd (package bluez-obexd) on the
user's session bus; install.sh sets both up on Trixie. A phone that offers cover art shows an ObexPort on
its media player (iPhones use L2CAP PSM 0x1009). The phone only names a picture for each song (the
track's ImgHandle) while an OBEX "bip-avrcp" session to that port is open, so one is opened as soon as
the port shows up and kept open (and reopened if it ends) while the phone is connected.
Image1.GetThumbnail then fetches the picture: a 200x200 JPEG that every phone must offer, which is
plenty for the 240x240 screen. (Image1.Get with an empty description is refused by some phones.)

Because BlueZ reads a song's details once, the song that's playing when the session opens usually
comes without a picture; the next one has it. app.py looks that one up online instead.
"""

import hashlib
import logging
import os

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

OBEX = "org.bluez.obex"
OBEX_PATH = "/org/bluez/obex"
CLIENT = "org.bluez.obex.Client1"
IMAGE = "org.bluez.obex.Image1"
TRANSFER = "org.bluez.obex.Transfer1"
QUICK_TRIES = 4  # obexd often refuses the very first session right after it starts: try again soon...
QUICK_RETRY_S = 2
SLOW_RETRY_S = 60  # ...then only now and then (the phone may simply not allow it)
TRANSFER_TIMEOUT_S = 20


class CoverArt:
    def __init__(self, cache_dir, on_image, on_connected=None):
        """on_image(key, path_or_None) is called on the GLib main loop; on_image(None, None) means the art
        connection didn't work out (so there's no point waiting for the phone's pictures).
        on_connected() is called when the art connection is (back) up."""
        self.dir = cache_dir
        self._on_image = on_image
        self._on_connected = on_connected
        self.session = None
        self.target = None  # (phone address, OBEX port)
        self._connecting = False
        self._failures = 0
        self._retry = 0  # GLib source of a scheduled reconnect
        self._want = None  # (handle, key) to fetch once the session is up
        self._transfers = {}  # transfer path -> (key, file, timeout source)
        self._subs = []
        self._watch = 0
        try:
            os.makedirs(cache_dir, exist_ok=True)
            self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except (GLib.Error, OSError) as err:
            log.info("No session bus, so no album art over Bluetooth: %s", err)
            self.conn = None
            return
        self._subs = [
            self.conn.signal_subscribe(None, "org.freedesktop.DBus.Properties", "PropertiesChanged", None,
                                       TRANSFER, Gio.DBusSignalFlags.NONE, self._props_changed),
            self.conn.signal_subscribe(OBEX, "org.freedesktop.DBus.ObjectManager", "InterfacesRemoved", None,
                                       None, Gio.DBusSignalFlags.NONE, self._removed),
        ]
        self._watch = Gio.bus_watch_name_on_connection(self.conn, OBEX, Gio.BusNameWatcherFlags.NONE,
                                                       None, self._obexd_gone)

    @property
    def connected(self):
        return bool(self.session)

    @property
    def offered(self):
        """The connected phone offers album art over Bluetooth, and it's worth waiting for."""
        if not self.conn or not self.target:
            return False
        return self.connected or self._connecting or self._failures < QUICK_TRIES

    def set_target(self, address, port):
        """The connected phone's address and its media player's OBEX port (None when there's none)."""
        target = (address, int(port)) if address and port else None
        if target == self.target:
            return
        self._drop_session()
        self.target = target
        self._failures = 0
        if target:
            self._connect()

    def fetch(self, handle, key):
        """Get the picture for a track's ImgHandle; on_image(key, path) once it's here."""
        if not self.conn or not self.target:
            return False
        if not self.session:
            self._want = (handle, key)
            self._connect()
            return True
        name = hashlib.sha1(f"{key}|{handle}".encode()).hexdigest()
        dest = os.path.join(self.dir, f"bt-{name}.jpg")
        self.conn.call(OBEX, self.session, IMAGE, "GetThumbnail", GLib.Variant("(ss)", (dest, handle)),
                       GLib.VariantType("(oa{sv})"), Gio.DBusCallFlags.NONE, 15000, None,
                       self._started, (key, dest, self.session))
        return True

    def close(self):
        self._drop_session()
        self.target = None
        if self.conn:
            for sub in self._subs:
                self.conn.signal_unsubscribe(sub)
            self._subs = []
        if self._watch:
            Gio.bus_unwatch_name(self._watch)
            self._watch = 0

    # -- session -------------------------------------------------------------------------------
    def _connect(self):
        self._cancel_retry()
        if self._connecting or self.session or not self.target or not self.conn:
            return False
        self._connecting = True
        address, port = self.target
        # BIP over L2CAP (a PSM, e.g. 0x1009) is the norm; a small number would be an RFCOMM channel.
        where = {"Channel": GLib.Variant("y", port)} if port <= 30 else {"PSM": GLib.Variant("q", port)}
        args = GLib.Variant("(sa{sv})", (address, {"Target": GLib.Variant("s", "bip-avrcp"), **where}))
        self.conn.call(OBEX, OBEX_PATH, CLIENT, "CreateSession", args, GLib.VariantType("(o)"),
                       Gio.DBusCallFlags.NONE, 20000, None, self._created, self.target)
        return False

    def _created(self, conn, result, target):
        self._connecting = False
        try:
            path = conn.call_finish(result).unpack()[0]
        except GLib.Error as err:
            if target != self.target:
                return
            self._failures += 1
            quick = self._failures < QUICK_TRIES
            (log.debug if quick else log.info)("Album art connection to the phone didn't work (try %d): %s",
                                               self._failures, err.message)
            if not self._give_up_wanted():
                GLib.idle_add(self._deliver, None, None)  # "don't wait for the phone's art"
            self._schedule_retry(QUICK_RETRY_S if quick else SLOW_RETRY_S)
            return
        if target != self.target:  # the phone changed meanwhile
            self._remove_session(path)
            return
        self.session = path
        self._failures = 0
        log.info("Connected to the phone for album art over Bluetooth")
        if self._want:
            handle, key = self._want
            self._want = None
            self.fetch(handle, key)
        if self._on_connected:
            self._on_connected()

    def _schedule_retry(self, seconds):
        self._cancel_retry()
        if self.target:
            self._retry = GLib.timeout_add_seconds(seconds, self._connect)

    def _cancel_retry(self):
        if self._retry:
            GLib.source_remove(self._retry)
            self._retry = 0

    def _lost_session(self, why):
        """The session ended without us closing it (phone hung up, obexd restarted): open a new one."""
        log.info("Album art connection ended (%s); reconnecting", why)
        self.session = None
        for path in list(self._transfers):
            self._finish(path, False)
        self._schedule_retry(1)

    def _removed(self, conn, sender, path, iface, signal, params):
        obj, _ifaces = params.unpack()
        if self.session and obj == self.session:
            self._lost_session("closed by the phone")

    def _obexd_gone(self, conn, name):
        if self.session:
            self._lost_session("obexd stopped")

    def _give_up_wanted(self):
        if self._want:
            _handle, key = self._want
            self._want = None
            GLib.idle_add(self._deliver, key, None)
            return True
        return False

    def _drop_session(self):
        self._cancel_retry()
        for path in list(self._transfers):
            self._finish(path, False)
        self._want = None
        if self.session:
            self._remove_session(self.session)
            self.session = None

    def _remove_session(self, path):
        if self.conn:
            self.conn.call(OBEX, OBEX_PATH, CLIENT, "RemoveSession", GLib.Variant("(o)", (path,)), None,
                           Gio.DBusCallFlags.NONE, 5000, None, None, None)

    # -- transfers -------------------------------------------------------------------------------
    def _started(self, conn, result, data):
        key, dest, session = data
        try:
            transfer, props = conn.call_finish(result).unpack()
        except GLib.Error as err:
            log.info("Couldn't get album art over Bluetooth: %s", err.message)
            if session == self.session and ("UnknownObject" in err.message or "UnknownMethod" in err.message):
                self._lost_session("gone")
            self._deliver(key, None)
            return
        source = GLib.timeout_add_seconds(TRANSFER_TIMEOUT_S, self._timed_out, transfer)
        self._transfers[transfer] = (key, dest, source)
        if props.get("Status") in ("complete", "error"):
            self._finish(transfer, props["Status"] == "complete")

    def _props_changed(self, conn, sender, path, iface, signal, params):
        if path not in self._transfers:
            return
        _iface, changed, _invalid = params.unpack()
        status = changed.get("Status")
        if status in ("complete", "error"):
            self._finish(path, status == "complete")

    def _timed_out(self, path):
        if path in self._transfers:
            log.info("Album art over Bluetooth took too long")
            key, dest, _source = self._transfers[path]
            self._transfers[path] = (key, dest, 0)  # this timer is ending by itself
            self._finish(path, False)
        return False

    def _finish(self, path, ok):
        key, dest, source = self._transfers.pop(path)
        if source:
            GLib.source_remove(source)
        if ok and os.path.exists(dest) and os.path.getsize(dest) > 0:
            log.info("Album art received over Bluetooth")
            self._deliver(key, dest)
        else:
            try:
                os.remove(dest)
            except OSError:
                pass
            self._deliver(key, None)

    def _deliver(self, key, path):
        self._on_image(key, path)
        return False
