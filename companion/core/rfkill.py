"""Read Linux rfkill state for Bluetooth (soft / hard block) from sysfs.

The pure reducer `blocked_from_entries` is separated from the sysfs I/O so it is
fully unit-testable without `/sys`. The reader scans `/sys/class/rfkill/*` for entries
whose `type` is ``bluetooth`` and reads the sibling ``soft`` / ``hard`` 0|1 files.

These files are world-readable, so no root is required. Nothing here raises: an
unreadable or absent sysfs simply yields ``(False, False)`` (treated as not blocked),
and the adapter classifier then falls back to the BlueZ Powered state.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

_RFKILL_ROOT = "/sys/class/rfkill"


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    try:
        return int(str(value).strip()) != 0
    except (TypeError, ValueError):
        return False


def blocked_from_entries(entries: Iterable[dict]) -> tuple[bool, bool]:
    """Pure reducer over rfkill entries.

    Each entry is a mapping with ``type`` / ``soft`` / ``hard``. Only ``bluetooth``
    entries are considered. A block counts if ANY bluetooth radio reports it, so a
    laptop with several radios is handled deterministically.

    Returns ``(soft_blocked, hard_blocked)``.
    """
    soft = False
    hard = False
    for entry in entries:
        if str(entry.get("type", "")).strip().lower() != "bluetooth":
            continue
        if _truthy(entry.get("soft")):
            soft = True
        if _truthy(entry.get("hard")):
            hard = True
    return soft, hard


def read_entries(root: str = _RFKILL_ROOT) -> list[dict]:
    """Best-effort read of every rfkill entry from sysfs. Never raises."""
    out: list[dict] = []
    try:
        base = Path(root)
        if not base.is_dir():
            return out
        for entry in sorted(base.iterdir()):
            try:
                rtype = (entry / "type").read_text().strip()
                soft = (entry / "soft").read_text().strip()
                hard = (entry / "hard").read_text().strip()
            except OSError:
                continue
            out.append({"name": entry.name, "type": rtype,
                        "soft": soft, "hard": hard})
    except OSError:
        log.debug("rfkill sysfs unavailable", exc_info=True)
    return out


def bluetooth_blocked(root: str = _RFKILL_ROOT) -> tuple[bool, bool]:
    """Live ``(soft_blocked, hard_blocked)`` for Bluetooth, read from sysfs."""
    return blocked_from_entries(read_entries(root))
