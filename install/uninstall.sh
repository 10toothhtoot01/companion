#!/usr/bin/env bash
# Remove a Companion install created by install.sh (from-source) or
# install-appimage.sh (AppImage menu integration). Safe to run for either.
set -euo pipefail

PREFIX="${PREFIX:-$HOME/.local}"

rm -rf "$PREFIX/share/companion"        # from-source venv (install.sh)
rm -rf "$PREFIX/lib/companion"          # installed AppImage (install-appimage.sh)
rm -f  "$PREFIX/bin/companion"
rm -f  "$PREFIX/share/applications/companion.desktop"
rm -f  "$PREFIX/share/icons/hicolor/scalable/apps/companion.svg"

command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$PREFIX/share/applications" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
  gtk-update-icon-cache -f -t "$PREFIX/share/icons/hicolor" >/dev/null 2>&1 || true

echo "Uninstalled. (A system polkit policy, if you installed one, must be removed with sudo.)"
