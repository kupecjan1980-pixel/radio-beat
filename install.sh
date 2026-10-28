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

command -v python3 >/dev/null || { echo "Missing: python3" >&2; exit 1; }
if ! python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Gtk, Adw' 2>/dev/null; then
    echo "Needs GTK 4 and libadwaita for Python (python-gobject / python3-gobject, gtk4, libadwaita)." >&2
    [ -e /run/ostree-booted ] && echo "On Bazzite / Fedora Atomic: rpm-ostree install python3-gobject gtk4 libadwaita, then reboot." >&2
    exit 1
fi
# mpv plays the stream: the system's, else the Flathub one (atomic systems like Bazzite).
if ! command -v mpv >/dev/null; then
    if command -v flatpak >/dev/null; then
        if ! flatpak info io.mpv.Mpv >/dev/null 2>&1; then
            echo "Installing mpv from Flathub (for you only)…"
            # Bazzite has Flathub for the whole system only; a user install needs
            # it in the user's own remotes too (no password for either).
            flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
            flatpak install -y --user --noninteractive flathub io.mpv.Mpv
        fi
    else
        echo "Missing: mpv" >&2; exit 1
    fi
fi

# The station's logo, for the icon and the window when nothing has a cover.
if [ ! -s logo.png ]; then
    curl -fsSL -A "Mozilla/5.0" https://www.radiobeat.cz/img/logo@2x.png -o logo.png
fi

mkdir -p "$DEST" "$(dirname "$BIN")" "$(dirname "$DESKTOP")"
install -m 644 radio_beat.py logo.png "$DEST/"
# Fonts like the logo's, for the "Beat Radio" text style (SIL OFL, licences beside them).
mkdir -p "$DEST/fonts"
install -m 644 fonts/* "$DEST/fonts/"
# Which commit this is (get.sh passes it; a checkout knows it), for the update check.
echo "${RADIO_BEAT_VERSION:-$(git rev-parse HEAD 2>/dev/null || echo local)}" > "$DEST/VERSION"
HAVE_CAIRO=$(python3 -c 'import cairo' 2>/dev/null && echo 1 || true)
for size in 48 64 128 256 512; do
    mkdir -p "$ICONS/${size}x${size}/apps" "$DEST/icons/hicolor/${size}x${size}/apps"
    if [ -n "$HAVE_CAIRO" ]; then
        python3 make_icon.py logo.png "$ICONS/${size}x${size}/apps/$APP_ID.png" "$size"
    else
        cp logo.png "$ICONS/${size}x${size}/apps/$APP_ID.png"  # square already; themes scale it
    fi
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
# KDE Plasma ties windows to launchers through its own cache (ksycoca); a new
# entry may not be in it yet, and the window then gets a generic icon.
for k in kbuildsycoca6 kbuildsycoca5; do
    command -v "$k" >/dev/null && { "$k" >/dev/null 2>&1 || true; break; }
done

# An existing autostart entry points at the installed launcher.
if [ -f "$AUTOSTART" ]; then
    sed -i "s|^Exec=.*|Exec=$BIN --autostart|" "$AUTOSTART"
fi
echo "Installed. Start it from the app launcher (Rádio Beat) or run: radio-beat"
