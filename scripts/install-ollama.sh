#!/usr/bin/env bash
# Install Ollama for your user only (no sudo) and pull KIAROS's offline backup model.
#   scripts/install-ollama.sh [model]      default model: qwen2.5:3b
# Ollama listens on 127.0.0.1 only, runs as a systemd user service, and starts at login.
set -euo pipefail
MODEL="${1:-${OLLAMA_MODEL:-qwen2.5:3b}}"
PREFIX="$HOME/.local/opt/ollama"
UNIT="$HOME/.config/systemd/user/ollama.service"

if command -v ollama >/dev/null 2>&1 && [ ! -x "$PREFIX/bin/ollama" ]; then
  OLLAMA_BIN="$(command -v ollama)"
  echo "Using existing Ollama at $OLLAMA_BIN"
else
  OLLAMA_BIN="$PREFIX/bin/ollama"
  if [ ! -x "$OLLAMA_BIN" ]; then
    grep -qw avx2 /proc/cpuinfo || echo "Warning: this CPU lacks AVX2; Ollama will be very slow."
    # Resumable download into a permanent cache, so a dropped connection doesn't start over.
    cache="$HOME/.cache/kiaros"
    mkdir -p "$cache"
    archive="$cache/ollama-linux-amd64.tar.zst"
    url="https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst"
    echo "Downloading Ollama (about 1.5 GB; resumes if interrupted)…"
    for attempt in $(seq 1 30); do
      if curl -fL -C - --connect-timeout 20 --speed-limit 1024 --speed-time 60 -o "$archive" "$url"; then
        if zstd -tq "$archive" 2>/dev/null; then break; fi
        echo "Download was corrupted; starting it again."; rm -f "$archive"
      else
        echo "Connection dropped (attempt $attempt); resuming in 5 seconds…"; sleep 5
      fi
      [ "$attempt" = 30 ] && { echo "Download kept failing. Run this script again later; it resumes."; exit 1; }
    done
    mkdir -p "$PREFIX"
    tar --zstd -xf "$archive" -C "$PREFIX"
    rm -f "$archive"
  fi
  mkdir -p "$HOME/.local/bin"
  ln -sf "$OLLAMA_BIN" "$HOME/.local/bin/ollama"
fi

mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=Ollama (local AI models for KIAROS)
After=network-online.target

[Service]
ExecStart=$OLLAMA_BIN serve
Environment=OLLAMA_HOST=127.0.0.1:11434
Environment=OLLAMA_KEEP_ALIVE=30m
Environment=OLLAMA_VULKAN=0
Environment=GGML_VK_VISIBLE_DEVICES=
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNITEOF
systemctl --user daemon-reload
systemctl --user enable --now ollama.service

for _ in $(seq 1 60); do curl -fs http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
curl -fs http://127.0.0.1:11434/api/version || { echo "Ollama didn't start: journalctl --user -u ollama"; exit 1; }
echo
echo "Pulling $MODEL…"
"$OLLAMA_BIN" pull "$MODEL"
echo
echo "Done. KIAROS will use $MODEL as its offline backup."
echo "Use a different model: scripts/install-ollama.sh llama3.2:3b  (and set OLLAMA_MODEL in .env)"
