"""Atomic, reversible config IO.

Every write Companion makes to PipeWire/WirePlumber drop-in dirs goes through here so
that:
  - writes are atomic (temp file + os.replace on the same filesystem)
  - the previous version is backed up (<file>.companion.bak) for one-click rollback
  - we only ever touch our own drop-in files (prefixed `90-companion-`)

This is the robustness contract from TECH_NOTES: no half-written configs, always
reversible, never clobber the user's hand-written files.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

PREFIX = "90-companion-"

PIPEWIRE_DROPIN = Path.home() / ".config" / "pipewire" / "pipewire.conf.d"
WIREPLUMBER_DROPIN = Path.home() / ".config" / "wireplumber" / "wireplumber.conf.d"


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_suffix(target.suffix + ".companion.bak")
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    # Write to a temp file in the SAME directory so os.replace is atomic.
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".companion-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_dropin(base_dir: Path, name: str, text: str) -> Path:
    """Write a Companion-owned drop-in. `name` is the unprefixed file name."""
    target = base_dir / f"{PREFIX}{name}"
    _atomic_write(target, text)
    return target


def rollback(base_dir: Path, name: str) -> bool:
    """Restore the most recent backup for a Companion drop-in. Returns True if restored."""
    target = base_dir / f"{PREFIX}{name}"
    backup = target.with_suffix(target.suffix + ".companion.bak")
    if backup.exists():
        os.replace(backup, target)
        return True
    if target.exists():
        target.unlink()
        return True
    return False
