"""Deterministic capability + adapter classification (framework-free, unit-tested).

This is the single source of truth for two FDA decisions:
  1. Which adapter STATE we are in (absent / blocked / off / ready), so the UI can
     surface exactly one honest banner instead of silently assuming an adapter exists.
  2. Which VERBS are enabled for a given device snapshot, so verb gating is centralized
     and identical everywhere (detail panel, tray, more-panel) rather than ad-hoc.

No Qt, no D-Bus here on purpose: it is pure data in -> data out, so every branch is
testable without hardware.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from companion.core.models import Device, DeviceKind


class AdapterState(str, Enum):
    ABSENT = "absent"               # no org.bluez.Adapter1 at all
    HARD_BLOCKED = "hard_blocked"   # rfkill physical switch
    SOFT_BLOCKED = "soft_blocked"   # rfkill soft block
    POWERED_OFF = "powered_off"     # Adapter1.Powered = false
    READY = "ready"


# Precedence matters: a hard block hides power state, absence hides everything.
def classify_adapter(present: bool, powered: bool,
                      rfkill_soft: bool = False,
                      rfkill_hard: bool = False) -> AdapterState:
    if not present:
        return AdapterState.ABSENT
    if rfkill_hard:
        return AdapterState.HARD_BLOCKED
    if rfkill_soft:
        return AdapterState.SOFT_BLOCKED
    if not powered:
        return AdapterState.POWERED_OFF
    return AdapterState.READY


_ADAPTER_MESSAGE = {
    AdapterState.ABSENT:
        "No Bluetooth adapter found. Plug one in or check that the driver loaded.",
    AdapterState.HARD_BLOCKED:
        "Bluetooth is hardware-blocked. Use the physical switch or Fn key to enable it.",
    AdapterState.SOFT_BLOCKED:
        "Bluetooth is blocked. Unblock it with:  rfkill unblock bluetooth",
    AdapterState.POWERED_OFF:
        "Bluetooth is off. Turn it on to scan and connect.",
    AdapterState.READY: "",
}


def adapter_message(state: AdapterState) -> str:
    return _ADAPTER_MESSAGE.get(state, "")


def adapter_actionable(state: AdapterState) -> bool:
    """True when the app can fix the state itself (power on / rfkill unblock)."""
    return state in (AdapterState.POWERED_OFF, AdapterState.SOFT_BLOCKED)


@dataclass(frozen=True)
class Verbs:
    """Which actions are valid for a device RIGHT NOW. Drives enabled/disabled UI."""
    connect: bool
    sound: bool
    share: bool
    transport: bool
    volume: bool
    mic: bool
    more: bool


def enabled_verbs(device: Device) -> Verbs:
    """Pure verb gating from a device snapshot.

    - connect/more are always offered (pair, forget, info make sense in any state).
    - sound/share/volume need a CONNECTED audio-out device.
    - transport (AVRCP) needs a connected device that actually exposes a media player.
    """
    if getattr(device, "is_local", False):
        # System output (headphone jack, HDMI, USB): no Bluetooth verbs, but it can take
        # a volume and join a Share/combine group with Bluetooth devices.
        return Verbs(connect=False, sound=False, share=True, transport=False,
                     volume=True, mic=False, more=False)
    connected = bool(device.connected)
    audio_out = bool(device.is_audio_out)
    return Verbs(
        connect=True,
        sound=connected and audio_out,
        share=connected and audio_out,
        transport=connected and bool(device.has_media_player),
        volume=connected and audio_out,
        mic=connected and bool(device.has_mic),
        more=True,
    )


def mic_toggle_profile(device: Device) -> Optional[str]:
    """The card profile to switch to when the MIC verb is tapped (one-tap toggle).

    - In music / A2DP mode (mic off) -> return the device's headset (HFP/HSP) profile to
      turn the mic ON. Returns None when the device exposes no headset profile, i.e. its
      mic simply can't be used on this system.
    - In call / headset mode (mic on) -> return the REMEMBERED A2DP music profile so
      turning the mic OFF restores the exact prior codec (e.g. a2dp-sink-ldac), never a
      generic default. Falls back to plain "a2dp-sink" only if nothing was remembered.
    """
    if device.mic_active:
        return device.music_profile or "a2dp-sink"
    return device.headset_profile


def clean_alias(text: Optional[str]) -> Optional[str]:
    """Normalize a user-entered device name for the BlueZ Alias. Returns the trimmed
    text, or None if it's blank — so callers can reject an empty rename instead of
    wiping the device's name."""
    if not text:
        return None
    cleaned = text.strip()
    return cleaned or None


def primary_label(device: Device) -> str:
    """Deterministic label for the primary button across the device lifecycle."""
    if getattr(device, "is_local", False):
        return "SYSTEM OUTPUT"
    if device.connected:
        return "DISCONNECT"
    if device.paired:
        return "CONNECT"
    return "PAIR & CONNECT"


def kind_noun(kind: DeviceKind) -> str:
    return {
        DeviceKind.HEADPHONES: "Headphones",
        DeviceKind.EARBUDS: "Earbuds",
        DeviceKind.SPEAKER: "Speaker",
        DeviceKind.INPUT: "Input device",
        DeviceKind.PHONE: "Phone",
        DeviceKind.UNKNOWN: "Device",
    }.get(kind, "Device")
