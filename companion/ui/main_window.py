"""Main window: three columns — Devices | Device | Share — mirroring the mockup."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizeGrip, QVBoxLayout, QWidget,
)

from companion.core.models import Device
from companion.ui import theme
from companion.ui.device_detail import DeviceDetail
from companion.ui.device_tile import DeviceTile
from companion.ui.share_sheet import ShareSheet


class _AdapterBanner(QWidget):
    """A single honest status strip shown only when the adapter isn't Ready.

    Hidden entirely when Bluetooth is Ready so the normal UI stays uncluttered.
    """
    action = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("AdapterBanner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setFont(theme.tracked_font(9.5, spacing_px=1.5))
        self._btn = QPushButton("")
        self._btn.setProperty("verb", "primary")
        self._btn.setFont(theme.tracked_font(9, spacing_px=2.5, bold=True))
        self._btn.clicked.connect(self.action)
        lay.addWidget(self._label, 1)
        lay.addWidget(self._btn)
        self.hide()

    def show_state(self, message: str, actionable: bool, action_label: str) -> None:
        if not message:
            self.hide()
            return
        self._label.setText(message)
        self._btn.setText(action_label)
        self._btn.setVisible(bool(actionable and action_label))
        self.show()


class _TitleBar(QWidget):
    """Frameless-window chrome: drag to move, minimize, and close-to-tray.

    The window is borderless (Nothing-style), so this bar is the ONLY move/min/close
    affordance. The close button hides the window to the tray rather than quitting --
    a real Quit lives in the tray menu -- so background playback/sharing keeps running.
    """
    minimize_clicked = Signal()
    close_clicked = Signal()

    def __init__(self, window: QWidget) -> None:
        super().__init__()
        self.setObjectName("TitleBar")
        self.setFixedHeight(40)
        self._win = window
        self._drag_off: Optional[QPoint] = None

        name = QLabel("COMPANION")
        name.setObjectName("TitleBarName")
        name.setFont(theme.tracked_font(9.5, spacing_px=4.0, bold=True))

        self._min = QPushButton("\u2013")          # en dash = minimize
        self._min.setObjectName("WinBtn")
        self._close = QPushButton("\u2715")         # multiplication x = close
        self._close.setObjectName("WinBtnClose")
        for b in (self._min, self._close):
            b.setFixedSize(30, 26)
            b.setFont(theme.tracked_font(12, spacing_px=0.0, bold=True))
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
        self._min.clicked.connect(self.minimize_clicked)
        self._close.clicked.connect(self.close_clicked)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 10, 0)
        lay.setSpacing(6)
        lay.addWidget(name)
        lay.addStretch(1)
        lay.addWidget(self._min)
        lay.addWidget(self._close)

    # --- drag the borderless window by this bar ---
    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.LeftButton:
            self._drag_off = (e.globalPosition().toPoint()
                              - self._win.frameGeometry().topLeft())
            e.accept()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self._drag_off is not None and (e.buttons() & Qt.LeftButton):
            self._win.move(e.globalPosition().toPoint() - self._drag_off)
            e.accept()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        self._drag_off = None

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()


class MainWindow(QWidget):
    scan_requested = Signal()
    adapter_action_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle("Companion")
        self.setWindowFlags(Qt.FramelessWindowHint)   # headerless: our own chrome
        self.resize(1180, 760)
        self._tiles: dict[str, DeviceTile] = {}
        self._selected: Optional[str] = None
        self._minimize_to_tray = False                 # set True by the app if a tray exists

        eyebrow = QLabel("DEVICES")
        eyebrow.setProperty("role", "eyebrow")
        eyebrow.setFont(theme.tracked_font(8.5, spacing_px=3.0))
        self._list_box = QVBoxLayout()
        self._scan = QPushButton("+  SCAN FOR DEVICES")
        self._scan.setProperty("verb", "ghost")
        self._scan.setFont(theme.tracked_font(10, spacing_px=3.0))
        self._scan.clicked.connect(self.scan_requested)
        left = QVBoxLayout()
        left.addWidget(eyebrow)
        left.addLayout(self._list_box)
        left.addStretch(1)
        left.addWidget(self._scan)
        left_w = QWidget()
        left_w.setLayout(left)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left_w)

        self.detail = DeviceDetail()
        self.share = ShareSheet()

        self._banner = _AdapterBanner()
        self._banner.action.connect(self.adapter_action_requested)

        self._titlebar = _TitleBar(self)
        self._titlebar.minimize_clicked.connect(self.showMinimized)
        self._titlebar.close_clicked.connect(self.close)

        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.addWidget(scroll, 5)
        columns.addWidget(self.detail, 4)
        columns.addWidget(self.share, 4)

        # Frameless windows lose the native resize border, so add an explicit grip.
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 4, 4)
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._titlebar)
        root.addWidget(self._banner)
        root.addLayout(columns, 1)
        root.addLayout(grip_row)

    # ---- list management -------------------------------------------------
    def upsert_device(self, device: Device) -> None:
        tile = self._tiles.get(device.path)
        if tile is None:
            tile = DeviceTile(device)
            tile.clicked.connect(self._on_tile_clicked)
            self._tiles[device.path] = tile
            self._list_box.addWidget(tile)
        else:
            tile.refresh(device)
        # Keep the open detail view in sync with live changes.
        if self._selected == device.path:
            self.detail.set_device(device)
        self._refresh_share()

    def remove_device(self, path: str) -> None:
        tile = self._tiles.pop(path, None)
        if tile:
            tile.deleteLater()
        if self._selected == path:
            self._selected = None
            self.detail.clear()           # don't leave a stale device on screen
        self._refresh_share()             # keep the share checklist accurate

    def device_for(self, path: str) -> Optional[Device]:
        tile = self._tiles.get(path)
        return tile.device if tile else None

    def selected_path(self) -> Optional[str]:
        return self._selected

    def set_adapter_state(self, message: str, actionable: bool,
                          action_label: str = "") -> None:
        """Surface the classified adapter state as one banner (empty == hidden)."""
        self._banner.show_state(message, actionable, action_label)

    def set_minimize_to_tray(self, enabled: bool) -> None:
        """When True, closing the window hides it to the tray instead of quitting."""
        self._minimize_to_tray = enabled

    def closeEvent(self, event) -> None:
        # The headerless close button (and the WM close) hide to the tray so playback,
        # sharing, and auto-reconnect keep running in the background. Real quit is the
        # tray's Quit action. With no tray available, close really closes.
        if self._minimize_to_tray:
            event.ignore()
            self.hide()
        else:
            event.accept()

    def _refresh_share(self) -> None:
        self.share.set_devices([t.device for t in self._tiles.values()])

    def reflect_volume(self, path: str, fraction: float) -> None:
        """Mirror a volume change onto BOTH the per-device slider and the Share member
        slider so the two stay synced concurrently (setters block signals == no loop)."""
        self.detail.set_volume_value(path, fraction)
        self.share.set_member_volume(path, fraction)

    def _on_tile_clicked(self, path: str) -> None:
        tile = self._tiles.get(path)
        if tile:
            self._selected = path
            self.detail.set_device(tile.device)
