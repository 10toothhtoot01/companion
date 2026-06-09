"""Runtime audio control via the pw / pactl / wpctl CLIs.

WHY CLI, not a static config file:
    A PipeWire `pipewire.conf.d` drop-in only takes effect on a PipeWire restart
    (documented on the ArchWiki). That is unusable for an instant "Play on 2". The
    live, no-restart path is PulseAudio's `module-combine-sink` loaded at runtime with
    `pactl load-module`, which returns a module index we can `unload-module` later.

DESIGN:
    `build_*` functions are pure (argv lists) and unit-tested without a server.
    `run_*`  functions actually execute, guarded by shutil.which, and are kept thin.

This module never blocks the Qt thread for long: callers run it via the executor /
qasync. All commands are argv lists (no shell), so device names can't inject anything.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Optional, Sequence

from companion.audio.parse import (
    bt_card_for_address,
    bt_sink_for_address,
    bt_source_for_address,
    local_output_sinks,
    output_sinks,
    parse_cards,
    parse_module_index,
    parse_pw_dump,
    parse_sink_input_ids,
    parse_wpctl_volume,
)
from companion.core.models import Codec, PwCard, PwNode, ShareTarget
from companion.audio.parse import codec_label

log = logging.getLogger(__name__)

COMBINE_SINK_NAME = "companion_combine"
_TIMEOUT = 8.0

# The default sink that was active before we created a combine group, so Share can
# restore it on teardown (the combine sink is removed on unload and must not be left
# as the dangling default). Only one combine group exists at a time (the app tears the
# previous one down first), so a single module-level value is sufficient.
_prev_default_sink: Optional[str] = None


class AudioToolMissing(RuntimeError):
    """Raised when a required CLI (pactl/pw-dump/wpctl) is not installed."""


# --------------------------------------------------------------------------- #
# Pure command builders (unit-tested)
# --------------------------------------------------------------------------- #
def build_pwdump_cmd() -> list[str]:
    return ["pw-dump"]


def build_load_combine_cmd(slaves: Sequence[str],
                           sink_name: str = COMBINE_SINK_NAME) -> list[str]:
    if not slaves:
        raise ValueError("combine sink needs at least one slave node")
    return [
        "pactl", "load-module", "module-combine-sink",
        f"sink_name={sink_name}",
        "slaves=" + ",".join(slaves),
        "channels=2",
    ]


def build_unload_cmd(module_index: int) -> list[str]:
    return ["pactl", "unload-module", str(module_index)]


def build_list_modules_cmd() -> list[str]:
    return ["pactl", "list", "short", "modules"]


def parse_combine_module_indices(text: str,
                                 sink_name: str = COMBINE_SINK_NAME) -> list[int]:
    """Indices of every loaded module-combine-sink whose args name OUR sink.

    `pactl list short modules` prints tab-separated rows like:
        42\tmodule-combine-sink\tsink_name=companion_combine slaves=...
    We match BOTH the module name and our sink_name so we never unload an unrelated
    combine sink the user created themselves. Pure (text in -> indices out).
    """
    indices: list[int] = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or "module-combine-sink" not in parts[1]:
            continue
        args = parts[2] if len(parts) > 2 else ""
        if f"sink_name={sink_name}" in args:
            try:
                indices.append(int(parts[0].strip()))
            except (ValueError, TypeError):
                continue
    return indices


def build_set_default_cmd(sink_name: str) -> list[str]:
    return ["pactl", "set-default-sink", sink_name]


def build_get_default_cmd() -> list[str]:
    return ["pactl", "get-default-sink"]


def build_list_sink_inputs_cmd() -> list[str]:
    return ["pactl", "-f", "json", "list", "sink-inputs"]


def build_move_input_cmd(input_id: int, sink_name: str) -> list[str]:
    return ["pactl", "move-sink-input", str(input_id), sink_name]


def build_get_volume_cmd(node_id: int) -> list[str]:
    return ["wpctl", "get-volume", str(node_id)]


def build_set_mute_cmd(node_id: int, state) -> list[str]:
    """Mute command. `state` may be True/False or the string "toggle"."""
    if state == "toggle":
        arg = "toggle"
    else:
        arg = "1" if state else "0"
    return ["wpctl", "set-mute", str(node_id), arg]


def parse_muted(text: str) -> bool:
    """wpctl get-volume prints '[MUTED]' suffix when the node is muted."""
    return "[MUTED]" in (text or "").upper()


def build_set_volume_cmd(node_id: int, fraction: float) -> list[str]:
    frac = max(0.0, min(1.0, fraction))
    return ["wpctl", "set-volume", str(node_id), f"{frac:.2f}"]


# --------------------------------------------------------------------------- #
# Thin runners
# --------------------------------------------------------------------------- #
def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    tool = cmd[0]
    if shutil.which(tool) is None:
        raise AudioToolMissing(tool)
    log.debug("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)


def dump_nodes() -> list[PwNode]:
    """Snapshot all PipeWire nodes (for node-name + codec discovery)."""
    proc = _run(build_pwdump_cmd())
    return parse_pw_dump(proc.stdout)


def resolve_sink(address: str, nodes: Optional[list[PwNode]] = None) -> Optional[PwNode]:
    """Discover the REAL bluez_output node for a device MAC. Never guesses a name."""
    nodes = nodes if nodes is not None else dump_nodes()
    return bt_sink_for_address(nodes, address)


def resolve_codec(address: str, nodes: Optional[list[PwNode]] = None) -> Optional[Codec]:
    """Read the live negotiated codec for a device from PipeWire."""
    node = resolve_sink(address, nodes)
    if node is None:
        return None
    mapped = codec_label(node.bluez_codec)
    if mapped is None:
        return None
    name, kbps = mapped
    return Codec(name=name, bitrate_kbps=kbps)


def resolve_source(address: str, nodes: Optional[list[PwNode]] = None) -> Optional[PwNode]:
    """Discover the REAL bluez_input (microphone) source node for a device MAC.

    Returns None unless the card is currently in a headset (HFP/HSP) profile — that's the
    only time the mic source exists in PipeWire.
    """
    nodes = nodes if nodes is not None else dump_nodes()
    return bt_source_for_address(nodes, address)


def card_for_address(address: str) -> Optional[PwCard]:
    """List PipeWire/PulseAudio cards and return the one backing a Bluetooth MAC."""
    try:
        cards = parse_cards(_run(build_list_cards_cmd()).stdout)
    except (AudioToolMissing, subprocess.SubprocessError):
        return None
    return bt_card_for_address(cards, address)


def set_card_profile(card_name: str, profile: str) -> bool:
    """Switch a card's active profile (e.g. music <-> headset/mic). True on success."""
    try:
        return _run(build_set_card_profile_cmd(card_name, profile)).returncode == 0
    except (AudioToolMissing, subprocess.SubprocessError):
        return False


