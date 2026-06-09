"""Plain data models shared between the D-Bus/audio layers and the UI.

Framework-free (no Qt, no D-Bus) so they are trivial to test and reason about. The UI
maps them to widgets; the BlueZ + audio layers produce them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DeviceKind(str, Enum):
    HEADPHONES = "headphones"
    EARBUDS = "earbuds"
    SPEAKER = "speaker"
    INPUT = "input"          # mouse / keyboard / controller
    PHONE = "phone"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Codec:
    """Resolved active codec, derived from the live PipeWire node — never guessed."""
    name: str                       # e.g. "LDAC", "AAC", "SBC-XQ"
    sample_rate_hz: Optional[int] = None
    bit_depth: Optional[int] = None
    bitrate_kbps: Optional[int] = None

    @property
    def label(self) -> str:
        bits = [self.name]
        if self.bitrate_kbps:
            bits.append(f"{self.bitrate_kbps}K")
        elif self.sample_rate_hz:
            bits.append(f"{self.sample_rate_hz // 1000} kHz")
        return " · ".join(bits)


@dataclass(frozen=True)
class NowPlaying:
    """AVRCP track metadata, if the remote exposes a media player."""
    title: Optional[str] = None
    artist: Optional[str] = None
    status: Optional[str] = None     # playing | paused | stopped

    @property
    def label(self) -> str:
        if self.title and self.artist:
            return f"{self.title} — {self.artist}"
        return self.title or self.artist or ""


@dataclass
class Device:
    """A snapshot of one Bluetooth device. `path` is the stable D-Bus object path."""
    path: str                       # /org/bluez/hci0/dev_XX_XX_XX_XX_XX_XX
    address: str                    # XX:XX:XX:XX:XX:XX
    name: str
    kind: DeviceKind = DeviceKind.UNKNOWN
    paired: bool = False
    connected: bool = False
    trusted: bool = False
    # Battery1.Percentage (0-100) if exposed, else None. Never fabricate.
    battery: Optional[int] = None
    # Signal strength. RSSI is only present during discovery; None when connected.
    rssi: Optional[int] = None
    # Active codec, resolved from the live PipeWire node (audio.codecs.parse).
    codec: Optional[Codec] = None
    # Volume 0.0..1.0 from wpctl / MediaTransport1, else None.
    volume: Optional[float] = None
    now_playing: Optional[NowPlaying] = None
    # True when the remote exposes an AVRCP MediaPlayer1 (enables transport controls).
    has_media_player: bool = False
    # Per-device output mute (PipeWire node mute), None when unknown.
    muted: Optional[bool] = None
    uuids: list[str] = field(default_factory=list)
    icon: Optional[str] = None      # freedesktop icon name hint from BlueZ
    # Live PipeWire identity, discovered (never guessed) from pw-dump.
    node_name: Optional[str] = None     # e.g. bluez_output.AA_BB_..._.a2dp-sink
    node_id: Optional[int] = None       # PipeWire global id for wpctl
    # --- microphone (HFP/HSP) state ---
    # True when the device advertises a mic-bearing profile (HFP-HF / HSP-HS).
    has_mic: bool = False
    # The PipeWire/PulseAudio card backing this device (bluez_card.AA_BB_..).
    card_name: Optional[str] = None
    active_profile: Optional[str] = None
    # The A2DP (music) profile to return to when leaving call/mic mode, remembered so the
    # exact prior codec profile is restored rather than guessed.
    music_profile: Optional[str] = None
    # The headset (HFP/HSP) profile that turns the mic on for this device.
    headset_profile: Optional[str] = None
    # True when the card is in headset mode now (mic live, voice-quality audio).
    mic_active: bool = False
    mic_node_id: Optional[int] = None    # PipeWire source node id (only present in mic mode)
    mic_volume: Optional[float] = None   # 0.0..1.0, else None
    mic_muted: Optional[bool] = None
    # --- local (non-Bluetooth) output, e.g. the built-in headphone jack / HDMI ---
    # When True this "device" is a system audio sink, NOT a BlueZ device: it is always
    # connected, exposes no Bluetooth verbs (connect/pair/codec/mic), but CAN take a
    # volume and join a Share/combine group so audio plays on the jack and Bluetooth at
    # once. `path` is then `pw:<node_name>` and `node_name`/`node_id` are pre-resolved.
    is_local: bool = False

    @property
    def is_audio_out(self) -> bool:
        if self.is_local:
            return True
        from companion.bluez.constants import device_supports_audio_out
        return device_supports_audio_out(self.uuids)

    @property
    def address_underscored(self) -> str:
        return self.address.replace(":", "_").upper()


@dataclass(frozen=True)
class ShareTarget:
    """One sink participating in a play-on-multiple group."""
    device_path: str
    name: str
    pipewire_node: str              # the REAL bluez_output node name (discovered)
    latency_offset_ms: int = 0
    is_primary: bool = False


@dataclass(frozen=True)
class PwNode:
    """A PipeWire node as parsed from pw-dump."""
    id: int
    name: str
    media_class: str
    bluez_address: Optional[str] = None
    bluez_codec: Optional[str] = None
    description: Optional[str] = None

    @property
    def is_audio_sink(self) -> bool:
        return self.media_class.startswith("Audio/Sink")

    @property
    def is_audio_source(self) -> bool:
        """A capture node (e.g. a Bluetooth headset mic, `bluez_input.*`)."""
        return self.media_class.startswith("Audio/Source")


@dataclass(frozen=True)
class PwCard:
    """A PipeWire/PulseAudio card as parsed from `pactl list cards`.

    For Bluetooth the card name is `bluez_card.AA_BB_..` and its profile list includes
    a per-codec entry like `a2dp-sink-ldac`. Switching the ACTIVE profile re-negotiates
    the A2DP codec live — no service restart, no Bluetooth reconnect — and is scoped to
    this one device, so other outputs keep their own codec.
    """
    name: str
    bluez_address: Optional[str] = None
    active_profile: Optional[str] = None
    profiles: tuple[str, ...] = ()
