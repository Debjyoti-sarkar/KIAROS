# KIAROS

**Knowledge-Integrated Autonomous Responsive Operating System**: a voice assistant that runs on your Linux laptop and in your pocket as an Android app.

Talk to it from either device. It answers out loud, controls the laptop, places calls and texts from your phone, reads your calendar and email, takes notes, sets timers, does researched web reports, and drives Claude Code to build software.

```
            ┌──────────── Android app / phone browser ────────────┐
  "call mom"│  tap-to-talk · native speech · dialer/SMS/Maps      │
            └──────────────────────┬──────────────────────────────┘
                                   │ Wi-Fi or Tailscale (token-protected)
            ┌──────────────────────▼──────────────────────────────┐
            │  KIAROS server (FastAPI, on the laptop)             │
            │  Claude Haiku 5.5 (voice) · Opus 5.5 (research)     │
            │  laptop control · calendar · mail · notes · timers  │
            │  Claude Code for builds                             │
            └──────────────────────▲──────────────────────────────┘
  "volume up"                      │ localhost
            ┌──────────────────────┴──────────────────────────────┐
            │  Laptop window (always listening) · particle orb    │
            └─────────────────────────────────────────────────────┘
```

## What you can say

| Area | Examples |
|------|----------|
| **Laptop** | "volume to 30", "mute", "pause the music", "next song", "open VS Code", "open downloads", "lock my laptop", "how's my laptop", "battery", "brightness up", "shut down the laptop" *(asks to confirm)* |
| **Phone** | "call Mom", "text Rahul I'm running late", "WhatsApp Priya", "take me to the airport" |
| **Contacts** | "save Mom's number 98765 43210" |
| **Time** | "remind me in 10 minutes to check the oven", "set a 25 minute timer" |
| **Web** | "search for the best biryani near me", "open github.com", "research the best laptops under 1 lakh" (live web search → report) |
| **Calendar / mail** | "what's on my calendar", "any new emails" *(read-only)* |
| **Notes / tasks** | "save that as a note", "read my note about the trip", "remind me to call the bank tomorrow", "what's on my list" |
| **Code** | "build me a landing page for my bakery", "jump into my-app and fix the login bug", "where are we on that build" |
| **Screen** | "what's on my screen" *(needs `gnome-screenshot`)* |

Commands you say on the phone can act on the laptop ("pause the music on my laptop"), and phone actions you ask for at the laptop are sent to your phone if it's connected.

## Quick start (laptop)

Requirements: Linux (tested on Ubuntu GNOME, Wayland), Python 3.11+, Node.js 18+, Chrome or Chromium.

```bash
cd kiaros
scripts/setup.sh              # venv, dependencies, frontend build, .env, HTTPS cert
.venv/bin/python server.py    # or: scripts/install-service.sh to start at login
```

Open **http://localhost:8340**. Settings opens automatically on first run. Paste your [Anthropic API key](https://console.anthropic.com/) and you're talking. The laptop window listens continuously; use the mic button to mute.

`scripts/install-service.sh` installs a systemd user service (starts with your desktop session) and a **KIAROS** launcher in your app grid that opens it as its own window.

## Phone

Two options. Both need the phone on the same Wi-Fi as the laptop (or see *Away from home* below).

**Android app (recommended).** Native speech recognition, places calls and texts directly.
```bash
scripts/build-apk.sh          # → dist/KIAROS.apk
```
Install the APK on the phone. On the laptop, open KIAROS → menu → **Pair phone**, then type the server address and token it shows into the app.

**Phone browser (no install).** On the laptop, open menu → **Pair phone** and scan the QR code. Accept the self-signed certificate warning once. Then use Chrome's menu → **Add to Home screen** for an app icon. Browsers require a tap to open the dialer, so calls and texts appear as a one-tap button.

On the phone, **tap the orb or the mic button to talk**. The keyboard button lets you type a command instead.

