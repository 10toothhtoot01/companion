"""Parsers for `pw-dump` / `pactl -f json` output.

These turn the audio server's JSON into the small dataclasses the rest of the app uses.
Kept pure (string/JSON in, dataclass out) so they are fully unit-testable without a
running PipeWire.

Why this exists: the single most common reason hand-rolled combine setups fail is using
the wrong sink node name. Bluetooth sinks are named with a profile suffix, e.g.
`bluez_output.AA_BB_CC_DD_EE_FF.a2dp-sink`. We discover the real name from pw-dump keyed
by the device's MAC (`api.bluez5.address`) instead of guessing it.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

from companion.core.models import PwCard, PwNode

# Map PipeWire's bluez codec tokens to friendly names + nominal bitrates.
_CODEC_LABEL = {
    "ldac": ("LDAC", 990),
    "aptx_hd": ("aptX HD", 576),
    "aptx": ("aptX", 352),
    "aac": ("AAC", 320),
    "sbc_xq": ("SBC-XQ", 452),
    "sbc": ("SBC", 328),
    "opus_05": ("Opus", None),
}


def parse_pw_dump(text: str) -> list[PwNode]:
    """Parse `pw-dump` JSON into PwNode entries (only PipeWire nodes)."""
    try:
        objects = json.loads(text)
    except (ValueError, TypeError):
        return []
    nodes: list[PwNode] = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        info = obj.get("info") or {}
        props = info.get("props") or {}
        name = props.get("node.name")
        if not name:
            continue
        nodes.append(PwNode(
            id=int(obj.get("id", props.get("object.id", -1))),
            name=name,
            media_class=props.get("media.class", ""),
            bluez_address=_norm_addr(props.get("api.bluez5.address")),
            bluez_codec=props.get("api.bluez5.codec"),
            description=props.get("node.description"),
        ))
    return nodes


def _norm_addr(addr: Optional[str]) -> Optional[str]:
    return addr.upper() if isinstance(addr, str) else None


def bt_sink_for_address(nodes: Iterable[PwNode], address: str) -> Optional[PwNode]:
    """Find the audio-sink node belonging to a Bluetooth MAC address."""
    want = address.upper()
    for n in nodes:
        if n.is_audio_sink and n.bluez_address == want:
            return n
    return None


def bt_source_for_address(nodes: Iterable[PwNode], address: str) -> Optional[PwNode]:
    """Find the audio-SOURCE (microphone) node for a Bluetooth MAC.

    The `bluez_input.*` source only exists while the card is in a headset (HFP/HSP)
    profile, so this returns None in A2DP/music mode — which is exactly how the caller
    knows the mic is currently unavailable.
    """
    want = address.upper()
    for n in nodes:
        if n.is_audio_source and n.bluez_address == want:
            return n
    return None


# Node-name prefixes that identify real Bluetooth audio endpoints. Anything else that is
# an Audio/Sink is a LOCAL output (built-in headphone jack, HDMI, USB DAC, etc).
_BT_NODE_PREFIXES = ("bluez_output.", "bluez_input.")
# Our own virtual combine sink — never present it as a selectable output device.
_COMBINE_SINK_NAME = "companion_combine"


def is_local_output(node: PwNode) -> bool:
    """True for a non-Bluetooth audio OUTPUT sink (built-in jack, HDMI, USB DAC).

    These are the system's own outputs (e.g. `alsa_output.pci-..._analog-stereo`). They
    are ordinary PipeWire sinks, so they can join a combine group exactly like a BT sink
    — which is what lets the app play to the headphone jack and Bluetooth devices at
    once. The combine sink we create is itself an Audio/Sink, so it is filtered out here.
    """
    if not node.is_audio_sink:
        return False
    if node.name.startswith(_BT_NODE_PREFIXES):
        return False
    if node.name == _COMBINE_SINK_NAME:
        return False
    return True


def local_output_sinks(nodes: Iterable[PwNode]) -> list[PwNode]:
    """All non-Bluetooth output sinks, preserving pw-dump order (jack, HDMI, USB...)."""
    return [n for n in nodes if is_local_output(n)]


def output_sinks(nodes: Iterable[PwNode]) -> list[PwNode]:
    """Every selectable output sink — Bluetooth AND local — minus our own combine sink.

    Used to populate the tray's quick output switcher. Sources, streams and the virtual
    combine sink are excluded; pw-dump order is preserved.
    """
    return [n for n in nodes
            if n.is_audio_sink and n.name != _COMBINE_SINK_NAME]


def parse_cards(pactl_json: str) -> list[PwCard]:
    """Parse `pactl -f json list cards` into PwCard entries.

    Profiles marked unavailable (e.g. a codec the remote can't do) are dropped so the
    caller never tries to select a profile the device would reject.
    """
    try:
        data = json.loads(pactl_json)
    except (ValueError, TypeError):
        return []
    cards: list[PwCard] = []
    for item in data if isinstance(data, list) else []:
        name = item.get("name")
        if not name:
            continue
        props = item.get("properties") or {}
        profiles_obj = item.get("profiles") or {}
        available: list[str] = []
        if isinstance(profiles_obj, dict):
            for pname, pinfo in profiles_obj.items():
                if isinstance(pinfo, dict) and pinfo.get("available") is False:
                    continue
                available.append(pname)
        cards.append(PwCard(
            name=name,
            bluez_address=_norm_addr(props.get("api.bluez5.address")),
            active_profile=item.get("active_profile"),
            profiles=tuple(available),
        ))
    return cards


def bt_card_for_address(cards: Iterable[PwCard], address: str) -> Optional[PwCard]:
    """Find the PipeWire card belonging to a Bluetooth MAC address."""
    want = address.upper()
    for c in cards:
        if c.bluez_address == want:
            return c
    return None


def codec_label(token: Optional[str]) -> Optional[tuple[str, Optional[int]]]:
    """Map a bluez codec token (e.g. 'ldac') to (label, nominal_kbps)."""
    if not token:
        return None
    return _CODEC_LABEL.get(token.lower(), (token.upper(), None))


def parse_module_index(load_module_output: str) -> Optional[int]:
    """`pactl load-module` prints the new module index on stdout. Parse it."""
    line = (load_module_output or "").strip().splitlines()
    if not line:
        return None
    token = line[-1].strip()
    return int(token) if token.isdigit() else None


def parse_sink_input_ids(pactl_json: str) -> list[int]:
    """From `pactl -f json list sink-inputs`, return the active sink-input ids."""
    try:
        data = json.loads(pactl_json)
    except (ValueError, TypeError):
        return []
    ids: list[int] = []
    for item in data if isinstance(data, list) else []:
        idx = item.get("index")
        if isinstance(idx, int):
            ids.append(idx)
    return ids


def parse_wpctl_volume(text: str) -> Optional[float]:
    """`wpctl get-volume <id>` prints e.g. 'Volume: 0.65' (optionally ' [MUTED]')."""
    for part in (text or "").replace("\n", " ").split():
        try:
            v = float(part)
        except ValueError:
            continue
        return max(0.0, min(1.0, v))
    return None