def start_combine(targets: Sequence[ShareTarget]) -> Optional[int]:
    """Create a live combine sink fanning out to the targets and route audio to it.

    Returns the pactl module index (for teardown), or None if nothing was created.
    """
    global _prev_default_sink
    slaves = [t.pipewire_node for t in targets if t.pipewire_node]
    if not slaves:
        return None
    # Snapshot the sink-inputs that exist BEFORE loading the module. The combine
    # module creates its OWN streams feeding each slave (they also appear as
    # sink-inputs); moving those onto the combine sink would feed it into itself —
    # an audio feedback loop. So we only ever relocate streams that pre-dated the
    # combine sink. Likewise remember the previous default sink to restore on stop.
    try:
        pre_existing = parse_sink_input_ids(
            _run(build_list_sink_inputs_cmd()).stdout)
    except (AudioToolMissing, subprocess.SubprocessError):
        pre_existing = []
    try:
        prev_default = _run(build_get_default_cmd()).stdout.strip() or None
    except (AudioToolMissing, subprocess.SubprocessError):
        prev_default = None
    proc = _run(build_load_combine_cmd(slaves))
    module_index = parse_module_index(proc.stdout)
    if module_index is None:
        log.warning("combine sink load returned no module index: %r", proc.stdout)
        return None
    _prev_default_sink = prev_default
    # Make new audio go to the combine sink, and pull the PRE-EXISTING streams over
    # (never the combine module's own slave-feeding streams).
    _run(build_set_default_cmd(COMBINE_SINK_NAME))
    try:
        for sid in pre_existing:
            _run(build_move_input_cmd(sid, COMBINE_SINK_NAME))
    except (AudioToolMissing, subprocess.SubprocessError):
        log.debug("could not move existing sink-inputs; new audio still routes")
    return module_index


def stop_combine(module_index: Optional[int]) -> bool:
    """Tear down the combine sink created by start_combine() and restore the default."""
    global _prev_default_sink
    if module_index is None:
        return False
    ok = False
    try:
        _run(build_unload_cmd(module_index))
        ok = True
    except (AudioToolMissing, subprocess.SubprocessError):
        ok = False
    # Restore the prior default sink — the combine sink no longer exists, so leaving
    # it as the default would send audio nowhere.
    if _prev_default_sink:
        try:
            _run(build_set_default_cmd(_prev_default_sink))
        except (AudioToolMissing, subprocess.SubprocessError):
            log.debug("could not restore previous default sink")
    _prev_default_sink = None
    return ok


def stop_all_combines(sink_name: str = COMBINE_SINK_NAME) -> int:
    """Unload EVERY companion combine sink currently loaded; return how many.

    Used both at startup (sweep a sink left behind by a previous crash) and on quit, so
    the combine sink never lingers in KDE / pavucontrol / `pactl list` after the app is
    gone. Safe to call when none exist or when pactl is missing.
    """
    global _prev_default_sink
    try:
        listing = _run(build_list_modules_cmd()).stdout
    except (AudioToolMissing, subprocess.SubprocessError):
        return 0
    count = 0
    for idx in parse_combine_module_indices(listing, sink_name):
        try:
            _run(build_unload_cmd(idx))
            count += 1
        except (AudioToolMissing, subprocess.SubprocessError):
            continue
    if count and _prev_default_sink:
        try:
            _run(build_set_default_cmd(_prev_default_sink))
        except (AudioToolMissing, subprocess.SubprocessError):
            log.debug("could not restore previous default sink during sweep")
    _prev_default_sink = None
    return count


