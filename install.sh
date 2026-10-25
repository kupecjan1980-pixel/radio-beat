#!/usr/bin/env bash
# Installs Rádio Beat for this user: app in ~/.local/share/radio-beat,
# `radio-beat` in ~/.local/bin, icon + menu entry. Rerun to update;
# `./install.sh --remove` takes it all away again.
set -euo pipefail
cd "$(dirname "$0")"
APP_ID=local.hanis.RadioBeat
DEST="$HOME/.local/share/radio-beat"
BIN="$HOME/.local/bin/radio-beat"
ICONS="$HOME/.local/share/icons/hicolor"
DESKTOP="$HOME/.local/share/applications/$APP_ID.desktop"
AUTOSTART="$HOME/.config/autostart/$APP_ID.desktop"

if [ "${1:-}" = "--remove" ]; then
    rm -rf "$DEST" "$BIN" "$DESKTOP" "$AUTOSTART"
    find "$ICONS" -name "$APP_ID.png" -delete 2>/dev/null || true
    echo "Rádio Beat removed."
    exit 0
fi

for dep in mpv python3; do
    command -v "$dep" >/dev/null || { echo "Missing: $dep" >&2; exit 1; }
done
python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); import cairo' \
    || { echo "Needs python-gobject, gtk4, libadwaita and python-cairo" >&2; exit 1; }

# The station's logo, for the icon and the window when nothing has a cover.
if [ ! -s logo.png ]; then
    curl -fsSL -A "Mozilla/5.0" https://www.radiobeat.cz/img/logo@2x.png -o logo.png
fi

mkdir -p "$DEST" "$(dirname "$BIN")" "$(dirname "$DESKTOP")"
install -m 644 radio_beat.py logo.png "$DEST/"
for size in 48 64 128 256 512; do
    mkdir -p "$ICONS/${size}x${size}/apps" "$DEST/icons/hicolor/${size}x${size}/apps"
    python3 make_icon.py logo.png "$ICONS/${size}x${size}/apps/$APP_ID.png" "$size"
    cp "$ICONS/${size}x${size}/apps/$APP_ID.png" "$DEST/icons/hicolor/${size}x${size}/apps/"
done
gtk-update-icon-cache -q -t "$ICONS" 2>/dev/null || true

cat > "$BIN" <<EOF
#!/bin/sh
exec python3 "$DEST/radio_beat.py" "\$@"
EOF
chmod 755 "$BIN"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Rádio Beat
GenericName=Internet radio
Comment=Rádio Beat live, with what's playing
Exec=$BIN
Icon=$APP_ID
StartupWMClass=$APP_ID
Categories=AudioVideo;Audio;Player;
Keywords=radio;beat;rock;
EOF
update-desktop-database -q "$(dirname "$DESKTOP")" 2>/dev/null || true

# An existing autostart entry points at the installed launcher.
if [ -f "$AUTOSTART" ]; then
    sed -i "s|^Exec=.*|Exec=$BIN --autostart|" "$AUTOSTART"
fi
echo "Installed. Start it from the app launcher (Rádio Beat) or run: radio-beat"