### Away from home
Install [Tailscale](https://tailscale.com/) on the laptop and phone, then use the laptop's Tailscale IP (100.x.y.z) as the server address. Don't port-forward KIAROS to the internet. The token is the only thing protecting a server that can control your laptop.

## Configuration

Everything can be set in the Settings panel, or in `.env` (see `.env.example`):

| Setting | Purpose |
|---------|---------|
| `ANTHROPIC_API_KEY` | **Required.** Claude powers the conversation. |
| `FISH_API_KEY`, `FISH_VOICE_ID` | Optional premium voice. Without them each device uses its own built-in voice. |
| `CALENDAR_ICS_URLS` | Read-only calendar via ICS link (Google: Settings → calendar → *Secret address in iCal format*). Comma-separate several. |
| `MAIL_ADDRESS`, `MAIL_APP_PASSWORD`, `MAIL_IMAP_HOST` | Read-only email over IMAP. Gmail needs an [app password](https://myaccount.google.com/apppasswords). |
| `NOTES_DIR` | Where Markdown notes are saved (default `~/Documents/KIAROS Notes`). |
| `USER_NAME`, `HONORIFIC`, `WEATHER_LOCATION` | Personalisation. |
| `KIAROS_TOKEN` | Phone pairing token, generated on first start. Change it to revoke all paired devices. |
| `KIAROS_PROJECTS_DIR` | Where Claude Code builds new projects (default `~/Desktop`). |

Optional laptop tools that unlock more: `gnome-screenshot` (screen vision), `playerctl` / `brightnessctl` (media and brightness on non-GNOME desktops), and the [Claude Code CLI](https://code.claude.com/docs) (build/project commands).

## Security model

- The server listens on your network so the phone can reach it. **Every request from anything other than the laptop itself must carry the pairing token** (`KIAROS_TOKEN`), including API calls, the voice WebSocket, and report links.
- Laptop controls are an allowlist (`system_control.py`). KIAROS never runs arbitrary shell commands from speech. Shutdown, restart, suspend and logout require a spoken "yes".
- Email is read-only by construction (IMAP `EXAMINE` + `BODY.PEEK`; nothing is ever marked read, sent or deleted).
- Notes are only ever created, never edited or deleted.
- Build and project commands run Claude Code with `--dangerously-skip-permissions` inside the target project folder, the same trade-off as the original project. Only pair devices you trust.

## Architecture

| Layer | Technology |
|-------|-----------|
| Server | FastAPI + Python (`server.py`); HTTP on 8340, HTTPS on 8341 when `cert.pem` exists |
| LLM | `claude-haiku-5-5` (low effort) for the voice loop; `claude-opus-5-5` with web search for research (`llm.py`) |
| Laptop control | `system_control.py`: PipeWire `wpctl`, MPRIS over D-Bus, `.desktop` app launch, GNOME lock, `systemctl` power |
| Desktop actions | `actions.py`: terminal (gnome-terminal / ptyxis / konsole …), browser, Claude Code launch |
| Calendar / mail / notes | `calendar_access.py` (ICS) · `mail_access.py` (IMAP, read-only) · `notes_access.py` (Markdown files) |
| Memory | SQLite + FTS5 (`memory.py`): facts, tasks, notes, contacts |
| Client | Vite + TypeScript + Three.js orb; one codebase for laptop, PWA and app (`frontend/`) |
| Android | Capacitor 8 + native speech recognition / TTS plugins (`frontend/android/`) |
| Auth | `auth.py`: loopback trusted, everything else needs the token |

### Voice loop
1. The device transcribes speech (Web Speech API on the laptop, Android's recognizer in the app).
2. The transcript goes to the server over the WebSocket, tagged with which device is speaking.
3. Short, unambiguous laptop commands ("volume up") run instantly without an LLM call.
4. Everything else goes to Claude, which answers in a sentence or two and may append one `[ACTION:…]` tag.
5. The server runs the action locally, or forwards phone actions to the phone.
6. The reply is spoken via Fish Audio (if configured) or the device's own voice, and shown as a caption.

## Development

```bash
.venv/bin/python server.py                 # backend
cd frontend && npm run dev                 # hot-reload UI on :5173 (proxies to :8340)
cd frontend && npm run android:sync        # rebuild + copy web assets into the Android project
cd frontend && npm run android:open        # open in Android Studio
```

## License & credits

KIAROS is a derivative of the open-source **JARVIS** voice assistant by Ethan Rogers ([ethanplus.ai](https://ethanplus.ai)), used under its license: **free for personal, non-commercial use**, and commercial use requires a license from the original author. See [LICENSE](LICENSE), which must be kept with any copy.

Powered by [Anthropic Claude](https://anthropic.com). Optional voice by [Fish Audio](https://fish.audio).
