"""Device detail: the three-verb surface (Connect · Sound · Share) + mono truth strip.

The merge-and-minimize centerpiece: one big battery dot-ring hero, three verbs, a quiet
readout (codec / volume / now-playing), and everything advanced behind MORE.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from companion.core.models import Device
from companion.core import capabilities
from companion.ui import glyphs, theme


class _BatteryHero(QWidget):
    """Large centered battery dot-ring with the percentage in the middle (Mo4)."""
    def __init__(self) -> None:
        super().__init__()
        self._device: Device | None = None
        self.setMinimumSize(220, 220)

    def set_device(self, device: Device) -> None:
        self._device = device
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        c = QPointF(self.width() / 2, self.height() / 2)
        pct = self._device.battery if self._device else None
        glyphs.draw_battery_ring(p, c, min(self.width(), self.height()) * 0.36,
                                 pct, dots=24, dot_r=4.0)
        p.setPen(glyphs.INK)
        p.setFont(theme.tracked_font(34, spacing_px=1.0, bold=True))
        label = f"{pct}" if pct is not None else "··"
        p.drawText(self.rect(), Qt.AlignCenter, label)
        p.end()


class _TruthRow(QWidget):
    def __init__(self, key: str, value: str) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        k = QLabel(key)
        k.setProperty("role", "eyebrow")
        k.setFont(theme.tracked_font(8.5, spacing_px=3.0))
        v = QLabel(value)
        v.setProperty("role", "truth")
        v.setAlignment(Qt.AlignRight)
        row.addWidget(k)
        row.addStretch(1)
        row.addWidget(v)
        self._v = v

    def set_value(self, value: str) -> None:
        self._v.setText(value)


class DeviceDetail(QWidget):
    connect_requested = Signal(str)
    disconnect_requested = Signal(str)
    sound_requested = Signal(str)
    mic_requested = Signal(str)
    volume_changed = Signal(str, float)   # (device_path, 0.0..1.0)
    more_requested = Signal(str)
    transport_requested = Signal(str, str)   # (device_path, verb)
    mute_toggled = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._device: Device | None = None
        self._suppress_volume = False

        self._hero = _BatteryHero()
        self._title = QLabel("—")
        self._title.setAlignment(Qt.AlignCenter)
        self._title.setFont(theme.tracked_font(20, spacing_px=1.5, bold=True))
        # Long names wrap within the detail column instead of forcing the window wider.
        self._title.setWordWrap(True)
        self._title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._subtitle = QLabel("")
        self._subtitle.setProperty("role", "muted")
        self._subtitle.setAlignment(Qt.AlignCenter)
        self._subtitle.setFont(theme.tracked_font(8.5, spacing_px=3.0))
        self._now = QLabel("")
        self._now.setProperty("accent", "true")
        self._now.setAlignment(Qt.AlignCenter)

        # AVRCP transport — visible only when the remote exposes a media player.
        self._prev = QPushButton("◁◁")
        self._playpause = QPushButton("▷")
        self._next = QPushButton("▷▷")
        for _b in (self._prev, self._playpause, self._next):
            _b.setProperty("verb", "ghost")
            _b.setFont(theme.tracked_font(13, spacing_px=2.0, bold=True))
        self._prev.clicked.connect(lambda: self._transport("previous"))
        self._playpause.clicked.connect(self._toggle_playpause)
        self._next.clicked.connect(lambda: self._transport("next"))
        self._transport_row = QWidget()
        _trow = QHBoxLayout(self._transport_row)
        _trow.setContentsMargins(0, 0, 0, 0)
        _trow.addStretch(1)
        _trow.addWidget(self._prev)
        _trow.addWidget(self._playpause)
        _trow.addWidget(self._next)
        _trow.addStretch(1)
        self._transport_row.setVisible(False)

        self._primary = QPushButton("CONNECT")
        self._primary.setProperty("verb", "primary")
        self._primary.setFont(theme.tracked_font(11, spacing_px=4.0, bold=True))
        self._primary.clicked.connect(self._toggle_connect)

        self._sound = QPushButton("SOUND")
        self._sound.setProperty("verb", "secondary")
        self._sound.setFont(theme.tracked_font(10, spacing_px=3.0))
        self._sound.clicked.connect(lambda: self._emit(self.sound_requested))
        self._mic = QPushButton("MIC")
        self._mic.setProperty("verb", "secondary")
        self._mic.setFont(theme.tracked_font(10, spacing_px=3.0))
        self._mic.clicked.connect(lambda: self._emit(self.mic_requested))
        verbs = QHBoxLayout()
        verbs.addWidget(self._sound)
        verbs.addWidget(self._mic)

        self._codec_row = _TruthRow("CODEC", "—")
        self._profile_row = _TruthRow("PROFILE", "—")

        vol_label = QLabel("VOLUME")
        vol_label.setProperty("role", "eyebrow")
        vol_label.setFont(theme.tracked_font(8.5, spacing_px=3.0))
        self._volume = QSlider(Qt.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.valueChanged.connect(self._on_volume)
        self._mute = QPushButton("MUTE")
        self._mute.setProperty("verb", "ghost")
        self._mute.setFont(theme.tracked_font(9, spacing_px=2.0))
        self._mute.clicked.connect(self._emit_mute)
        self._vol_row = QHBoxLayout()
        self._vol_row.addWidget(self._volume, 1)
        self._vol_row.addWidget(self._mute)

        self._more = QPushButton("···  MORE")
        self._more.setProperty("verb", "ghost")
        self._more.clicked.connect(lambda: self._emit(self.more_requested))

        root = QVBoxLayout(self)
        root.addWidget(self._hero, alignment=Qt.AlignCenter)
        root.addWidget(self._title)
        root.addWidget(self._subtitle)
        root.addWidget(self._now)
        root.addWidget(self._transport_row)
        root.addSpacing(12)
        root.addWidget(self._primary)
        root.addLayout(verbs)
        root.addSpacing(12)
        root.addWidget(self._codec_row)
        root.addWidget(self._profile_row)
        root.addSpacing(8)
        root.addWidget(vol_label)
        root.addLayout(self._vol_row)
        root.addStretch(1)
        root.addWidget(self._more, alignment=Qt.AlignCenter)

    def set_volume_value(self, path: str, fraction: float) -> None:
        """Reflect an external volume change (e.g. from the Share tab) WITHOUT emitting,
        so the per-device slider stays in lock-step with the group slider."""
        if self._device is None or self._device.path != path:
            return
        self._device.volume = fraction
        self._suppress_volume = True
        self._volume.setValue(int(round(fraction * 100)))
        self._suppress_volume = False

    def set_device(self, device: Device) -> None:
        self._device = device
        self._hero.set_device(device)
        self._title.setText(device.name)
        bits = [device.kind.value.upper()]
        bits.append("CONNECTED" if device.connected else
                    ("PAIRED" if device.paired else "NEW"))
        self._subtitle.setText("  ·  ".join(bits))
        self._primary.setText(capabilities.primary_label(device))
        self._codec_row.set_value(device.codec.label if device.codec else "—")
        self._profile_row.set_value("remembered ✓" if device.trusted else "—")
        self._now.setText(device.now_playing.label if device.now_playing else "")
        if device.volume is not None:
            self._suppress_volume = True
            self._volume.setValue(int(round(device.volume * 100)))
            self._suppress_volume = False
        verbs = capabilities.enabled_verbs(device)
        self._volume.setEnabled(verbs.volume)
        self._sound.setEnabled(verbs.sound)
        self._mic.setEnabled(verbs.mic)
        self._mic.setText("MIC ●" if device.mic_active else "MIC")
        self._more.setEnabled(verbs.more)
        # A local system output (headphone jack/HDMI) can't be connected/disconnected.
        self._primary.setEnabled(not getattr(device, "is_local", False))
        self._mute.setEnabled(verbs.volume)
        self._mute.setText("UNMUTE" if device.muted else "MUTE")
        self._transport_row.setVisible(verbs.transport)
        playing = bool(device.now_playing and device.now_playing.status == "playing")
        self._playpause.setText("❚❚" if playing else "▷")

    def clear(self) -> None:
        """Reset to an empty placeholder when no device is selected (e.g. the
        selected device was removed/forgotten). Avoids showing stale state."""
        self._device = None
        self._hero.set_device(None)
        self._title.setText("—")
        self._subtitle.setText("NO DEVICE SELECTED")
        self._now.setText("")
        self._codec_row.set_value("—")
        self._profile_row.set_value("—")
        self._transport_row.setVisible(False)
        for button in (self._primary, self._sound, self._mic, self._mute):
            button.setEnabled(False)
        self._suppress_volume = True
        self._volume.setValue(0)
        self._volume.setEnabled(False)
        self._suppress_volume = False

    def _toggle_connect(self) -> None:
        if not self._device:
            return
        sig = self.disconnect_requested if self._device.connected else self.connect_requested
        sig.emit(self._device.path)

    def _emit(self, signal) -> None:
        if self._device:
            signal.emit(self._device.path)

    def _on_volume(self, value: int) -> None:
        if self._device and not self._suppress_volume:
            self.volume_changed.emit(self._device.path, value / 100.0)

    def _transport(self, verb: str) -> None:
        if self._device:
            self.transport_requested.emit(self._device.path, verb)

    def _toggle_playpause(self) -> None:
        if not self._device:
            return
        playing = bool(self._device.now_playing and
                       self._device.now_playing.status == "playing")
        self.transport_requested.emit(self._device.path,
                                      "pause" if playing else "play")

    def _emit_mute(self) -> None:
        if self._device:
            self.mute_toggled.emit(self._device.path)
