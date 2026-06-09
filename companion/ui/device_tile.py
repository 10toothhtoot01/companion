"""A single device row/tile in the left list.

Merges battery, codec and signal into one calm glance (the merge-and-minimize goal):
one tile answers "which device, how charged, what quality, how strong" with zero chrome.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from companion.core.models import Device
from companion.ui import glyphs, theme


class _TileGlyph(QWidget):
    """Battery ring + a small signal dot-row underneath."""
    def __init__(self, device: Device) -> None:
        super().__init__()
        self._device = device
        self.setFixedSize(96, 96)

    def set_device(self, device: Device) -> None:
        self._device = device
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        c = QPointF(self.width() / 2, self.height() / 2 - 8)
        glyphs.draw_battery_ring(p, c, 30, self._device.battery, dots=12, dot_r=2.6)
        if self._device.battery is not None:
            p.setPen(glyphs.INK)
            p.setFont(theme.tracked_font(11, spacing_px=0.5, bold=True))
            p.drawText(int(c.x() - 20), int(c.y() - 8), 40, 16,
                       Qt.AlignCenter, str(self._device.battery))
        # Signal dots only when meaningful (during discovery RSSI is present).
        bars = glyphs.rssi_to_bars(self._device.rssi)
        glyphs.draw_signal(p, self.width() / 2 - 24, self.height() - 14, bars,
                           bars=5, gap=12.0, dot_r=2.4)
        p.end()


class DeviceTile(QFrame):
    clicked = Signal(str)  # emits device path

    def __init__(self, device: Device) -> None:
        super().__init__()
        self.setObjectName("DeviceTile")
        self._device = device
        self._glyph = _TileGlyph(device)

        self._name = QLabel(device.name)
        self._name.setFont(theme.tracked_font(12.5, spacing_px=1.0, bold=True))
        # A long device name must never widen the list column: wrap it, and let the
        # layout (not the text) own the width so an over-long name clips rather than
        # blowing the column/window out.
        self._name.setWordWrap(True)
        self._name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._meta = QLabel()
        self._meta.setProperty("role", "muted")
        self._codec = QLabel()
        self._codec.setProperty("accent", "true")

        text = QVBoxLayout()
        text.addWidget(self._name)
        text.addWidget(self._meta)
        text.addWidget(self._codec)

        row = QHBoxLayout(self)
        row.addWidget(self._glyph)
        row.addLayout(text, 1)
        self.refresh(device)

    @property
    def device(self) -> Device:
        return self._device

    def refresh(self, device: Device) -> None:
        self._device = device
        self._glyph.set_device(device)
        self._name.setText(device.name)
        kind = device.kind.value.upper()
        state = "CONNECTED" if device.connected else (
            "PAIRED" if device.paired else "NEW")
        self._meta.setText(f"{kind} · {state}")
        self._codec.setText(device.codec.label if device.codec else "")
        self.setProperty("selected", device.connected)
        self.setProperty("idle", not device.connected)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, _event) -> None:
        self.clicked.emit(self._device.path)
