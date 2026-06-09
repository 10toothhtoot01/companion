"""BlueZ D-Bus names and well-known audio service UUIDs.

Grounded in the BlueZ D-Bus API (git.kernel.org/pub/scm/bluetooth/bluez.git, doc/*.rst)
and the BlueZ readthedocs mirror. See TECH_NOTES for the call->doc mapping.
"""

# Service / bus
BLUEZ_SERVICE = "org.bluez"
BLUEZ_ROOT = "/org/bluez"

# Interfaces
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
BATTERY_IFACE = "org.bluez.Battery1"
MEDIA_TRANSPORT_IFACE = "org.bluez.MediaTransport1"
MEDIA_CONTROL_IFACE = "org.bluez.MediaControl1"      # deprecated; not used
MEDIA_PLAYER_IFACE = "org.bluez.MediaPlayer1"        # AVRCP now-playing
MEDIA_ENDPOINT_IFACE = "org.bluez.MediaEndpoint1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
AGENT_IFACE = "org.bluez.Agent1"

# Standard freedesktop interfaces
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

# Our pairing agent registration.
AGENT_PATH = "/org/companion/agent"
# KeyboardDisplay lets us confirm passkeys for phones while auto-accepting headsets.
AGENT_CAPABILITY = "KeyboardDisplay"

# Common A2DP / HFP profile UUIDs (short form expanded to base UUID).
UUID_A2DP_SINK = "0000110b-0000-1000-8000-00805f9b34fb"     # AudioSink
UUID_A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"   # AudioSource
UUID_HFP_HF = "0000111e-0000-1000-8000-00805f9b34fb"        # Handsfree
UUID_HFP_AG = "0000111f-0000-1000-8000-00805f9b34fb"        # Handsfree AG
UUID_AVRCP = "0000110e-0000-1000-8000-00805f9b34fb"         # A/V Remote Control
UUID_HSP_HS = "00001108-0000-1000-8000-00805f9b34fb"        # Headset (HSP)
UUID_HSP_AG = "00001112-0000-1000-8000-00805f9b34fb"        # Headset AG (HSP)

AUDIO_OUTPUT_UUIDS = frozenset({UUID_A2DP_SINK, UUID_HFP_HF})
# A device carries a usable microphone when it advertises a hands-free / headset role
# (HFP-HF or HSP-HS). Those map to PipeWire's `headset-head-unit*` card profiles, which
# is exactly when a `bluez_input.*` source appears.
MIC_INPUT_UUIDS = frozenset({UUID_HFP_HF, UUID_HSP_HS})


def device_supports_audio_out(uuids) -> bool:
    """True if the device advertises an audio-sink-style profile we can route to."""
    return bool(AUDIO_OUTPUT_UUIDS.intersection(u.lower() for u in (uuids or [])))


def device_supports_mic(uuids) -> bool:
    """True if the device advertises a mic-bearing profile (HFP-HF / HSP-HS).

    This is the honest gate for offering microphone controls: only these profiles
    expose a `bluez_input.*` source once the card is in `headset-head-unit*` mode.
    """
    return bool(MIC_INPUT_UUIDS.intersection(u.lower() for u in (uuids or [])))
