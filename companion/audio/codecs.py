"""Codec metadata and quality knobs.

The codec *list* mirrors WirePlumber's `bluez5.codecs` default set and the per-device
quality keys exposed via `monitor.bluez.rules` -> `update-props`. We never invent codecs;
this table only describes what BlueZ/WirePlumber can actually negotiate.

See TECH_NOTES (WirePlumber 0.5.x).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodecInfo:
    key: str                # WirePlumber codec token, e.g. "ldac"
    label: str              # UI label, e.g. "LDAC"
    lossless_capable: bool = False
    note: str = ""


# Order = our "Best -> Compatible" preference ramp surfaced behind the Sound verb.
KNOWN_CODECS: tuple[CodecInfo, ...] = (
    CodecInfo("ldac", "LDAC", note="Up to 990 kbps; quality auto|hq|sq|mq"),
    CodecInfo("aptx_hd", "aptX HD", note="24-bit"),
    CodecInfo("aptx", "aptX"),
    CodecInfo("aac", "AAC", note="bitratemode 0=cbr,1-5 vbr"),
    CodecInfo("sbc_xq", "SBC-XQ", note="Requires bluez5.enable-sbc-xq=true"),
    CodecInfo("sbc", "SBC", note="Universal fallback"),
)

# Valid values for the per-device quality keys (validated before we write config).
LDAC_QUALITY = ("auto", "hq", "sq", "mq")        # bluez5.a2dp.ldac.quality
AAC_BITRATE_MODE = (0, 1, 2, 3, 4, 5)            # bluez5.a2dp.aac.bitratemode

_BY_KEY = {c.key: c for c in KNOWN_CODECS}


def by_key(key: str) -> CodecInfo | None:
    return _BY_KEY.get(key.lower())


def all_keys() -> list[str]:
    return [c.key for c in KNOWN_CODECS]


def codec_family(value: str) -> str:
    """Normalize a codec token (e.g. "sbc_xq") or a live codec name (e.g. "SBC-XQ")
    to a comparable family. SBC-XQ negotiates as the SBC codec, so both map to "sbc"."""
    v = "".join(ch for ch in (value or "").lower() if ch.isalnum())
    if "ldac" in v:
        return "ldac"
    if "aptxhd" in v:
        return "aptxhd"
    if "aptx" in v:
        return "aptx"
    if "aac" in v:
        return "aac"
    if "sbc" in v:
        return "sbc"
    return v


def requested_codec_satisfied(requested_key: str, negotiated_name: str | None) -> bool:
    """True when the codec that actually negotiated matches what the user asked for
    (by family). Used to detect a fallback so the UI can tell the truth."""
    if not negotiated_name:
        return False
    return codec_family(requested_key) == codec_family(negotiated_name)


def profile_for_codec(codec_key: str, available_profiles) -> str | None:
    """Pick the PipeWire card profile that selects `codec_key`, from a card's list of
    available profiles.

    PipeWire names per-codec A2DP profiles `a2dp-sink-<token>` (e.g. `a2dp-sink-ldac`,
    `a2dp-sink-sbc_xq`). Selecting that profile re-negotiates the codec live. Returns
    None when this PipeWire build exposes no matching per-codec profile (older
    versions) so the caller can fall back to the reload+reconnect path.

    An exact token match (e.g. sbc_xq -> a2dp-sink-sbc_xq) is preferred over a mere
    same-family match (sbc_xq would otherwise also satisfy the sbc family).
    """
    key = (codec_key or "").lower()
    family = codec_family(key)
    exact: str | None = None
    family_match: str | None = None
    for prof in available_profiles or ():
        low = prof.lower()
        if not low.startswith("a2dp-sink-"):
            continue
        token = low.rsplit("-", 1)[-1]
        if token == key:
            exact = prof
        elif codec_family(token) == family and family_match is None:
            family_match = prof
    return exact or family_match


# --------------------------------------------------------------------------- #
# Headset / call (HFP/HSP) profiles — the microphone path
# --------------------------------------------------------------------------- #
# A Bluetooth audio device exposes TWO mutually-exclusive PipeWire card profiles:
#   * `a2dp-sink[-<codec>]`     — high-quality stereo OUTPUT, but NO microphone.
#   * `headset-head-unit[-msbc]`— HFP/HSP: the mic works, but audio is mono voice-grade
#     (CVSD ~8 kHz narrowband, or mSBC ~16 kHz "HD voice" wideband).
# Switching the card's ACTIVE profile flips between "music" and "call/mic" mode live,
# with no reconnect. This is the exact mechanism a Linux user needs to use a headset mic.
def is_headset_profile(profile) -> bool:
    return (profile or "").lower().startswith("headset-head-unit")


def is_a2dp_profile(profile) -> bool:
    return (profile or "").lower().startswith("a2dp-sink")


def headset_profiles(available) -> list:
    return [p for p in (available or ()) if is_headset_profile(p)]


def best_headset_profile(available):
    """Pick the mic profile, preferring mSBC (wideband HD voice) over CVSD narrowband."""
    heads = headset_profiles(available)
    if not heads:
        return None
    return next((p for p in heads if "msbc" in p.lower()), heads[0])


def best_a2dp_profile(available):
    """Pick a music (A2DP) profile to return to from call mode. Prefer the plain
    `a2dp-sink` so we don't force a specific codec; fall back to the first A2DP profile."""
    a2dps = [p for p in (available or ()) if is_a2dp_profile(p)]
    if not a2dps:
        return None
    return next((p for p in a2dps if p.lower() == "a2dp-sink"), a2dps[0])


