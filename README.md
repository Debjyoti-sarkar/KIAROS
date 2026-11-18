<div align="center">

<img src="frontend/public/icon-192.png" width="96" alt="KIAROS">

# KIAROS

**Knowledge-Integrated Autonomous Responsive Operating System**

A voice assistant that lives on your Linux laptop and in your pocket.<br>
Talk to it from either device. Both stay in sync.

</div>

---

## What it does

Say it to your laptop or your phone; KIAROS answers out loud and gets it done.

| | Try saying |
|---|---|
| 💻 **Control the laptop** | "volume to 30" · "mute" · "pause the music" · "next song" · "open VS Code" · "open downloads" · "lock my laptop" · "battery" · "how's my laptop" · "shut down the laptop" *(asks you to confirm)* |
| 📱 **Use the phone** | "call Mom" · "text Rahul I'm running late" · "WhatsApp Priya see you soon" · "take me to the airport" |
| 👤 **Contacts** | "save Mom's number 98765 43210" |
| ⏰ **Timers & reminders** | "remind me in 10 minutes to check the oven" · "set a 25 minute timer" (rings on every device) |
| 🌐 **Web & research** | "search for biryani near me" · "open github.com" · "research the best laptops under 1 lakh" (live web search, delivered as a report) |
| 📅 **Calendar & email** | "what's on my calendar" · "any new emails" (read-only) |
| 📝 **Notes & tasks** | "save that as a note" · "read my note about the trip" · "remind me to call the bank tomorrow" · "what's on my list" |
| 👨‍💻 **Code** | "build me a landing page for my bakery" · "jump into my-app and fix the login bug" · "where are we on that build" |
| 🖥️ **Screen** | "what's on my screen" |

### Laptop and phone, working together

- **One conversation everywhere.** Start on the laptop, continue on the phone. KIAROS remembers what you were talking about across devices and restarts.
- **Live mirror.** Whatever you say on one device, and the reply, shows up instantly on the other as captions, marked *on phone* or *on laptop*. Only the device you spoke to says the reply out loud.
- **Cross-device actions.** Say "call Mom" at the laptop and your phone dials. Say "pause the music" on the phone and the laptop pauses.
- **Presence.** The laptop and phone icons at the top light up when each device is connected.
- **Shared everything.** Tasks, notes, contacts, memories, timers and builds are the same on every device.

## How it fits together

```
     📱 Phone (Android app or browser)            💻 Laptop window
     tap to talk · calls · SMS · Maps             always listening · orb
                  │                                     │
                  │  Wi-Fi or Tailscale                 │ localhost
                  │  (pairing token required)           │
                  └──────────────┬──────────────────────┘
                                 ▼
                ┌────────────────────────────────────┐
                │  KIAROS server (runs on the laptop) │
                │  Claude Haiku 5.5 · Opus 5.5        │
                │  shared conversation & memory       │
                │  laptop control · calendar · mail   │
                │  notes · timers · Claude Code       │
                └────────────────────────────────────┘
```

The laptop is the brain. The phone is a remote that talks to it, so **the laptop needs to be on** for the phone to work.

---

## Install on the laptop

