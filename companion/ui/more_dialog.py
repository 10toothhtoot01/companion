"""Advanced per-device panel (the "··· MORE" surface).

This exists so the MORE button is no longer a dead stub. It surfaces the lower-level,
less-frequent actions that don't deserve a top-level verb, plus an honest read-only
diagnostics dump (address / roles / live PipeWire node) so power users can see exactly
what the app detected — no hidden state.

Every control here maps to a real, wired action:
  - RECONNECT      -> BluezDevice.reconnect()        (disconnect then connect)
  - TRUST/UNTRUST  -> BluezDevice.set_trusted()      (auto-reconnect on availability)
  - FORGET         -> Adapter1.RemoveDevice()        (unpair; confirm-guarded)
  - AUTO-CONNECT   -> WirePlumber bluez5.auto-connect per-device rule
No button is inert.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
    QWidget,
)

from companion.core.models import Device
from companion.core import capabilities
from companion.ui import theme
from companion.ui.dialog_chrome import make_frameless


class _Row(QWidget):
    """A label : value diagnostics row in the Nothing dot-matrix style."""

    def __init__(self, label: str, value: str) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        key = QLabel(label.upper())
        key.setProperty("role", "eyebrow")
        key.setFont(theme.tracked_font(8.0, spacing_px=2.5))
        val = QLabel(value or "—")
        val.setProperty("role", "muted")
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        val.setWordWrap(True)
        lay.addWidget(key)
        lay.addStretch(1)
        lay.addWidget(val)


def _truth(value: bool) -> str:
    return "yes" if value else "no"


class MoreDialog(QDialog):
    """Advanced actions + diagnostics for a single device."""

    reconnect_requested = Signal(str)
    trust_toggled = Signal(str, bool)   # (path, desired_trusted)
    forget_requested = Signal(str)
    autoconnect_toggled = Signal(str, bool)  # (path, desired_enabled)
    rename_requested = Signal(str, str)      # (path, new_alias)

    def __init__(self, device: Device, auto_connect: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._path = device.path
        self._autoconnect_on = bool(auto_connect)
        self.setWindowTitle(f"{device.name} · Advanced")
        self.setMinimumWidth(360)

        root = QVBoxLayout(self)
        root.insertWidget(0, make_frameless(self, "ADVANCED"))

        title = QLabel(device.name)
        title.setFont(theme.tracked_font(14, spacing_px=1.0, bold=True))
        root.addWidget(title)
        eyebrow = QLabel(f"{capabilities.kind_noun(device.kind)}  ·  "
                         f"{'CONNECTED' if device.connected else 'OFFLINE'}")
        eyebrow.setProperty("role", "muted")
        eyebrow.setFont(theme.tracked_font(8.0, spacing_px=3.0))
        root.addWidget(eyebrow)
        root.addSpacing(10)

        # --- read-only diagnostics (exactly what we auto-detected) ---
        codec = device.codec.label if device.codec else "—"
        node = device.node_name or "—"
        node_id = str(device.node_id) if device.node_id is not None else "—"
        battery = f"{device.battery}%" if device.battery is not None else "—"
        rows = [
            ("Address", device.address),
            ("Paired", _truth(device.paired)),
            ("Trusted", _truth(device.trusted)),
            ("Connected", _truth(device.connected)),
            ("Audio out", _truth(device.is_audio_out)),
            ("Media player", _truth(device.has_media_player)),
            ("Battery", battery),
            ("Codec", codec),
            ("PipeWire node", node),
            ("Node id", node_id),
        ]
        for label, value in rows:
            root.addWidget(_Row(label, value))

        root.addSpacing(14)

        # --- rename (writes the BlueZ Alias; shows everywhere the instant BlueZ acks) ---
        rename_row = QHBoxLayout()
        self._alias = QLineEdit(device.name)
        self._alias.setMaxLength(64)
        self._alias.setPlaceholderText("Device name")
        self._alias.returnPressed.connect(self._on_rename)
        self._rename = QPushButton("RENAME")
        self._rename.setProperty("verb", "ghost")
        self._rename.setFont(theme.tracked_font(9, spacing_px=2.0))
        self._rename.clicked.connect(self._on_rename)
        rename_row.addWidget(self._alias, 1)
        rename_row.addWidget(self._rename)
        root.addLayout(rename_row)

        # --- actions (each is wired to a real device method) ---
        actions = QHBoxLayout()
        self._reconnect = QPushButton("RECONNECT")
        self._reconnect.setProperty("verb", "ghost")
        self._reconnect.setFont(theme.tracked_font(9, spacing_px=2.0))
        self._reconnect.setEnabled(device.connected)
        self._reconnect.clicked.connect(self._on_reconnect)

        self._trust = QPushButton("UNTRUST" if device.trusted else "TRUST")
        self._trust.setProperty("verb", "ghost")
        self._trust.setFont(theme.tracked_font(9, spacing_px=2.0))
        self._trust_desired = not device.trusted
        self._trust.clicked.connect(self._on_trust)

        self._autoconnect = QPushButton(
            "AUTO-CONNECT: ON" if self._autoconnect_on else "AUTO-CONNECT: OFF")
        self._autoconnect.setProperty("verb", "ghost")
        self._autoconnect.setFont(theme.tracked_font(9, spacing_px=2.0))
        self._autoconnect.clicked.connect(self._on_autoconnect)

        self._forget = QPushButton("FORGET")
        self._forget.setProperty("verb", "ghost")
        self._forget.setFont(theme.tracked_font(9, spacing_px=2.0))
        self._forget.clicked.connect(self._on_forget)

        actions.addWidget(self._reconnect)
        actions.addWidget(self._trust)
        actions.addStretch(1)
        root.addLayout(actions)
        actions2 = QHBoxLayout()
        actions2.addWidget(self._autoconnect)
        actions2.addWidget(self._forget)
        actions2.addStretch(1)
        root.addLayout(actions2)

        close = QPushButton("CLOSE")
        close.setProperty("verb", "primary")
        close.setFont(theme.tracked_font(10, spacing_px=3.0, bold=True))
        close.clicked.connect(self.accept)
        root.addSpacing(8)
        root.addWidget(close)

    def _on_reconnect(self) -> None:
        self.reconnect_requested.emit(self._path)
        self.accept()

    def _on_trust(self) -> None:
        self.trust_toggled.emit(self._path, self._trust_desired)
        self.accept()

    def _on_autoconnect(self) -> None:
        self.autoconnect_toggled.emit(self._path, not self._autoconnect_on)
        self.accept()

    def _on_rename(self) -> None:
        alias = capabilities.clean_alias(self._alias.text())
        if alias is None:
            QMessageBox.information(self, "Rename",
                                    "Enter a non-empty name for this device.")
            return
        self.rename_requested.emit(self._path, alias)
        self.accept()

    def _on_forget(self) -> None:
        reply = QMessageBox.question(
            self, "Forget device",
            "Remove this device? You'll need to pair it again to reconnect.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.forget_requested.emit(self._path)
            self.accept()