def list_local_outputs(nodes: Optional[list[PwNode]] = None) -> list[PwNode]:
    """Live system audio outputs (headphone jack, HDMI, USB) from the PipeWire graph."""
    if nodes is None:
        nodes = dump_nodes()
    return local_output_sinks(nodes)


def list_output_sinks(nodes: Optional[list[PwNode]] = None) -> list[PwNode]:
    """Every selectable output sink (Bluetooth + local) for the tray switcher."""
    if nodes is None:
        nodes = dump_nodes()
    return output_sinks(nodes)


def get_default_sink() -> Optional[str]:
    """The current default sink NAME (pactl), or None if it can't be read."""
    try:
        out = _run(build_get_default_cmd()).stdout.strip()
        return out or None
    except (AudioToolMissing, subprocess.SubprocessError):
        return None


def set_default_sink(sink_name: str) -> bool:
    """Make sink_name the system default output. Returns True on success."""
    try:
        _run(build_set_default_cmd(sink_name))
        return True
    except (AudioToolMissing, subprocess.SubprocessError):
        return False


def get_volume(node_id: int) -> Optional[float]:
    try:
        return parse_wpctl_volume(_run(build_get_volume_cmd(node_id)).stdout)
    except (AudioToolMissing, subprocess.SubprocessError):
        return None


def set_volume(node_id: int, fraction: float) -> bool:
    try:
        _run(build_set_volume_cmd(node_id, fraction))
        return True
    except (AudioToolMissing, subprocess.SubprocessError):
        return False


def set_mute(node_id: int, state) -> bool:
    """Mute/unmute/toggle a single PipeWire node. Per-device by node id."""
    try:
        _run(build_set_mute_cmd(node_id, state))
        return True
    except (AudioToolMissing, subprocess.SubprocessError):
        return False


def is_muted(node_id: int) -> Optional[bool]:
    try:
        return parse_muted(_run(build_get_volume_cmd(node_id)).stdout)
    except (AudioToolMissing, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------- #
# Live codec switching via PipeWire card profiles (no restart, no reconnect)
# --------------------------------------------------------------------------- #
def build_list_cards_cmd() -> list[str]:
    return ["pactl", "-f", "json", "list", "cards"]


def build_set_card_profile_cmd(card_name: str, profile: str) -> list[str]:
    return ["pactl", "set-card-profile", card_name, profile]


def switch_codec_profile(address: str, codec_key: str) -> Optional[str]:
    """Switch a Bluetooth device's codec LIVE via its PipeWire card profile.

    This re-negotiates the A2DP codec in place: no WirePlumber restart and no
    Bluetooth reconnect, so there's no audio drop on other outputs and the switch is
    near-instant. It is per-device by construction (each bluez_card is one device), so
    two devices sharing one combined output can run different codecs simultaneously.

    Returns the profile name applied (or the already-active matching profile) on
    success, or None when no matching per-codec profile is exposed by this PipeWire
    build so the caller can fall back to reload+reconnect. Raises AudioToolMissing if
    `pactl` is not installed.
    """
    from companion.audio import codecs
    cards = parse_cards(_run(build_list_cards_cmd()).stdout)
    card = bt_card_for_address(cards, address)
    if card is None:
        return None
    profile = codecs.profile_for_codec(codec_key, card.profiles)
    if profile is None:
        return None
    if card.active_profile == profile:
        return profile                       # already on the requested codec profile
    proc = _run(build_set_card_profile_cmd(card.name, profile))
    if proc.returncode != 0:
        log.warning("set-card-profile %s %s failed: %s",
                    card.name, profile, (proc.stderr or "").strip())
        return None
    return profile


def device_codec_profiles(address: str) -> tuple[list[str], list[str]]:
    """Return (device_profiles, system_profiles) of a2dp-sink-* profile names.

    `device_profiles` are the codec profiles negotiable on THIS device's link (the
    intersection of its advertised caps and the local encoders). `system_profiles` is
    the union across ALL cards, which lets the diagnosis tell apart "this device
    doesn't advertise the codec" from "the encoder isn't installed anywhere locally."
    Raises AudioToolMissing if pactl is absent.
    """
    cards = parse_cards(_run(build_list_cards_cmd()).stdout)
    card = bt_card_for_address(cards, address)
    device_profiles = list(card.profiles) if card else []
    system: list[str] = []
    for c in cards:
        for p in c.profiles:
            if p.lower().startswith("a2dp-sink-") and p not in system:
                system.append(p)
    return device_profiles, system
