#!/usr/bin/env bash
# KIAROS first-time setup for Linux: Python venv, frontend build, .env, HTTPS certificate.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v npm >/dev/null || { echo "Node.js 18+ (npm) is required"; exit 1; }

say "Python environment"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say "Frontend build"
( cd frontend && npm ci --no-audit --no-fund && npm run build )

say "Configuration"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — add your ANTHROPIC_API_KEY there or in the app's Settings panel."
fi
chmod 600 .env

say "HTTPS certificate (needed for the microphone in phone browsers)"
if [ ! -f cert.pem ] || [ "${1:-}" = "--new-cert" ]; then
  SAN="DNS:localhost,IP:127.0.0.1"
  for ip in $(hostname -I 2>/dev/null); do
    case "$ip" in *:*|172.17.*) ;; *) SAN="$SAN,IP:$ip" ;; esac
  done
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout key.pem -out cert.pem -subj "/CN=KIAROS" -addext "subjectAltName=$SAN" 2>/dev/null
  chmod 600 key.pem
  echo "Certificate covers: $SAN"
else
  echo "cert.pem exists (run with --new-cert if your IP changed)."
fi

say "Optional tools"
for t in gnome-screenshot playerctl brightnessctl; do
  command -v "$t" >/dev/null && echo "  ✓ $t" || echo "  - $t not installed (sudo apt install $t)"
done
command -v claude >/dev/null && echo "  ✓ Claude Code CLI" || echo "  - Claude Code CLI not found (needed for build/project commands)"

cat <<MSG

KIAROS is ready.
  Run now:            .venv/bin/python server.py
  Run at login:       scripts/install-service.sh
  Open on the laptop: http://localhost:8340
  Android app:        scripts/build-apk.sh
MSG
