"""System tray widget — the 3-second path, now fully wired.

Qt6/KDE notes baked in (TECH_NOTES):
  - Keep a Python reference to BOTH the QSystemTrayIcon and its QMenu or they get GC'd
    and the icon vanishes.
  - Probe QSystemTrayIcon.isSystemTrayAvailable() before showing; on KDE/Wayland Qt uses
    the StatusNotifierItem host automatically.

The tray exposes Qt signals so the app can wire Sound / Share / Swap / Open / Quit to the
same handlers as the main window (no dead actions).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from companion.core.models import Device
from companion.ui import glyphs


class CompanionTray(QSystemTrayIcon):
    sound_requested = Signal()
    share_requested = Signal()
    swap_requested = Signal()
    open_requested = Signal()
    quit_requested = Signal()
    output_selected = Signal(str)   # pactl sink NAME chosen from the Output submenu

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._menu = QMenu()
        self._primary: Optional[Device] = None
        self._build_menu()
        self.setToolTip("Companion")
        self.refresh(None, live=False)
        self.activated.connect(self._on_activated)

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def _build_menu(self) -> None:
        self._act_sound = QAction("Sound", self._menu)
        self._act_share = QAction("Share", self._menu)
        self._act_swap = QAction("Swap device", self._menu)
        self._act_open = QAction("Open Companion", self._menu)
        self._act_quit = QAction("Quit", self._menu)
        self._act_sound.triggered.connect(lambda: self.sound_requested.emit())
        self._act_share.triggered.connect(lambda: self.share_requested.emit())
        self._act_swap.triggered.connect(lambda: self.swap_requested.emit())
        self._act_open.triggered.connect(lambda: self.open_requested.emit())
        self._act_quit.triggered.connect(lambda: self.quit_requested.emit())
        # Quick output switcher: populated live by the app via set_outputs().
        self._output_menu = QMenu("Output", self._menu)
        self._output_menu.setEnabled(False)
        self._output_actions: list[QAction] = []
        for a in (self._act_sound, self._act_share, self._act_swap):
            self._menu.addAction(a)
        self._menu.addMenu(self._output_menu)
        self._menu.addSeparator()
        self._menu.addAction(self._act_open)
        self._menu.addAction(self._act_quit)
        self.setContextMenu(self._menu)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_requested.emit()

    def refresh(self, primary: Optional[Device], live: bool) -> None:
        self._primary = primary
        percent = primary.battery if primary else None
        self.setIcon(QIcon(glyphs.tray_pixmap(percent, live)))
        has = primary is not None
        self._act_sound.setEnabled(has)
        self._act_share.setEnabled(has)
        if primary:
            codec = primary.codec.label if primary.codec else "—"
            tip = f"{primary.name} · {codec}"
            if primary.battery is not None:
                tip += f" · {primary.battery}%"
            self.setToolTip(tip)
        else:
            self.setToolTip("Companion · no device")

    def set_outputs(self, outputs) -> None:
        """Populate the Output submenu. `outputs` is a list of (sink_name, label,
        is_default). Picking one emits output_selected(sink_name). Keep a reference to
        the QActions so Qt doesn't garbage-collect them out of the live menu."""
        self._output_menu.clear()
        self._output_actions = []
        if not outputs:
            self._output_menu.setEnabled(False)
            return
        self._output_menu.setEnabled(True)
        for sink_name, label, is_default in outputs:
            act = QAction(label, self._output_menu)
            act.setCheckable(True)
            act.setChecked(bool(is_default))
            act.triggered.connect(
                lambda _checked=False, n=sink_name: self.output_selected.emit(n))
            self._output_menu.addAction(act)
            self._output_actions.append(act)
