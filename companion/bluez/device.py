"""Async wrapper over org.bluez.Device1 (+ Battery1) for one device.

Methods/properties used are exactly those documented in the BlueZ Device1 API:
  methods   : Connect(), Disconnect(), ConnectProfile(uuid), Pair(), CancelPairing()
  properties: Address, Name/Alias, Paired, Connected, Trusted, RSSI, UUIDs, Icon, Class
  Battery1  : Percentage

We subscribe to PropertiesChanged so the UI reflects live state without polling, and we
keep a handle to the subscription so we can UNSUBSCRIBE when the device goes away (no
leaked signal handlers).
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from dbus_fast import Variant

from .constants import BATTERY_IFACE, BLUEZ_SERVICE, DEVICE_IFACE, PROPERTIES_IFACE
from companion.core.models import Codec, DeviceKind, Device, NowPlaying

log = logging.getLogger(__name__)

_ICON_TO_KIND = {
    "audio-headphones": DeviceKind.HEADPHONES,
    "audio-headset": DeviceKind.HEADPHONES,
    "audio-card": DeviceKind.SPEAKER,
    "audio-speakers": DeviceKind.SPEAKER,
    "input-mouse": DeviceKind.INPUT,
    "input-keyboard": DeviceKind.INPUT,
    "input-gaming": DeviceKind.INPUT,
    "phone": DeviceKind.PHONE,
}

_EARBUD_HINTS = ("buds", "earbud", "airpods", "pods", "earphone", "freebuds")


def classify(icon: Optional[str], name: Optional[str]) -> DeviceKind:
    """Pure kind classifier (icon first, then a name heuristic for earbuds)."""
    lname = (name or "").lower()
    if any(h in lname for h in _EARBUD_HINTS):
        return DeviceKind.EARBUDS
    return _ICON_TO_KIND.get(icon or "", DeviceKind.UNKNOWN)


class BluezDevice:
    def __init__(self, bus, path: str) -> None:
        self._bus = bus
        self.path = path
        self._iface = None           # org.bluez.Device1
        self._props_iface = None     # org.freedesktop.DBus.Properties
        self._changed_handler = None
        self.model = Device(path=path, address="", name="(unknown)")

    # ---- lifecycle -------------------------------------------------------
    async def _ensure_ifaces(self):
        if self._iface is None:
            introspection = await self._bus.introspect(BLUEZ_SERVICE, self.path)
            obj = self._bus.get_proxy_object(BLUEZ_SERVICE, self.path, introspection)
            self._iface = obj.get_interface(DEVICE_IFACE)
            self._props_iface = obj.get_interface(PROPERTIES_IFACE)
        return self._iface

    async def watch(self, on_change: Callable[["BluezDevice"], None]) -> None:
        await self._ensure_ifaces()

        def _changed(interface: str, changed: dict, invalidated: list) -> None:
            vals = {k: (v.value if isinstance(v, Variant) else v)
                    for k, v in changed.items()}
            if interface == DEVICE_IFACE:
                self.update_from_props(vals)
            elif interface == BATTERY_IFACE and "Percentage" in vals:
                self.model.battery = int(vals["Percentage"])
            on_change(self)

        self._changed_handler = _changed
        self._props_iface.on_properties_changed(_changed)

    def unwatch(self) -> None:
        """Remove the PropertiesChanged subscription (called on device removal)."""
        if self._props_iface is not None and self._changed_handler is not None:
            try:
                self._props_iface.off_properties_changed(self._changed_handler)
            except Exception:
                pass
            self._changed_handler = None

    # ---- state mapping ---------------------------------------------------
    def update_from_props(self, props: dict, battery: Optional[int] = None) -> None:
        m = self.model
        if "Address" in props:
            m.address = props["Address"]
        if props.get("Alias"):
            m.name = props["Alias"]
        elif props.get("Name"):
            m.name = props["Name"]
        for key, attr in (("Paired", "paired"), ("Connected", "connected"),
                          ("Trusted", "trusted")):
            if key in props:
                setattr(m, attr, bool(props[key]))
        if "RSSI" in props:
            m.rssi = int(props["RSSI"])
        if "UUIDs" in props:
            m.uuids = list(props["UUIDs"])
            from companion.bluez.constants import device_supports_mic
            m.has_mic = device_supports_mic(m.uuids)
        if "Icon" in props:
            m.icon = props["Icon"]
        # Re-classify whenever icon or name may have changed.
        if m.kind is DeviceKind.UNKNOWN or "Icon" in props or "Alias" in props or "Name" in props:
            kind = classify(m.icon, m.name)
            if kind is not DeviceKind.UNKNOWN:
                m.kind = kind
        if battery is not None:
            m.battery = int(battery)

    def set_codec(self, codec: Optional[Codec]) -> None:
        self.model.codec = codec

    def set_volume(self, fraction: Optional[float]) -> None:
        self.model.volume = fraction

    def set_now_playing(self, now: Optional[NowPlaying]) -> None:
        self.model.now_playing = now

    # ---- actions ---------------------------------------------------------
    async def connect(self) -> None:
        await (await self._ensure_ifaces()).call_connect()

    async def disconnect(self) -> None:
        await (await self._ensure_ifaces()).call_disconnect()

    async def connect_profile(self, uuid: str) -> None:
        await (await self._ensure_ifaces()).call_connect_profile(uuid)

    async def pair(self) -> None:
        """Pair a new device. Requires a registered Agent1 (see bluez.agent)."""
        iface = await self._ensure_ifaces()
        await iface.call_pair()
        # Trust so it auto-reconnects later without re-authorizing each profile.
        try:
            await iface.set_trusted(True)
        except Exception:
            log.debug("could not set Trusted on %s", self.path)

    async def cancel_pairing(self) -> None:
        try:
            await (await self._ensure_ifaces()).call_cancel_pairing()
        except Exception:
            pass

    async def set_trusted(self, trusted: bool) -> None:
        await self._ensure_ifaces()  # ensure props iface ready
        await self._props_iface.call_set(
            DEVICE_IFACE, "Trusted", Variant("b", bool(trusted)))

    async def set_alias(self, alias: str) -> None:
        """Set a friendly name via the writable Device1.Alias property. BlueZ persists it
        and emits PropertiesChanged, so update_from_props() reflects it back into the
        model. Setting Alias to "" would reset to the remote Name, so callers must pass a
        non-empty string (see capabilities.clean_alias).

        We write through the standard org.freedesktop.DBus.Properties.Set rather than a
        generated `set_alias` accessor: it is independent of dbus-fast's property-setter
        naming and works across versions, and it sends the value with the exact 's'
        signature BlueZ expects for Alias."""
        await self._ensure_ifaces()  # ensure props iface ready
        await self._props_iface.call_set(
            DEVICE_IFACE, "Alias", Variant("s", str(alias)))

    async def reconnect(self) -> None:
        """Disconnect then connect — used to apply a new codec immediately."""
        await self.disconnect()
        await self.connect()
