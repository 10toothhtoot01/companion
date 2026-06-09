# Display font

Drop a dot-matrix / NDot-style **TTF or OTF** here (Qt6 only accepts TTF/TTC/OTF via
`QFontDatabase.addApplicationFont`). `companion/ui/theme.py` loads the first font it finds
and substitutes its family name into the QSS `__DISPLAY_FONT__` token.

If no font is present, Companion falls back to a monospace family so it still launches.

Suggested free options with the right mechanical feel: a dot-matrix display face, or a
grid-based mono. Keep the license file alongside the font.
