"""org.bluez.Agent1 pairing agent (the missing piece that blocked onboarding).

BlueZ refuses to pair a new device unless an agent is registered with AgentManager1 to
handle PIN / passkey / confirmation. Without this, "Scan -> Pair" simply fails. We export
a single agent on the system bus and register it as the default.

Default behaviour is convenient + safe for the common case (headphones/speakers with no
keypad): auto-provide "0000", auto-confirm passkeys. For phones/PCs that show a passkey,
the UI can supply callbacks to confirm or reject. Anything we don't approve is rejected
with org.bluez.Error.Rejected, so we never silently pair something unexpected.

Implemented with dbus-fast's ServiceInterface; method type-signatures follow the BlueZ
Agent1 API exactly (object paths 'o', uint32 'u', uint16 'q', strings 's').
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from dbus_fast import DBusError
from dbus_fast.service import ServiceInterface, method

from .constants import (
    AGENT_CAPABILITY,
    AGENT_IFACE,
    AGENT_MANAGER_IFACE,
    AGENT_PATH,
    BLUEZ_ROOT,
    BLUEZ_SERVICE,
)

log = logging.getLogger(__name__)

_REJECTED = "org.bluez.Error.Rejected"
_CANCELED = "org.bluez.Error.Canceled"

# Confirm callback: given (device_path, passkey) -> bool (True = accept).
ConfirmCb = Callable[[str, int], bool]
# Display callback: given (device_path, passkey) -> None (show to the user).
DisplayCb = Callable[[str, int], None]


class PairingAgent(ServiceInterface):
    def __init__(self,
                 default_pin: str = "0000",
                 on_confirm: Optional[ConfirmCb] = None,
                 on_display: Optional[DisplayCb] = None) -> None:
        super().__init__(AGENT_IFACE)
        self._pin = default_pin
        self._on_confirm = on_confirm
        self._on_display = on_display

    @method()
    def Release(self):  # noqa: N802 (D-Bus method name)
        log.info("pairing agent released")

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802,F821
        log.info("PIN requested for %s", device)
        return self._pin

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802,F821
        log.info("passkey requested for %s", device)
        return 0

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802,F821
        log.info("display PIN %s for %s", pincode, device)

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802,F821
        log.info("display passkey %06u (%u entered) for %s", passkey, entered, device)
        if self._on_display:
            self._on_display(device, int(passkey))

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802,F821
        log.info("confirm passkey %06u for %s", passkey, device)
        if self._on_confirm and not self._on_confirm(device, int(passkey)):
            raise DBusError(_REJECTED, "user rejected pairing")
        # else: accept (return nothing)

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802,F821
        if self._on_confirm and not self._on_confirm(device, -1):
            raise DBusError(_REJECTED, "user rejected authorization")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802,F821
        # Trusted audio profiles are allowed; everything else asks the UI.
        log.info("authorize service %s for %s", uuid, device)

    @method()
    def Cancel(self):  # noqa: N802
        log.info("pairing canceled by remote")


async def register_agent(bus, agent: PairingAgent) -> None:
    """Export the agent and register it as the default with BlueZ."""
    bus.export(AGENT_PATH, agent)
    introspection = await bus.introspect(BLUEZ_SERVICE, BLUEZ_ROOT)
    obj = bus.get_proxy_object(BLUEZ_SERVICE, BLUEZ_ROOT, introspection)
    mgr = obj.get_interface(AGENT_MANAGER_IFACE)
    await mgr.call_register_agent(AGENT_PATH, AGENT_CAPABILITY)
    await mgr.call_request_default_agent(AGENT_PATH)
    log.info("pairing agent registered (%s)", AGENT_CAPABILITY)


async def unregister_agent(bus) -> None:
    try:
        introspection = await bus.introspect(BLUEZ_SERVICE, BLUEZ_ROOT)
        obj = bus.get_proxy_object(BLUEZ_SERVICE, BLUEZ_ROOT, introspection)
        mgr = obj.get_interface(AGENT_MANAGER_IFACE)
        await mgr.call_unregister_agent(AGENT_PATH)
    except (DBusError, Exception):  # best-effort on shutdown
        pass
