#!/usr/bin/env bash
# Rádio Beat hub: the shared database every Rádio Beat reaches over the internet, run on THIS computer.
# Installs it as a user service (starts with the session, restarts if it stops), listening on 127.0.0.1:48766
# only; a tunnel (e.g. `tailscale funnel 48766`) makes it reachable from the internet.
#   ./hub-setup.sh            install and start
#   ./hub-setup.sh --remove   stop and remove (the shared data stays in ~/.local/share/radio-beat-hub)
set -euo pipefail
cd "$(dirname "$0")"
DEST="$HOME/.local/share/radio-beat"
UNIT="$HOME/.config/systemd/user/radio-beat-hub.service"

if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now radio-beat-hub.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "Rádio Beat hub removed (data kept in ~/.local/share/radio-beat-hub)."
    exit 0
fi

mkdir -p "$DEST" "$(dirname "$UNIT")"
install -m 644 radio_beat_hub.py "$DEST/"
cat > "$UNIT" <<'EOF'
[Unit]
Description=Rádio Beat hub (shared pictures, plays and track info for every Rádio Beat)
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 %h/.local/share/radio-beat/radio_beat_hub.py --port 48766 --max-gb 20
Restart=always
RestartSec=10
Nice=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now radio-beat-hub.service
sleep 1
echo "Rádio Beat hub: $(systemctl --user is-active radio-beat-hub.service)"
curl -fsS http://127.0.0.1:48766/radio-beat/v1/hello && echo
