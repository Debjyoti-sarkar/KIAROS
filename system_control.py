"""
KIAROS System Control — voice-driven laptop controls for Linux (GNOME/PipeWire first).

Every command is on an explicit allowlist; nothing here runs arbitrary shell.
Power actions (shutdown/restart/suspend/logout) require a spoken confirmation,
handled by the server before calling run_command().

Public entry point: run_command(name, arg) -> str (a short spoken confirmation).
"""

import asyncio
import configparser
import difflib
import logging
import os
import re
import shutil
import time
from pathlib import Path

log = logging.getLogger("kiaros.system")

CONFIRM_REQUIRED = {"shutdown", "restart", "suspend", "logout"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run(*cmd: str, timeout: float = 5.0) -> tuple[int, str]:
    if not shutil.which(cmd[0]):
        return 127, ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        return 124, ""
    except Exception as e:
        log.warning(f"{cmd[0]} failed: {e}")
        return 1, ""


async def _gdbus(dest: str, path: str, method: str, *args: str) -> tuple[int, str]:
    return await _run("gdbus", "call", "--session", "--dest", dest,
                      "--object-path", path, "--method", method, *args)


def _pct(arg: str, default: int = 10) -> int:
    m = re.search(r"\d+", arg or "")
    return max(0, min(150, int(m.group()))) if m else default


# ---------------------------------------------------------------------------
# Volume (PipeWire wpctl, falling back to PulseAudio pactl)
# ---------------------------------------------------------------------------

async def get_volume() -> tuple[int | None, bool]:
    code, out = await _run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
    if code == 0:
        m = re.search(r"([\d.]+)", out)
        return (round(float(m.group(1)) * 100) if m else None), "MUTED" in out
    code, out = await _run("pactl", "get-sink-volume", "@DEFAULT_SINK@")
    if code == 0:
        m = re.search(r"(\d+)%", out)
        _, mute = await _run("pactl", "get-sink-mute", "@DEFAULT_SINK@")
        return (int(m.group(1)) if m else None), "yes" in mute
    return None, False


async def set_volume(kind: str, amount: int) -> bool:
    if shutil.which("wpctl"):
        target = {"up": f"{amount}%+", "down": f"{amount}%-", "set": f"{amount}%"}[kind]
        code, _ = await _run("wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", target)
        if kind != "down":
            await _run("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0")
        return code == 0
    target = {"up": f"+{amount}%", "down": f"-{amount}%", "set": f"{amount}%"}[kind]
    code, _ = await _run("pactl", "set-sink-volume", "@DEFAULT_SINK@", target)
    return code == 0


async def set_mute(state: str) -> bool:
    if shutil.which("wpctl"):
        code, _ = await _run("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", state)
    else:
        code, _ = await _run("pactl", "set-sink-mute", "@DEFAULT_SINK@", state)
    return code == 0


# ---------------------------------------------------------------------------
# Media (MPRIS over D-Bus; playerctl if installed)
# ---------------------------------------------------------------------------

async def _mpris_players() -> list[str]:
    code, out = await _gdbus("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames")
    if code != 0:
        return []
    return re.findall(r"org\.mpris\.MediaPlayer2\.[\w.\-]+", out)


async def media(action: str) -> bool:
    method = {"play_pause": "PlayPause", "play": "Play", "pause": "Pause",
              "next": "Next", "previous": "Previous", "stop": "Stop"}[action]
    if shutil.which("playerctl"):
        code, _ = await _run("playerctl", method.lower() if action != "play_pause" else "play-pause")
        return code == 0
    players = await _mpris_players()
    if not players:
        return False
    ok = False
    # Pause/stop applies to every player; play/next/previous to the first one.
    targets = players if action in ("pause", "stop") else players[:1]
    for p in targets:
        code, _ = await _gdbus(p, "/org/mpris/MediaPlayer2", f"org.mpris.MediaPlayer2.Player.{method}")
        ok = ok or code == 0
    return ok


# ---------------------------------------------------------------------------
# Apps (.desktop entries)
# ---------------------------------------------------------------------------

_APP_DIRS = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path.home() / ".local/share/applications",
    Path("/var/lib/snapd/desktop/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path.home() / ".local/share/flatpak/exports/share/applications",
]
_app_cache: dict[str, str] = {}
_app_cache_time = 0.0


def _scan_apps() -> dict[str, str]:
    """Map lowercase app names (and keywords) -> desktop file id."""
    global _app_cache, _app_cache_time
    if _app_cache and time.time() - _app_cache_time < 300:
        return _app_cache
    apps: dict[str, str] = {}
    for d in _APP_DIRS:
        if not d.is_dir():
            continue
        for f in d.glob("*.desktop"):
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("NoDisplay", "false").lower() == "true" or e.get("Type", "Application") != "Application":
                continue
            name = e.get("Name", "").strip().lower()
            if name:
                apps.setdefault(name, f.stem)
            for kw in e.get("Keywords", "").split(";"):
                kw = kw.strip().lower()
                if kw:
                    apps.setdefault(kw, f.stem)
            apps.setdefault(f.stem.lower().split(".")[-1], f.stem)
    _app_cache, _app_cache_time = apps, time.time()
    return apps


def find_app(query: str) -> str | None:
    q = query.lower().strip()
    q = re.sub(r"^(the|my)\s+", "", q).removesuffix(" app")
    apps = _scan_apps()
    if q in apps:
        return apps[q]
    for name, desktop_id in apps.items():
        if name.startswith(q) or q in name:
            return desktop_id
    close = difflib.get_close_matches(q, list(apps), n=1, cutoff=0.7)
    return apps[close[0]] if close else None


async def open_app(query: str) -> str:
    desktop_id = find_app(query)
    if not desktop_id:
        # Last resort: an executable with that name.
        exe = shutil.which(query.lower().replace(" ", "-"))
        if exe:
            from actions import spawn_detached
            await spawn_detached(exe)
            return f"Opening {query}."
        return f"I couldn't find an app called {query}."
    from actions import spawn_detached
    if await spawn_detached("gtk-launch", desktop_id) or await spawn_detached("gio", "launch", desktop_id):
        return f"Opening {query}."
    return f"{query} wouldn't start."


# ---------------------------------------------------------------------------
# Screen lock, power, brightness
# ---------------------------------------------------------------------------

async def lock_screen() -> bool:
    code, _ = await _gdbus("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver.Lock")
    if code == 0:
        return True
    sid = os.getenv("XDG_SESSION_ID")
    if sid:
        code, _ = await _run("loginctl", "lock-session", sid)
        if code == 0:
            return True
    code, _ = await _run("loginctl", "lock-sessions")
    return code == 0


async def power(action: str) -> bool:
    if action == "logout":
        code, _ = await _gdbus("org.gnome.SessionManager", "/org/gnome/SessionManager",
                               "org.gnome.SessionManager.Logout", "uint32 1")
        return code == 0
    verb = {"shutdown": "poweroff", "restart": "reboot", "suspend": "suspend"}[action]
    # Delay slightly so KIAROS can finish speaking first.
    await asyncio.sleep(3)
    code, _ = await _run("systemctl", verb)
    return code == 0


async def brightness(direction: str, amount: int = 10) -> bool:
    if shutil.which("brightnessctl"):
        target = {"up": f"+{amount}%", "down": f"{amount}%-", "set": f"{amount}%"}[direction]
        code, _ = await _run("brightnessctl", "set", target)
        return code == 0
    if direction in ("up", "down"):
        method = "StepUp" if direction == "up" else "StepDown"
        code, _ = await _gdbus("org.gnome.SettingsDaemon.Power", "/org/gnome/SettingsDaemon/Power",
                               f"org.gnome.SettingsDaemon.Power.Screen.{method}")
        return code == 0
    return False


# ---------------------------------------------------------------------------
# Status: battery, CPU, memory, disk
# ---------------------------------------------------------------------------

def battery_status() -> str:
    for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        try:
            cap = (bat / "capacity").read_text().strip()
            status = (bat / "status").read_text().strip().lower()
        except OSError:
            continue
        state = {"charging": "and charging", "discharging": "on battery",
                 "full": "fully charged", "not charging": "plugged in"}.get(status, status)
        return f"Battery is at {cap} percent, {state}."
    return "This machine doesn't report a battery."


def system_status() -> str:
    parts = []
    try:
        load1 = os.getloadavg()[0]
        cpus = os.cpu_count() or 1
        parts.append(f"CPU load {round(load1 / cpus * 100)} percent")
    except OSError:
        pass
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k] = int(v.split()[0])
        used = 1 - info["MemAvailable"] / info["MemTotal"]
        parts.append(f"memory {round(used * 100)} percent used")
    except Exception:
        pass
    try:
        du = shutil.disk_usage(str(Path.home()))
        parts.append(f"{round(du.free / 1e9)} gigabytes free on disk")
    except OSError:
        pass
    status = ", ".join(parts)
    batt = battery_status()
    return (status[:1].upper() + status[1:] + ". " if status else "") + (batt if "doesn't" not in batt else "")


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

