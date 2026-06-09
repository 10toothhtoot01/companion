"""Frameless dialog chrome — a tiny draggable title bar + close button.

The main window is borderless (Nothing-style, no native OS title bar). The pop-up
dialogs (Sound / Mic / Advanced) used to keep the platform title bar, which looked
inconsistent. `make_frameless(dialog, title)` turns a QDialog borderless and returns a
`DialogChrome` bar the dialog inserts at the very top of its own layout:

  * the ✕ button closes the dialog via reject() (Cancel semantics), and
  * dragging anywhere on the bar moves the dialog, since a frameless window has no
    native move handle.

Kept deliberately tiny and Qt-only so it can be reused by every dialog without pulling
in app/business logic.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QWidget

from companion.ui import theme


class DialogChrome(QWidget):
    """Draggable header for a frameless dialog: title text + close (✕) button."""

    def __init__(self, dialog: QDialog, title: str) -> None:
        super().__init__()
        self.setObjectName("TitleBar")
        self.setFixedHeight(34)
        self._dialog = dialog
        self._drag_off: Optional[QPoint] = None

        name = QLabel(title.upper())
        name.setObjectName("TitleBarName")
        name.setFont(theme.tracked_font(9.0, spacing_px=4.0, bold=True))

        self._close = QPushButton("\u2715")
        self._close.setObjectName("WinBtnClose")
        self._close.setFixedSize(28, 24)
        self._close.setFont(theme.tracked_font(11, spacing_px=0.0, bold=True))
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.setFocusPolicy(Qt.NoFocus)
        self._close.clicked.connect(dialog.reject)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 8, 0)
        lay.setSpacing(6)
        lay.addWidget(name)
        lay.addStretch(1)
        lay.addWidget(self._close)

    # --- drag the borderless dialog by this bar ---
    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.LeftButton:
            self._drag_off = (e.globalPosition().toPoint()
                              - self._dialog.frameGeometry().topLeft())
            e.accept()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self._drag_off is not None and (e.buttons() & Qt.LeftButton):
            self._dialog.move(e.globalPosition().toPoint() - self._drag_off)
            e.accept()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        self._drag_off = None


def make_frameless(dialog: QDialog, title: str) -> DialogChrome:
    """Make `dialog` borderless and return a chrome bar to insert at its layout top."""
    dialog.setWindowFlag(Qt.FramelessWindowHint, True)
    return DialogChrome(dialog, title)
