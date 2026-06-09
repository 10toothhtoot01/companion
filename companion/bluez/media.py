"""AVRCP now-playing (org.bluez.MediaPlayer1) + A2DP transport volume helpers.

Codec *detection* is done from the live PipeWire node (audio.pwcli.resolve_codec), which
gives a human codec name (LDAC/AAC/...). BlueZ's MediaTransport1.Codec is only a numeric
A2DP id and can't distinguish vendor codecs, so we don't rely on it for the codec label.

What we DO read from BlueZ:
  - MediaPlayer1: Track {Title, Artist, Album}, Status  -> now-playing strip
  - MediaTransport1: Volume (0-127) -> fallback when wpctl isn't usable

Parsing of a player props dict is pure and unit-friendly (parse_player_props).
"""
from __future__ import annotations

import logging
from typing import Optional

from .constants import (
    BLUEZ_SERVICE,
    MEDIA_PLAYER_IFACE,
    OBJECT_MANAGER_IFACE,
    PROPERTIES_IFACE,
)
from companion.core.models import NowPlaying

log = logging.getLogger(__name__)


def _v(value):
    # Duck-typed Variant unwrap so the pure parsers don't require dbus_fast at import
    # time (a dbus_fast.Variant exposes both .signature and .value).
    if hasattr(value, "signature") and hasattr(value, "value"):
        return value.value
    return value


def parse_player_props(props: dict) -> NowPlaying:
    """Turn a MediaPlayer1 property dict into a NowPlaying (pure, testable)."""
    props = {k: _v(v) for k, v in (props or {}).items()}
    track = props.get("Track") or {}
    track = {k: _v(v) for k, v in track.items()}
    return NowPlaying(
        title=track.get("Title") or None,
        artist=track.get("Artist") or None,
        status=(props.get("Status") or None),
    )


async def find_player_path(bus, device_path: str) -> Optional[str]:
    """Find the MediaPlayer1 object that belongs to a device (player path is a child)."""
    introspection = await bus.introspect(BLUEZ_SERVICE, "/")
    root = bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
    om = root.get_interface(OBJECT_MANAGER_IFACE)
    objects = await om.call_get_managed_objects()
    for path, interfaces in objects.items():
        if MEDIA_PLAYER_IFACE in interfaces and path.startswith(device_path):
            return path
    return None


async def read_now_playing(bus, device_path: str) -> Optional[NowPlaying]:
    player_path = await find_player_path(bus, device_path)
    if not player_path:
        return None
    introspection = await bus.introspect(BLUEZ_SERVICE, player_path)
    obj = bus.get_proxy_object(BLUEZ_SERVICE, player_path, introspection)
    props_iface = obj.get_interface(PROPERTIES_IFACE)
    all_props = await props_iface.call_get_all(MEDIA_PLAYER_IFACE)
    return parse_player_props(all_props)


def transport_volume_to_fraction(volume_0_127: Optional[int]) -> Optional[float]:
    """Map MediaTransport1.Volume (0-127) to 0.0..1.0."""
    if volume_0_127 is None:
        return None
    return max(0.0, min(1.0, int(volume_0_127) / 127.0))