async def notify(title: str, body: str = "") -> bool:
    code, _ = await _run("notify-send", "-a", "KIAROS", title, body)
    return code == 0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

COMMANDS = {
    "volume_up", "volume_down", "volume_set", "mute", "unmute", "volume_get",
    "play_pause", "play", "pause", "next", "previous", "stop",
    "open_app", "open_folder", "lock", "brightness_up", "brightness_down",
    "battery", "status", "notify",
    "shutdown", "restart", "suspend", "logout",
}

_FOLDERS = {
    "downloads": "Downloads", "documents": "Documents", "pictures": "Pictures",
    "photos": "Pictures", "music": "Music", "videos": "Videos", "desktop": "Desktop",
    "home": "", "projects": "Desktop",
}


async def run_command(name: str, arg: str = "") -> str:
    """Execute one allowlisted system command; return a spoken confirmation."""
    name = (name or "").strip().lower()
    arg = (arg or "").strip()

    if name == "volume_up":
        return "Volume up." if await set_volume("up", _pct(arg)) else "Couldn't change the volume."
    if name == "volume_down":
        return "Volume down." if await set_volume("down", _pct(arg)) else "Couldn't change the volume."
    if name == "volume_set":
        n = _pct(arg, 50)
        return f"Volume set to {n} percent." if await set_volume("set", n) else "Couldn't change the volume."
    if name == "volume_get":
        vol, muted = await get_volume()
        if vol is None:
            return "I can't read the volume on this machine."
        return f"Volume is at {vol} percent" + (", muted." if muted else ".")
    if name == "mute":
        return "Muted." if await set_mute("1") else "Couldn't mute."
    if name == "unmute":
        return "Sound is back on." if await set_mute("0") else "Couldn't unmute."

    if name in ("play_pause", "play", "pause", "next", "previous", "stop"):
        if await media(name):
            return {"play_pause": "Done.", "play": "Playing.", "pause": "Paused.", "stop": "Stopped.",
                    "next": "Next track.", "previous": "Previous track."}[name]
        return "Nothing is playing that I can control."

    if name == "open_app":
        return await open_app(arg) if arg else "Which app?"
    if name == "open_folder":
        from actions import open_path
        key = arg.lower().strip()
        sub = _FOLDERS.get(key)
        path = Path.home() / sub if sub is not None else Path(arg).expanduser()
        return (await open_path(str(path)))["confirmation"]

    if name == "lock":
        return "Locking the screen." if await lock_screen() else "I couldn't lock the screen."
    if name in ("brightness_up", "brightness_down"):
        ok = await brightness("up" if name.endswith("up") else "down", _pct(arg))
        return "Done." if ok else "Brightness control isn't available here."

    if name == "battery":
        return battery_status()
    if name == "status":
        return system_status()
    if name == "notify":
        title, _, body = arg.partition("|||")
        await notify(title.strip() or "KIAROS", body.strip())
        return "Sent."

    if name in CONFIRM_REQUIRED:
        word = {"shutdown": "Shutting down", "restart": "Restarting", "suspend": "Suspending", "logout": "Logging out"}[name]
        asyncio.create_task(power(name))
        return f"{word} in a few seconds."

    return "That's not something I can control on this machine."
