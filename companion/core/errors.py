"""Framework-free classification of BlueZ / D-Bus errors into user-facing messages.

Kept out of the Qt layer so it can be unit-tested without PySide6 and reused by the
connect/pair/scan flows. Returns (user_message, is_transient):
  * an empty user_message means the condition is BENIGN (e.g. already connected, or
    not connected on disconnect) and the caller should treat it as success;
  * is_transient marks errors that are worth retrying after a short delay
    (the classic `br-connection-busy` / `InProgress` races on real adapters).

Matching is done on a lowercased blob of the D-Bus error name (`exc.type`, when present)
plus the human text, so it is robust to BlueZ's mix of typed and free-text errors.
"""
from __future__ import annotations


def friendly_bluez_error(exc: object) -> tuple[str, bool]:
    raw = f"{getattr(exc, 'type', '') or ''} {exc}".lower()

    # Benign: the desired end-state already holds.
    if any(s in raw for s in ("already connected", "alreadyexists", "already exists")):
        return ("", False)

    # Transient races worth a retry.
    if any(s in raw for s in ("busy", "in progress", "inprogress",
                              "le-connection-abort-by-local")):
        return ("The device is busy finishing another connection \u2014 retrying\u2026", True)

    # Pairing outcomes (terminal \u2014 the user must act on the device).
    if "authentication" in raw and ("cancel" in raw or "reject" in raw):
        return ("Pairing was cancelled or rejected on the device.", False)
    if "authentication" in raw and ("fail" in raw or "timeout" in raw):
        return ("Pairing failed \u2014 the PIN/passkey didn't match or timed out.", False)

    # No response \u2014 usually out of range or not in pairing mode.
    if any(s in raw for s in ("timeout", "timed out", "page-timeout")):
        return ("The device didn't respond. Make sure it's powered on, in range, "
                "and in pairing mode.", True)

    # The object went away between listing and acting.
    if any(s in raw for s in ("not available", "does not exist", "no such",
                              "unknownobject")):
        return ("That device is no longer available \u2014 try scanning again.", False)

    # Adapter isn't usable.
    if any(s in raw for s in ("not ready", "not powered", "blocked", "rfkill")):
        return ("The Bluetooth adapter isn't ready. Check that Bluetooth is on and "
                "not blocked (rfkill list).", False)

    # Profile/transport couldn't be brought up.
    if any(s in raw for s in ("profile unavailable", "connect failed", "connectfailed",
                              "protocol not available")):
        return ("Couldn't connect the device's audio profile. Toggle the device "
                "off/on and try again.", True)

    # Benign for a disconnect that's already down.
    if "not connected" in raw:
        return ("", False)

    return (f"Connection failed: {exc}", False)