**You need:** Linux (built for Ubuntu with GNOME), Python 3.11+, Node.js 18+, Google Chrome or Chromium, and an [Anthropic API key](https://console.anthropic.com/).

```bash
git clone https://github.com/Debjyoti-sarkar/KIAROS.git
cd KIAROS
scripts/setup.sh
```

`setup.sh` installs everything, builds the interface, creates your `.env` settings file, and makes the security certificate phone browsers need.

### Start it

```bash
.venv/bin/python server.py
```

Open **http://localhost:8340** in Chrome. The Settings panel opens the first time: paste your Anthropic API key, click **Save**, and start talking. On the laptop KIAROS listens all the time; click the mic button to mute it.

### Start it automatically at login (recommended)

```bash
scripts/install-service.sh
```

KIAROS then starts every time you log in, and a **KIAROS** app appears in your app grid that opens it in its own window.

| | |
|---|---|
| See what it's doing | `journalctl --user -u kiaros -f` |
| Restart it | `systemctl --user restart kiaros` |
| Turn off autostart | `systemctl --user disable --now kiaros` |

---

## Install on your phone

The phone and laptop must be on **the same Wi-Fi** (or see [Away from home](#away-from-home)).

### Option A: the Android app (recommended)

Native speech recognition, and it can dial and text directly.

1. On the laptop, build the app:
   ```bash
   scripts/build-apk.sh
   ```
   This creates `dist/KIAROS.apk`. It needs the Android SDK and Java 17 or 21, which Android Studio installs.
2. Get the APK onto the phone: copy it over (USB, Drive, Telegram…) and open it, allowing "Install unknown apps" when asked. Or plug the phone in with USB debugging on and run `adb install -r dist/KIAROS.apk`.
3. On the laptop, open KIAROS → **⋮ menu → Pair phone**.
4. In the phone app, type the **server address** and **token** from that screen and tap **Connect**.
5. **Tap the orb (or the mic button) and speak.** The ⌨ button lets you type instead.

### Option B: the phone's browser (nothing to install)

1. On the laptop, open KIAROS → **⋮ menu → Pair phone**.
2. Scan the QR code with your phone camera.
3. Chrome warns about the certificate the first time: tap **Advanced → Proceed**. The certificate is your own laptop's.
4. Chrome menu → **Add to Home screen** gives you a KIAROS icon.

In the browser, calls and texts appear as a button you tap once, because browsers don't let websites open the dialer on their own.

### Away from home

Install [Tailscale](https://tailscale.com/) (free) on the laptop and the phone and sign in to the same account. Then use the laptop's Tailscale address (`100.x.y.z`) as the server address in the app. **Don't** expose KIAROS to the open internet with port forwarding.

---

## Settings

Everything is in the app's **Settings** panel; it's stored in `.env` (template: `.env.example`).

| Setting | What it's for |
|---|---|
| **Anthropic API key** | Required. Claude is KIAROS's brain. |
| **Fish Audio key + voice ID** | Optional premium voice. Without it, each device uses its own built-in voice. |
| **Calendar link** | Google Calendar → Settings → your calendar → *Secret address in iCal format*. Several can be comma-separated. Read-only. |
| **Email + app password** | Gmail: create an [app password](https://myaccount.google.com/apppasswords). Read-only: KIAROS can never send, delete or mark email. |
| **Your name / how to address you / weather city** | Personalisation. |

Advanced options in `.env`:

| Variable | Default | |
|---|---|---|
| `NOTES_DIR` | `~/Documents/KIAROS Notes` | Where notes are saved (Markdown) |
| `KIAROS_PROJECTS_DIR` | `~/Desktop` | Where Claude Code builds new projects |
| `KIAROS_PORT` / `KIAROS_HTTPS_PORT` | `8340` / `8341` | Laptop + app / phone browser |
| `KIAROS_TOKEN` | generated | Phone pairing token. Delete it and restart to unpair every device. |
| `KIAROS_DATA_DIR` | `./data` | Memory, conversation, reports |

**Optional laptop extras:** `sudo apt install gnome-screenshot` lets KIAROS see your screen. Install the [Claude Code CLI](https://code.claude.com/docs) for "build me…" and project commands.

---

## Privacy & security

- **Only paired devices get in.** Anything that isn't the laptop itself must present the pairing token: every request, the voice connection, and report links.
- **No arbitrary commands.** Laptop control is a fixed list of safe actions. Shutdown, restart, sleep and log-out always ask "Say yes to confirm".
- **Email is read-only** and **notes are never edited or deleted.** KIAROS only reads email and only creates new notes.
- **Your data stays on your laptop** in `data/`. Speech text is sent to Anthropic to generate replies (and to Fish Audio if you enable it).
- **Builds** run Claude Code with full permissions inside the new project's folder. Only pair devices you trust.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "My Anthropic API key isn't set" | Settings → paste the key → Save. |
| Phone app can't connect | Same Wi-Fi? Server running? Address must include `:8340`. If `ufw` is on: `sudo ufw allow 8340,8341/tcp`. |
| Phone shows a red dot / "not paired" | The token changed. Pair again from the laptop. |
| Phone browser has no microphone | Use the `https://…:8341` link from the QR code, not `http://`. If your Wi-Fi IP changed, run `scripts/setup.sh --new-cert`. |
| Laptop doesn't hear me | Use Chrome/Chromium, allow the microphone, check the mic button isn't red (muted). |
| "I can't see the screen yet" | `sudo apt install gnome-screenshot`. |
| Build commands don't work | Install the Claude Code CLI and make sure `claude` runs in a terminal. |

---

## For developers

```bash
.venv/bin/python server.py           # backend on :8340 (and :8341 with a cert)
cd frontend && npm run dev           # live-reload UI on :5173
cd frontend && npm run android:sync  # rebuild + copy into the Android project
```

### Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest              # everything (~8 min; the UI tests render the orb without a GPU)
.venv/bin/python -m pytest -m "not ui"  # skip the browser UI tests (~2 min)
```

The suite starts a real server with throwaway data (your memory, contacts and `.env` are never touched) and covers:
- **End-user flows** (`tests/test_user_flows.py`): pairing and tokens, laptop commands, power confirmation, calling and texting via the phone, laptop↔phone sync, reconnect history, settings.
- **Browser UI** (`tests/test_ui.py`): your installed Chrome drives the real interface at laptop and phone sizes, including pairing, typed commands, both devices side by side, and the call button.
- **Unit tests** for planning, QA, suggestions, templates and browsing.

Tests that need a live Anthropic key or open internet skip themselves when those aren't available.

| Part | Where |
|---|---|
| Server, voice loop, actions, sync | `server.py` |
| Claude calls (models, research) | `llm.py` |
| Laptop controls (allowlist) | `system_control.py` |
| Terminal / browser / Claude Code | `actions.py` |
| Calendar · email · notes · screen | `calendar_access.py` · `mail_access.py` · `notes_access.py` · `screen.py` |
| Memory, tasks, contacts (SQLite) | `memory.py` |
| Pairing / auth | `auth.py` |
| Interface (laptop, browser, app) | `frontend/src/` |
| Android app | `frontend/android/` |
| Tests | `tests/` |

**Models:** `claude-haiku-5-5` (fast, for conversation) and `claude-opus-5-5` with web search (research). Change them with `KIAROS_FAST_MODEL` / `KIAROS_DEEP_MODEL`.

## License

Personal, non-commercial use. See [LICENSE](LICENSE) for the full terms.
