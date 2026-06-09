#!/usr/bin/env bash
# From-source install into a private virtualenv under ~/.local (no root needed).
#
# Creates an isolated venv (your system Python stays untouched), installs Companion
# into it, and drops a launcher + desktop entry + icon so it shows up in your app
# drawer.
#
# Usage:        bash packaging/install.sh
# Uninstall:    bash packaging/uninstall.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PREFIX="${PREFIX:-$HOME/.local}"
APPHOME="$PREFIX/share/companion"
VENV="$APPHOME/venv"

echo "==> Installing into $VENV"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install "$HERE" >/dev/null

echo "==> Installing launcher, desktop entry and icon"
mkdir -p "$PREFIX/bin"
cat > "$PREFIX/bin/companion" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/companion" "\$@"
EOF
chmod +x "$PREFIX/bin/companion"

ICON_PATH="$PREFIX/share/icons/hicolor/scalable/apps/companion.svg"
install -Dm644 resources/icons/companion.svg "$ICON_PATH"

# Write the desktop entry with ABSOLUTE Exec/Icon paths. GNOME (and most desktops)
# launch .desktop entries with a minimal environment that does NOT put ~/.local/bin on
# PATH, so a bare `Exec=companion` fails to start from the app drawer. Point straight at
# the installed launcher and icon so it works regardless of the session PATH.
mkdir -p "$PREFIX/share/applications"
sed -e "s|^Exec=.*|Exec=$PREFIX/bin/companion|" \
    -e "s|^Icon=.*|Icon=$ICON_PATH|" \
    packaging/companion.desktop > "$PREFIX/share/applications/companion.desktop"
chmod 644 "$PREFIX/share/applications/companion.desktop"

# Validate the desktop entry (warn only) and refresh menu/icon caches.
command -v desktop-file-validate >/dev/null 2>&1 && \
  desktop-file-validate "$PREFIX/share/applications/companion.desktop" || true
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$PREFIX/share/applications" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
  gtk-update-icon-cache -f -t "$PREFIX/share/icons/hicolor" >/dev/null 2>&1 || true
command -v xdg-desktop-menu >/dev/null 2>&1 && \
  xdg-desktop-menu forceupdate >/dev/null 2>&1 || true

echo
echo "Installed."
if ! echo ":$PATH:" | grep -q ":$PREFIX/bin:"; then
  echo "  NOTE: $PREFIX/bin is not on your PATH. Add this to ~/.bashrc:"
  echo "        export PATH=\"$PREFIX/bin:\$PATH\""
fi
echo "  Run it now with:  companion"
echo "  In the app drawer it's 'Companion'. If it isn't there yet, log out and back"
echo "  in (GNOME caches the app grid). The entry is at:"
echo "    $PREFIX/share/applications/companion.desktop"
echo
echo "  Optional (passwordless rfkill unblock):"
echo "    sudo install -Dm644 polkit/org.companion.bluetooth.policy \\"
echo "      /usr/share/polkit-1/actions/org.companion.bluetooth.policy"
