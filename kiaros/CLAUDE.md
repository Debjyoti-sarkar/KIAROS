# KIAROS — Voice AI Assistant (Linux laptop + Android phone)

## Overview
KIAROS (Knowledge-Integrated Autonomous Responsive Operating System) is a voice-first assistant. The server runs on the user's Linux laptop; clients are the laptop browser window, the phone browser (PWA), and the Android app (Capacitor). It controls the laptop, sends call/SMS/Maps actions to the phone, reads calendar (ICS) and email (IMAP, read-only), writes Markdown notes, sets timers, researches with web search, and drives Claude Code for builds.

## Quick Start
When a user opens this repo with Claude Code, help them:
1. `scripts/setup.sh` (venv, deps, frontend build, .env, HTTPS cert with LAN IP SANs)
2. Add `ANTHROPIC_API_KEY` to `.env` (or in the Settings panel)
3. `.venv/bin/python server.py`, or `scripts/install-service.sh` for a systemd user service
4. Open http://localhost:8340
5. Phone: `scripts/build-apk.sh` → install `dist/KIAROS.apk`, pair via menu → Pair phone

## Architecture
- **Backend**: FastAPI (`server.py`). HTTP :8340 (laptop + app), HTTPS :8341 (phone browser) when cert.pem exists.
- **LLM**: `llm.py`. `claude-haiku-5-5` at effort "low" for the voice loop; `claude-opus-5-5` + `web_search_20260209` + server-side refusal fallbacks for research. Always read replies with `llm.text_of()`: these models return thinking blocks, so never use `content[0].text`.
- **Auth**: `auth.py`. Loopback is trusted; all other HTTP/WS needs `KIAROS_TOKEN` (Bearer header or `?token=`). `/api/pairing` is loopback-only.
- **Laptop control**: `system_control.py`, an allowlisted command set (wpctl/pactl, MPRIS via gdbus, gtk-launch, GNOME ScreenSaver lock, systemctl power). Power actions need spoken confirmation (handled in `server.py` via `session["pending_confirm"]`).
- **Desktop actions**: `actions.py` (terminal detection, xdg-open, Claude Code launch, project naming).
- **Integrations**: `calendar_access.py` (ICS + recurring events), `mail_access.py` (IMAP EXAMINE/BODY.PEEK, read-only), `notes_access.py` (Markdown files, create-only), `screen.py` (screenshot tools + vision; window list only on X11).
- **Memory**: `memory.py`, SQLite + FTS5 at `data/kiaros.db` (memories, tasks, notes, contacts).
- **Frontend**: `frontend/src`. `config.ts` (server URL/token/device), `voice.ts` (web vs native speech, device TTS fallback), `main.ts` (state machine, device actions, captions), `settings.ts` (settings + pairing QR), `orb.ts` (Three.js).
- **Android**: `frontend/android` (Capacitor 8). `androidScheme: "http"` + cleartext so the app can reach the LAN server.

## WebSocket protocol (`/ws/voice`)
Client → server: `hello {device, platform, native}`, `transcript {text, isFinal}`, `fix_self`.
Server → client: `audio {data, text}` (Fish MP3), `text {text, speak}` (device TTS), `status {state}`, `device_action {action: call|sms|whatsapp|navigate|open_url|alert, ...}`.

## Action tags (LLM → server)
`[ACTION:SYSTEM] cmd ||| arg`, `[ACTION:PHONE] call|sms|whatsapp|navigate|open_url ||| target ||| message`, `[ACTION:ADD_CONTACT] name ||| number`, `[ACTION:TIMER] seconds ||| label`, `[ACTION:BROWSE]`, `[ACTION:RESEARCH]`, `[ACTION:BUILD]`, `[ACTION:OPEN_TERMINAL]`, `[ACTION:PROMPT_PROJECT] name ||| prompt`, `[ACTION:SCREEN]`, `[ACTION:ADD_TASK]`, `[ACTION:COMPLETE_TASK]`, `[ACTION:ADD_NOTE]`, `[ACTION:CREATE_NOTE]`, `[ACTION:READ_NOTE]`, `[ACTION:REMEMBER]`. Executed in `_dispatch_embedded_action()`.

## Conventions
- Personality: calm, precise, quietly witty; 1–2 spoken sentences max.
- All speech goes through `speak(ws, text)`, which strips markdown and banned phrases and applies the honorific.
- Never add arbitrary shell execution reachable from speech; extend the allowlist in `system_control.py` instead.
- Mail stays read-only; notes are create-only.
- The LICENSE (personal, non-commercial; attribution to the original JARVIS author) must stay intact.
