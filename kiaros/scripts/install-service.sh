#!/usr/bin/env bash
# Install KIAROS as a systemd user service that starts with your desktop session,
# plus an app launcher that opens it as its own window.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
UNIT_DIR="$HOME/.config/systemd/user"
APP_DIR="$HOME/.local/share/applications"
mkdir -p "$UNIT_DIR" "$APP_DIR" "$HOME/.local/share/icons"

[ -x .venv/bin/python ] || { echo "Run scripts/setup.sh first."; exit 1; }

cat > "$UNIT_DIR/kiaros.service" <<UNIT
[Unit]
Description=KIAROS voice assistant server
PartOf=graphical-session.target
After=graphical-session.target network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python $ROOT/server.py
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
UNIT

cp frontend/public/icon-512.png "$HOME/.local/share/icons/kiaros.png"
BROWSER_CMD="xdg-open http://localhost:8340"
for b in google-chrome google-chrome-stable chromium chromium-browser; do
  if command -v "$b" >/dev/null; then BROWSER_CMD="$b --app=http://localhost:8340 --class=KIAROS"; break; fi
done
cat > "$APP_DIR/kiaros.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=KIAROS
Comment=Voice assistant for your laptop and phone
Exec=$BROWSER_CMD
Icon=$HOME/.local/share/icons/kiaros.png
Terminal=false
Categories=Utility;
StartupWMClass=KIAROS
DESK

systemctl --user daemon-reload
systemctl --user enable --now kiaros.service
sleep 2
systemctl --user --no-pager --lines=0 status kiaros.service || true
echo
echo "KIAROS now starts automatically when you log in."
echo "Open it from your app grid (search 'KIAROS'), or visit http://localhost:8340"
echo "Logs:    journalctl --user -u kiaros -f"
echo "Stop:    systemctl --user disable --now kiaros"
