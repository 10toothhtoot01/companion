"""Dot-grid glyph painters — the signature of the Nothing aesthetic.

Everything is drawn from dots, not icon fonts:
  - battery     : a ring of dots, filled proportionally to charge
  - signal      : a row of dots, filled by strength bucket
  - share_link  : two dots joined by a short accent bar

These are pure QPainter helpers so they can be reused by tiles, detail view, and the
tray icon (rendered into a QPixmap).
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap

INK = QColor("#ffffff")
DIM = QColor("#333333")
ACCENT = QColor("#d71921")
CANVAS = QColor("#000000")


def draw_battery_ring(p: QPainter, center: QPointF, radius: float,
                      percent: Optional[int], dots: int = 12,
                      dot_r: float = 3.0) -> None:
    """Draw a ring of `dots`; fill the fraction matching `percent`.

    A None percent renders a fully dim ring (unknown / not reporting).
    """
    filled = 0 if percent is None else round((percent / 100.0) * dots)
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    for i in range(dots):
        # Start at the top (12 o'clock), go clockwise.
        angle = (-math.pi / 2) + (2 * math.pi * i / dots)
        x = center.x() + radius * math.cos(angle)
        y = center.y() + radius * math.sin(angle)
        p.setBrush(INK if i < filled else DIM)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(x, y), dot_r, dot_r)
    p.restore()


def draw_signal(p: QPainter, x: float, y: float, strength: int,
                bars: int = 5, gap: float = 12.0, dot_r: float = 3.0) -> None:
    """Row of dots; `strength` is 0..bars filled."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    for i in range(bars):
        p.setBrush(INK if i < strength else DIM)
        p.drawEllipse(QPointF(x + i * gap, y), dot_r, dot_r)
    p.restore()


def rssi_to_bars(rssi: Optional[int], bars: int = 5) -> int:
    """Map RSSI dBm (~ -40 strong .. -100 weak) to a 0..bars bucket."""
    if rssi is None:
        return bars  # connected devices don't report RSSI; assume strong link.
    clamped = max(-100, min(-40, rssi))
    frac = (clamped + 100) / 60.0
    return max(0, min(bars, round(frac * bars)))


def tray_pixmap(percent: Optional[int], live: bool, size: int = 64) -> QPixmap:
    """Render the tray icon: a battery dot-ring, red center dot when audio is live."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    c = QPointF(size / 2, size / 2)
    draw_battery_ring(p, c, size * 0.34, percent, dots=12, dot_r=size * 0.045)
    if live:
        p.setBrush(ACCENT)
        p.setPen(Qt.NoPen)
        p.drawEllipse(c, size * 0.07, size * 0.07)
    p.end()
    return pm