def profile_mode(active_profile) -> str:
    """Classify a card's active profile into a user-facing mode: music | mic | off."""
    if is_headset_profile(active_profile):
        return "mic"
    if is_a2dp_profile(active_profile):
        return "music"
    return "off"


def available_codecs(profiles) -> list[str]:
    """The codec keys that can actually be negotiated, derived from a card's
    `a2dp-sink-<codec>` profiles. This is exactly the intersection PipeWire computed of
    (what the device advertises) AND (what the local build can encode) — so it's the
    honest set to offer the user.
    """
    return [c.key for c in KNOWN_CODECS
            if profile_for_codec(c.key, profiles) is not None]


# Why a requested codec can't be used, and how to get it. The user is on Fedora, which
# ships WITHOUT the proprietary aptX encoder (patents) — it comes from RPM Fusion — and
# whose default AAC encoder is limited; LDAC encoding needs libldac.
_ENCODER_HINT = {
    "ldac": ("LDAC encoding needs the 'libldac' encoder. On Fedora: "
             "`sudo dnf install libldac`, then `systemctl --user restart wireplumber`."),
    "aptx": ("aptX encoding needs libfreeaptx (patent-encumbered, not in Fedora "
             "proper). Enable RPM Fusion, then `sudo dnf install pipewire-codec-aptx`."),
    "aptx_hd": ("aptX HD encoding needs libfreeaptx (patent-encumbered, not in Fedora "
                "proper). Enable RPM Fusion, then "
                "`sudo dnf install pipewire-codec-aptx`."),
    "aac": ("High-quality AAC encoding wants Fraunhofer FDK AAC. On Fedora: "
            "`sudo dnf install fdk-aac-free` (or RPM Fusion 'fdk-aac' for full "
            "quality), then restart PipeWire."),
}


def diagnose_fallback(requested_key: str, negotiated_name: str | None,
                      device_profiles, system_profiles=None) -> str:
    """Explain technically why `requested_key` fell back to `negotiated_name`.

    Returns "" when there is no fallback. `device_profiles` are the a2dp-sink profiles
    on THIS device's card (what this link can negotiate); `system_profiles` is the
    union across all cards, used to distinguish "the local encoder is missing entirely"
    from "this particular device just doesn't advertise it."
    """
    if requested_codec_satisfied(requested_key, negotiated_name):
        return ""
    info = by_key(requested_key)
    want = info.label if info else (requested_key or "").upper()
    got = negotiated_name or "nothing"
    on_device = profile_for_codec(requested_key, device_profiles) is not None
    if on_device:
        # The codec IS offered on this link, yet the transport still chose something
        # else: that is a runtime re-negotiation, almost always the headset/HFP path.
        return (
            f"{want} is offered on this link, but the transport negotiated {got}. "
            "That almost always means the device switched to the hands-free/headset "
            "(HFP/HSP) profile — e.g. an app opened its microphone — or the RF link "
            "degraded and BlueZ dropped to SBC mid-stream. Close anything using the "
            "mic, keep the device in range, and reselect the codec.")
    hint = _ENCODER_HINT.get((requested_key or "").lower(), "")
    on_system = (profile_for_codec(requested_key, system_profiles) is not None
                 if system_profiles is not None else None)
    if on_system is True:
        return (
            f"Your system CAN encode {want} (another card exposes it), but THIS device "
            f"doesn't advertise {want} in its A2DP capabilities, so {got} is the best "
            "codec both ends share. That's a limitation of the device/its firmware — "
            "no host-side setting can add a codec the headphones don't implement.")
    if on_system is False:
        return (
            f"{want} is not negotiable because your PipeWire build can't encode it, so "
            f"only {got} was offered. {hint}").strip()
    return (
        f"{want} could not be negotiated; {got} was used instead. Either this device "
        f"doesn't implement {want} or its encoder isn't installed locally. {hint}"
    ).strip()
