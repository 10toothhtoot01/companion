"""Sound dialog — the single surface behind the SOUND verb.

Merge-and-minimize: rather than a settings sprawl, one dialog picks the codec (best ->
compatible ramp) and the one quality knob that matters for the chosen codec. Confirming
writes the WirePlumber policy (which makes the choice stick across reconnects) and the
app reconnects the device once so it applies immediately.

Returns a SoundChoice the app maps to a global codec allow-list + a per-device rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QLabel, QRadioButton,
    QVBoxLayout,
)

from companion.audio import codecs
from companion.ui import theme
from companion.ui.dialog_chrome import make_frameless


@dataclass(frozen=True)
class SoundChoice:
    codec_key: str
    ldac_quality: Optional[str] = None
    aac_bitratemode: Optional[int] = None


class SoundDialog(QDialog):
    def __init__(self, device_name: str, current_codec_key: Optional[str] = None,
                 available_codecs: Optional[list[str]] = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SOUND")
        self.setMinimumWidth(360)

        root = QVBoxLayout(self)
        root.insertWidget(0, make_frameless(self, "SOUND"))
        eyebrow = QLabel(f"CODEC · {device_name.upper()}")
        eyebrow.setProperty("role", "eyebrow")
        eyebrow.setFont(theme.tracked_font(8.5, spacing_px=3.0))
        root.addWidget(eyebrow)

        sub = QLabel("Per device — each keeps its own codec, set independently.")
        sub.setProperty("role", "muted")
        sub.setWordWrap(True)
        root.addWidget(sub)

        self._group = QButtonGroup(self)
        self._buttons: dict[str, QRadioButton] = {}
        for info in codecs.KNOWN_CODECS:
            usable = available_codecs is None or info.key in available_codecs
            text = (f"{info.label}    {info.note}".strip() if usable
                    else f"{info.label}    — not available on this device")
            rb = QRadioButton(text)
            rb.setFont(theme.tracked_font(10.5, spacing_px=1.0))
            rb.setEnabled(usable)
            if not usable:
                rb.setProperty("role", "muted")
            self._group.addButton(rb)
            self._buttons[info.key] = rb
            root.addWidget(rb)
        # Default to the current codec if usable, else the best AVAILABLE codec; never
        # land the selection on a disabled (non-negotiable) option.
        chosen = current_codec_key if current_codec_key in self._buttons else None
        if available_codecs and (not chosen or chosen not in available_codecs):
            chosen = available_codecs[0]
        if not chosen or chosen not in self._buttons:
            chosen = "ldac"
        if not self._buttons[chosen].isEnabled():
            chosen = next((k for k, rb in self._buttons.items() if rb.isEnabled()),
                          chosen)
        self._buttons[chosen].setChecked(True)

        root.addSpacing(8)
        q = QLabel("LDAC QUALITY")
        q.setProperty("role", "eyebrow")
        root.addWidget(q)
        self._ldac = QComboBox()
        self._ldac.addItems(list(codecs.LDAC_QUALITY))
        self._ldac.setCurrentText("hq")
        root.addWidget(self._ldac)

        a = QLabel("AAC BITRATE MODE")
        a.setProperty("role", "eyebrow")
        root.addWidget(a)
        self._aac = QComboBox()
        self._aac.addItems([str(m) for m in codecs.AAC_BITRATE_MODE])
        self._aac.setCurrentText("5")
        root.addWidget(self._aac)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addSpacing(8)
        root.addWidget(buttons)

    def choice(self) -> SoundChoice:
        key = next(k for k, rb in self._buttons.items() if rb.isChecked())
        return SoundChoice(
            codec_key=key,
            ldac_quality=self._ldac.currentText() if key == "ldac" else None,
            aac_bitratemode=int(self._aac.currentText()) if key == "aac" else None,
        )
