"""AVRCP now-playing + transport control via org.bluez.MediaPlayer1.

When the local machine is the A2DP SINK (headphones/speaker role) BlueZ exposes the
remote's media player as a child object of the device path, implementing
`org.bluez.MediaPlayer1` with Play/Pause/Next/Previous/Stop methods and `Status` +
`Track` (a metadata dict) + `Position` properties. This is the modern AVRCP surface;
`org.bluez.MediaControl1` is deprecated and intentionally unused.

The wire/parse layer (`parse_player_metadata`, `passthrough_method`) is pure and
unit-tested; the thin `BluezMediaPlayer` runner does the actual D-Bus calls.
"""
from __future__ import annotations

import logging
from typing import Optional

from .constants import BLUEZ_SERVICE, MEDIA_PLAYER_IFACE, PROPERTIES_IFACE
from companion.core.models import NowPlaying

log = logging.getLogger(__name__)

# AVRCP passthrough verbs we expose -> MediaPlayer1 method stems (dbus_fast prefixes
# them with call_, e.g. call_play). Only standard AVRCP 1.x operations.
_VERB_TO_METHOD = {
    "play": "play",
    "pause": "pause",
    "stop": "stop",
    "next": "next",
    "previous": "previous",
    "prev": "previous",
    "fast_forward": "fast_forward",
    "rewind": "rewind",
}

_PLAYING = {"playing", "forward-seek", "reverse-seek"}


def passthrough_method(verb: str) -> Optional[str]:
    """Map a UI verb to a MediaPlayer1 method stem, or None if unsupported."""
    return _VERB_TO_METHOD.get(verb.lower())


def _unwrap(value):
    """Unwrap a dbus_fast Variant (has `.value`) or return the plain value."""
    return getattr(value, "value", value)


def parse_player_metadata(props: dict) -> Optional[NowPlaying]:
    """Build a NowPlaying from a MediaPlayer1 property dict (Variants or plain).

    Accepts either the full props map (with a `Track` sub-dict) or a flattened map.
    Returns None only when there is genuinely no usable info.
    """
    if not props:
        return None
    status = _unwrap(props.get("Status"))
    track = _unwrap(props.get("Track")) or {}
    if isinstance(track, dict):
        title = _unwrap(track.get("Title"))
        artist = _unwrap(track.get("Artist"))
        album = _unwrap(track.get("Album"))
    else:
        title = artist = album = None
    title = (title or None) if isinstance(title, str) else None
    artist = (artist or None) if isinstance(artist, str) else None
    norm_status = status.lower() if isinstance(status, str) else None
    if not any((title, artist, norm_status)):
        return None
    return NowPlaying(title=title, artist=artist, status=norm_status)


def is_playing(now: Optional[NowPlaying]) -> bool:
    return bool(now and now.status in _PLAYING)


class BluezMediaPlayer:
    """Thin async wrapper over a single MediaPlayer1 object (best-effort)."""

    def __init__(self, bus, player_path: str) -> None:
        self._bus = bus
        self.path = player_path
        self._iface = None
        self._props = None

    async def _ensure(self):
        if self._iface is None:
            introspection = await self._bus.introspect(BLUEZ_SERVICE, self.path)
            obj = self._bus.get_proxy_object(BLUEZ_SERVICE, self.path, introspection)
            self._iface = obj.get_interface(MEDIA_PLAYER_IFACE)
            self._props = obj.get_interface(PROPERTIES_IFACE)
        return self._iface

    async def control(self, verb: str) -> bool:
        """Send a transport passthrough. Returns False if unsupported/failed."""
        method = passthrough_method(verb)
        if method is None:
            return False
        try:
            iface = await self._ensure()
            await getattr(iface, f"call_{method}")()
            return True
        except Exception:
            log.debug("MediaPlayer1.%s failed on %s", method, self.path)
            return False

    async def now_playing(self) -> Optional[NowPlaying]:
        try:
            iface = await self._ensure()
            props = await self._props.call_get_all(MEDIA_PLAYER_IFACE)
            return parse_player_metadata(props)
        except Exception:
            log.debug("MediaPlayer1 GetAll failed on %s", self.path)
            return None
