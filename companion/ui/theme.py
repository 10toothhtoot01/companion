"""Theme loading: register the NDot-style mono font and apply the QSS.

PySide6 font rules (Qt6):
  - QFontDatabase.addApplicationFont returns an int id, -1 on failure; TTF/TTC/OTF only.
  - **letter-spacing in QSS is unreliable for widgets** (Mo1). The signature wide
    tracking of the Nothing look is therefore applied in code via
    QFont.setLetterSpacing(AbsoluteSpacing, px), which IS honored. QSS still carries
    colors/sizes; tracking comes from `tracked_font`.

If the bundled display font is missing we fall back to a monospace family so the app
still launches (capability-probe, never assume).
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase


def _resource(rel_parts: tuple, source: Path) -> Path:
    """Locate a bundled data file from source AND inside a PyInstaller build.

    PyInstaller does NOT collect non-.py files that live inside a package, so style.qss
    must be added explicitly in companion.spec. When frozen, that data is unpacked under
    sys._MEIPASS (the build's _internal dir for onedir, a temp dir for onefile); the spec
    places style.qss at companion/ui/ and the fonts at resources/fonts. Running from
    source, we fall back to the path relative to this file / the project tree.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base).joinpath(*rel_parts)
        if candidate.exists():
            return candidate
    return source


_QSS_PATH = _resource(("companion", "ui", "style.qss"),
                      Path(__file__).with_name("style.qss"))
_FONT_DIR = _resource(("resources", "fonts"),
                      Path(__file__).resolve().parents[2] / "resources" / "fonts")
_FALLBACK_FAMILY = "monospace"

_FAMILY = _FALLBACK_FAMILY  # resolved in apply()


def _load_font() -> str:
    if _FONT_DIR.exists():
        for path in sorted(_FONT_DIR.glob("*.ttf")) + sorted(_FONT_DIR.glob("*.otf")):
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id == -1:
                continue
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                return families[0]
    # No bundled display font: resolve the actual installed monospace family rather
    # than the literal "monospace" string, which some platforms/Qt builds don't map
    # to a real face (a common cause of mis-rendered text).
    try:
        fam = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        if fam:
            return fam
    except Exception:
        pass
    return _FALLBACK_FAMILY


def family() -> str:
    return _FAMILY


def tracked_font(size: float, spacing_px: float = 0.0, bold: bool = False) -> QFont:
    """Build a QFont with reliable letter-spacing (the QSS property is ignored)."""
    f = QFont(_FAMILY)
    f.setPointSizeF(size)
    f.setBold(bold)
    if spacing_px:
        f.setLetterSpacing(QFont.AbsoluteSpacing, spacing_px)
    return f


def apply(app) -> None:
    """Apply Companion's Nothing-style theme to a QApplication."""
    global _FAMILY
    _FAMILY = _load_font()
    base = QFont(_FAMILY)
    base.setPointSizeF(10.5)
    base.setLetterSpacing(QFont.AbsoluteSpacing, 0.5)
    app.setFont(base)
    qss = _QSS_PATH.read_text(encoding="utf-8") if _QSS_PATH.exists() else ""
    qss = qss.replace("__DISPLAY_FONT__", _FAMILY)
    app.setStyleSheet(qss)
