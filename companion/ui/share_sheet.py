"""Share sheet: multi-select sinks + per-member volume balance (the dual-output headline).

Friction-minimizing: picking targets is a checklist of devices you already own; each row
carries its own volume so you can balance a loud speaker against quiet earbuds; one
inverted button confirms "PLAYING ON N". Under the hood the app resolves each device's
REAL PipeWire node name (never guessed) and drives a live combine sink via
companion.audio.pwcli.

Note on latency: module-combine-sink has no reliable per-member delay knob, so we do NOT
show a latency slider that would silently do nothing. PipeWire's combine.latency-compensate
keeps members aligned automatically; a genuine manual per-member offset would require a
loopback chain and is intentionally omitted rather than faked.

The ShareTarget carries the resolved `pipewire_node` when known (device.node_name);
otherwise it falls back to the conventional `bluez_output.<MAC>.a2dp-sink` shape, and the
app re-resolves it against pw-dump before loading the combine sink.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from companion.core.models import Device, ShareTarget
from companion.ui import theme


def fallback_node_name(device: Device) -> str:
    """Conventional A2DP sink node name; only used until pw-dump resolves the real one."""
    return device.node_name or f"bluez_output.{device.address_underscored}.a2dp-sink"


class ShareSheet(QWidget):
    play_requested = Signal(list)   # emits list[ShareTarget]
    stop_requested = Signal()
    member_volume_changed = Signal(str, float)  # (device_path, 0.0..1.0)

    def __init__(self) -> None:
        super().__init__()
        self._rows: dict[str, QCheckBox] = {}
        self._vol: dict[str, QSlider] = {}
        self._row_widgets: dict[str, QWidget] = {}
        self._devices: dict[str, Device] = {}
        self._active: set[str] = set()   # live combine membership (auto-synced by app)

        self._eyebrow = QLabel("SHARE · PLAY ON MULTIPLE")
        self._eyebrow.setProperty("role", "eyebrow")
        self._eyebrow.setFont(theme.tracked_font(8.5, spacing_px=3.0))

        self._list = QVBoxLayout()

        self._play = QPushButton("PLAY ON SELECTED")
        self._play.setProperty("verb", "primary")
        self._play.setFont(theme.tracked_font(11, spacing_px=4.0, bold=True))
        self._play.clicked.connect(self._on_play)

        self._hint = QLabel(
            "Audio plays on every checked device at once. Use each slider to balance "
            "volume across the group -- handy when one device is louder than another.")
        self._hint.setProperty("role", "muted")
        self._hint.setWordWrap(True)

        root = QVBoxLayout(self)
        root.addWidget(self._eyebrow)
        root.addLayout(self._list)
        root.addSpacing(8)
        root.addWidget(self._hint)
        root.addStretch(1)
        root.addWidget(self._play)

    def set_devices(self, devices: list[Device]) -> None:
        """Reconcile the member list against the live device set INCREMENTALLY.

        Rows are added / removed / updated in place rather than torn down and rebuilt, so
        a background refresh (or a newly detected device or output) updates the mixer live
        without flicker and without yanking a slider out from under the user's finger.
        """
        incoming = {d.path: d for d in devices if d.is_audio_out}
        for path in list(self._rows.keys()):
            if path not in incoming:
                self._remove_row(path)
        for d in incoming.values():
            if d.path in self._rows:
                self._update_row(d)
            else:
                self._add_row(d)
        self._devices = incoming
        self._relabel()

    def _add_row(self, d: Device) -> None:
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(8)
        cb = QCheckBox(d.name)
        cb.setEnabled(d.connected)
        cb.setChecked(d.path in self._active if self._active else d.connected)
        cb.setFont(theme.tracked_font(10.5, spacing_px=1.0))
        # A long name must not stretch the share column either.
        cb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        vol = QSlider(Qt.Horizontal)
        vol.setRange(0, 100)
        vol.setValue(int(round((d.volume if d.volume is not None else 1.0) * 100)))
        vol.setFixedWidth(96)
        vol.setEnabled(d.connected)
        vol.setToolTip("Balance this device's volume within the group")
        vol.valueChanged.connect(
            lambda v, p=d.path: self.member_volume_changed.emit(p, v / 100.0))
        self._rows[d.path] = cb
        self._vol[d.path] = vol
        self._row_widgets[d.path] = row
        row_lay.addWidget(cb, 1)
        row_lay.addWidget(vol)
        self._list.addWidget(row)

    def _update_row(self, d: Device) -> None:
        cb = self._rows[d.path]
        if cb.text() != d.name:
            cb.setText(d.name)
        cb.setEnabled(d.connected)
        cb.blockSignals(True)
        cb.setChecked(d.path in self._active if self._active else d.connected)
        cb.blockSignals(False)
        vol = self._vol[d.path]
        vol.setEnabled(d.connected)
        # Don't fight a live drag; only reflect an external value when the user is idle.
        if not vol.isSliderDown() and d.volume is not None:
            target = int(round(d.volume * 100))
            if vol.value() != target:
                vol.blockSignals(True)
                vol.setValue(target)
                vol.blockSignals(False)

    def _remove_row(self, path: str) -> None:
        self._rows.pop(path, None)
        self._vol.pop(path, None)
        widget = self._row_widgets.pop(path, None)
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        self._devices.pop(path, None)

    def set_active_members(self, paths) -> None:
        """Reflect the live combine membership the app reconciled (group changed)."""
        self._active = set(paths)
        for path, cb in self._rows.items():
            cb.blockSignals(True)
            if self._active:
                cb.setChecked(path in self._active)
            else:
                d = self._devices.get(path)
                cb.setChecked(bool(d and d.connected))
            cb.blockSignals(False)
        self._relabel()

    def set_member_volume(self, path: str, fraction: float) -> None:
        """Reflect a volume set elsewhere (the per-device slider) onto this member's
        slider, without re-emitting -- keeps the group + device sliders concurrent."""
        slider = self._vol.get(path)
        if slider is None:
            return
        slider.blockSignals(True)
        slider.setValue(int(round(fraction * 100)))
        slider.blockSignals(False)
        d = self._devices.get(path)
        if d is not None:
            d.volume = fraction

    def _relabel(self) -> None:
        n = len(self._active)
        self._play.setText(f"PLAYING ON {n}" if n else "PLAY ON SELECTED")

    def _on_play(self) -> None:
        targets: list[ShareTarget] = []
        first = True
        for path, cb in self._rows.items():
            if not cb.isChecked():
                continue
            d = self._devices[path]
            targets.append(ShareTarget(
                device_path=path,
                name=d.name,
                pipewire_node=fallback_node_name(d),
                latency_offset_ms=0,
                is_primary=first,
            ))
            first = False
        if targets:
            self._play.setText(f"PLAYING ON {len(targets)}")
            self.play_requested.emit(targets)
        else:
            self._play.setText("PLAY ON SELECTED")
            self.stop_requested.emit()
