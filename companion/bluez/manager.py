"""BlueZ object discovery + lifecycle via the freedesktop ObjectManager.

Async `dbus-fast` client. Pattern follows the BlueZ API docs:
  - GetManagedObjects() for the initial snapshot
  - InterfacesAdded / InterfacesRemoved for device + battery + player arrival/removal
  - PropertiesChanged (per-object) for live updates

Responsibilities (closing the audit gaps):
  - power on every adapter (C5) and register a pairing agent (C4) on connect
  - START discovery with a filter and STOP it on demand / timeout (C5)
  - handle Battery1 arriving AFTER the device (Mo3)
  - track MediaPlayer1 for now-playing (M5) and unsubscribe handlers on removal

Callbacks are plain functions; the UI layer adapts them to Qt signals so this module
stays framework-free and unit-testable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus

from .adapter import Adapter
from .agent import PairingAgent, register_agent, unregister_agent
from .constants import (
    ADAPTER_IFACE,
    BATTERY_IFACE,
    BLUEZ_SERVICE,
    DEVICE_IFACE,
    MEDIA_PLAYER_IFACE,
    OBJECT_MANAGER_IFACE,
)
from .device import BluezDevice
from . import media
from companion.core import capabilities, rfkill

log = logging.getLogger(__name__)

_CALL_TIMEOUT = 8.0


async def _with_timeout(coro, what: str):
    try:
        return await asyncio.wait_for(coro, timeout=_CALL_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("D-Bus call timed out: %s", what)
        raise


def _unwrap(props: dict) -> dict:
    return {k: (v.value if isinstance(v, Variant) else v) for k, v in props.items()}


class BluezManager:
    def __init__(self,
                 on_confirm=None,
                 on_display=None) -> None:
        self._bus: Optional[MessageBus] = None
        self._om = None
        self._devices: dict[str, BluezDevice] = {}
        self._adapters: dict[str, Adapter] = {}
        self._agent = PairingAgent(on_confirm=on_confirm, on_display=on_display)
        self.on_device_added: Optional[Callable[[BluezDevice], None]] = None
        self.on_device_removed: Optional[Callable[[str], None]] = None
        self.on_device_changed: Optional[Callable[[BluezDevice], None]] = None
        self.on_adapter_changed: Optional[Callable[[], None]] = None

    # ---- connection ------------------------------------------------------
    async def connect(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        introspection = await _with_timeout(
            self._bus.introspect(BLUEZ_SERVICE, "/"), "introspect(/)")
        root = self._bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
        self._om = root.get_interface(OBJECT_MANAGER_IFACE)
        self._om.on_interfaces_added(self._handle_added)
        self._om.on_interfaces_removed(self._handle_removed)
        # Register the pairing agent so new devices can actually pair.
        try:
            await register_agent(self._bus, self._agent)
        except Exception:
            log.exception("could not register pairing agent (pairing may fail)")
        await self.resync()
        # Power on adapters so devices are visible, and watch for live power / block
        # changes (e.g. the user toggles Bluetooth in GNOME) so the banner stays honest.
        for adapter in self._adapters.values():
            await adapter.power_on()
            await adapter.watch(self._notify_adapter_changed)
        self._notify_adapter_changed()

    async def resync(self) -> None:
        objects = await _with_timeout(
            self._om.call_get_managed_objects(), "GetManagedObjects")
        for path, interfaces in objects.items():
            if ADAPTER_IFACE in interfaces and path not in self._adapters:
                self._adapters[path] = Adapter(self._bus, path)
            if DEVICE_IFACE in interfaces:
                await self._track(path, interfaces)

    # ---- device tracking -------------------------------------------------
    async def _track(self, path: str, interfaces: dict) -> None:
        props = _unwrap(interfaces.get(DEVICE_IFACE, {}))
        battery = _unwrap(interfaces.get(BATTERY_IFACE, {})).get("Percentage")
        dev = self._devices.get(path)
        if dev is None:
            dev = BluezDevice(self._bus, path)
            self._devices[path] = dev
            dev.update_from_props(props, battery=battery)
            await dev.watch(self._on_props_changed)
            if self.on_device_added:
                self.on_device_added(dev)
        else:
            dev.update_from_props(props, battery=battery)
            if self.on_device_changed:
                self.on_device_changed(dev)

    def _on_props_changed(self, dev: BluezDevice) -> None:
        if self.on_device_changed:
            self.on_device_changed(dev)

    def _handle_added(self, path: str, interfaces: dict) -> None:
        if ADAPTER_IFACE in interfaces and path not in self._adapters:
            adapter = Adapter(self._bus, path)
            self._adapters[path] = adapter
            asyncio.ensure_future(adapter.watch(self._notify_adapter_changed))
            self._notify_adapter_changed()
        if DEVICE_IFACE in interfaces:
            asyncio.ensure_future(self._track(path, interfaces))
        elif BATTERY_IFACE in interfaces:
            # Mo3: Battery1 commonly arrives AFTER the device. Find owner & update.
            owner = self._devices.get(path)
            if owner is None:
                # Battery path equals the device path for BlueZ; match by prefix too.
                owner = next((d for p, d in self._devices.items()
                              if path.startswith(p)), None)
            if owner is not None:
                pct = _unwrap(interfaces.get(BATTERY_IFACE, {})).get("Percentage")
                if pct is not None:
                    owner.model.battery = int(pct)
                    if self.on_device_changed:
                        self.on_device_changed(owner)
        elif MEDIA_PLAYER_IFACE in interfaces:
            owner = next((d for p, d in self._devices.items()
                          if path.startswith(p)), None)
            if owner is not None:
                asyncio.ensure_future(self._refresh_now_playing(owner))

    def _handle_removed(self, path: str, interfaces: list) -> None:
        if DEVICE_IFACE in interfaces and path in self._devices:
            dev = self._devices.pop(path)
            dev.unwatch()                      # no leaked signal handler
            if self.on_device_removed:
                self.on_device_removed(path)
        if ADAPTER_IFACE in interfaces and path in self._adapters:
            self._adapters.pop(path, None)
            self._notify_adapter_changed()

    async def _refresh_now_playing(self, dev: BluezDevice) -> None:
        try:
            now = await media.read_now_playing(self._bus, dev.path)
            dev.set_now_playing(now)
            if self.on_device_changed:
                self.on_device_changed(dev)
        except Exception:
            log.debug("now-playing read failed for %s", dev.path)

    # ---- queries / discovery --------------------------------------------
    @property
    def devices(self) -> list[BluezDevice]:
        return list(self._devices.values())

    def device(self, path: str) -> Optional[BluezDevice]:
        return self._devices.get(path)

    # ---- adapter state ---------------------------------------------------
    def _notify_adapter_changed(self) -> None:
        if self.on_adapter_changed:
            self.on_adapter_changed()

    async def adapter_state(self):
        """Classify the current adapter situation (absent / blocked / off / ready)."""
        present = bool(self._adapters)
        powered = False
        for adapter in self._adapters.values():
            try:
                if await adapter.powered():
                    powered = True
                    break
            except Exception:
                log.debug("powered() failed for %s", adapter.path)
        soft, hard = rfkill.bluetooth_blocked()
        return capabilities.classify_adapter(
            present, powered, rfkill_soft=soft, rfkill_hard=hard)

    async def power_on_adapters(self) -> bool:
        if not self._adapters:
            await self.resync()
        ok = False
        for adapter in self._adapters.values():
            try:
                await adapter.power_on()
                ok = True
            except Exception:
                log.exception("power on failed for %s", adapter.path)
        self._notify_adapter_changed()
        return ok

    async def remove_device(self, path: str) -> bool:
        """Forget a device via its owning adapter's RemoveDevice."""
        adapter_path = path.rsplit("/", 1)[0]
        adapter = self._adapters.get(adapter_path) or next(
            iter(self._adapters.values()), None)
        if adapter is None:
            return False
        await _with_timeout(adapter.remove_device(path), "RemoveDevice")
        return True

    async def start_discovery(self) -> None:
        if not self._adapters:
            await self.resync()
        for adapter in self._adapters.values():
            await adapter.power_on()
            try:
                await _with_timeout(adapter.start_discovery(), "StartDiscovery")
            except Exception:
                log.exception("start discovery failed on %s", adapter.path)

    async def stop_discovery(self) -> None:
        for adapter in self._adapters.values():
            try:
                await _with_timeout(adapter.stop_discovery(), "StopDiscovery")
            except Exception:
                log.debug("stop discovery failed on %s", adapter.path)

    async def disconnect(self) -> None:
        await self.stop_discovery()
        if self._bus:
            await unregister_agent(self._bus)
            self._bus.disconnect()
            self._bus = None
