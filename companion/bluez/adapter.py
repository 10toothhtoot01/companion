"""org.bluez.Adapter1 wrapper: power, discovery filter, start/stop discovery.

Fixes two correctness gaps:
  - the adapter is explicitly powered on before we expect to see devices
  - discovery is filtered (less noise/airtime) and is STOPPED, not left running forever

SetDiscoveryFilter takes a{sv}; we pass Transport="auto" and DuplicateData=false. We also
subscribe nothing here — device arrival is handled centrally by BluezManager via the
ObjectManager. This wrapper is intentionally small and side-effect-explicit.
"""
from __future__ import annotations

import logging

from dbus_fast import Variant

from .constants import ADAPTER_IFACE, BLUEZ_SERVICE, PROPERTIES_IFACE

log = logging.getLogger(__name__)


class Adapter:
    def __init__(self, bus, path: str) -> None:
        self._bus = bus
        self.path = path
        self._iface = None
        self._props = None
        self._discovering = False

    async def _ensure(self):
        if self._iface is None:
            introspection = await self._bus.introspect(BLUEZ_SERVICE, self.path)
            obj = self._bus.get_proxy_object(BLUEZ_SERVICE, self.path, introspection)
            self._iface = obj.get_interface(ADAPTER_IFACE)
            self._props = obj.get_interface(PROPERTIES_IFACE)
        return self._iface

    async def power_on(self) -> None:
        iface = await self._ensure()
        try:
            if not await iface.get_powered():
                await iface.set_powered(True)
                log.info("powered on adapter %s", self.path)
        except Exception:
            log.exception("could not power on adapter %s", self.path)

    async def set_filter(self) -> None:
        iface = await self._ensure()
        flt = {
            "Transport": Variant("s", "auto"),
            "DuplicateData": Variant("b", False),
        }
        try:
            await iface.call_set_discovery_filter(flt)
        except Exception:
            log.debug("adapter %s rejected discovery filter (non-fatal)", self.path)

    async def start_discovery(self) -> None:
        iface = await self._ensure()
        if self._discovering:
            return
        await self.set_filter()
        await iface.call_start_discovery()
        self._discovering = True
        log.info("discovery started on %s", self.path)

    async def stop_discovery(self) -> None:
        if not self._discovering:
            return
        iface = await self._ensure()
        try:
            await iface.call_stop_discovery()
        finally:
            self._discovering = False
            log.info("discovery stopped on %s", self.path)

    @property
    def discovering(self) -> bool:
        return self._discovering

    async def powered(self) -> bool:
        """Current Adapter1.Powered state (False if unreadable)."""
        iface = await self._ensure()
        try:
            return bool(await iface.get_powered())
        except Exception:
            log.debug("could not read Powered on %s", self.path)
            return False

    async def set_powered(self, on: bool) -> None:
        iface = await self._ensure()
        await iface.set_powered(bool(on))
        log.info("set %s Powered=%s", self.path, on)

    async def remove_device(self, device_path: str) -> None:
        """org.bluez.Adapter1.RemoveDevice — unpair / forget a known device."""
        iface = await self._ensure()
        await iface.call_remove_device(device_path)
        log.info("removed device %s from %s", device_path, self.path)

    async def watch(self, cb) -> None:
        """Subscribe to Adapter1 PropertiesChanged; invoke cb() on any change.

        Used to surface live Powered on/off (e.g. the user toggles Bluetooth in
        GNOME) without polling. Safe to call once per adapter.
        """
        await self._ensure()

        def _on_props(iface_name, _changed, _invalidated):
            if iface_name == ADAPTER_IFACE:
                cb()

        try:
            self._props.on_properties_changed(_on_props)
        except Exception:
            log.debug("could not watch adapter props on %s", self.path)
