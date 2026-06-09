"""Framework-free Share-group reconciliation (unit-tested without Qt/PipeWire).

The app keeps two notions of the Share group:
  * the DESIRED membership (the user's intent, which persists across a member
    disconnecting so it can auto-rejoin), and
  * the APPLIED membership (what the live combine sink is currently fanning to).

This helper decides, purely from those two sets, what the live combine sink should do.
"""
from __future__ import annotations

from typing import Iterable


def resolve_share_action(desired_member_paths: Iterable[str],
                         applied_member_paths: Iterable[str]) -> tuple[str, bool]:
    """Return (mode, changed).

    mode    -- "combine" when at least one member should be playing, else "off".
    changed -- True when desired membership differs from what's applied, i.e. the
               combine sink must be (re)built or torn down. Order-independent.
    """
    desired = set(desired_member_paths)
    applied = set(applied_member_paths)
    mode = "combine" if desired else "off"
    return mode, desired != applied
