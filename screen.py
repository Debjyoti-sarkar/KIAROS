"""
KIAROS Screen Awareness (Linux).

1. Window list: wmctrl on X11. GNOME on Wayland does not expose other apps'
   windows, so on Wayland this returns [] and KIAROS relies on screenshots.
2. Screenshot: the first available of gnome-screenshot, grim, spectacle,
   scrot, or ImageMagick `import` → Claude vision describes it.
"""

import asyncio
import base64
import logging
import os
import shutil
import tempfile
from pathlib import Path

import llm

log = logging.getLogger("kiaros.screen")


async def _run(*cmd: str, timeout: float = 10.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, out.decode(errors="replace")


async def get_active_windows() -> list[dict]:
    """Return [{"app", "title", "frontmost"}] where the session allows it (X11)."""
    if not shutil.which("wmctrl") or os.getenv("XDG_SESSION_TYPE") == "wayland":
        return []
    try:
        code, out = await _run("wmctrl", "-lx", timeout=5)
        if code != 0:
            return []
        active = ""
        if shutil.which("xdotool"):
            c2, a = await _run("xdotool", "getactivewindow", "getwindowname", timeout=3)
            active = a.strip() if c2 == 0 else ""
        windows = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 5:
                app = parts[2].split(".")[-1]
                title = parts[4].strip()
                windows.append({"app": app, "title": title, "frontmost": title == active})
        return windows
    except Exception as e:
        log.debug(f"wmctrl failed: {e}")
        return []


def screenshot_tool() -> list[str] | None:
    for tool, argv in (
        ("gnome-screenshot", ["gnome-screenshot", "-f", "{out}"]),
        ("grim", ["grim", "{out}"]),
        ("spectacle", ["spectacle", "-b", "-n", "-f", "-o", "{out}"]),
        ("scrot", ["scrot", "-o", "{out}"]),
        ("import", ["import", "-window", "root", "{out}"]),
    ):
        if shutil.which(tool):
            return argv
    return None


async def take_screenshot() -> str | None:
    """Capture the screen; return base64 PNG or None."""
    argv = screenshot_tool()
    if not argv:
        return None
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        code, _ = await _run(*[a.replace("{out}", tmp) for a in argv], timeout=15)
        data = Path(tmp).read_bytes() if Path(tmp).exists() else b""
        if code != 0 or not data:
            return None
        return base64.b64encode(data).decode()
    except Exception as e:
        log.warning(f"Screenshot failed: {e}")
        return None
    finally:
        Path(tmp).unlink(missing_ok=True)


async def describe_screen(client) -> str:
    """Describe what's on screen: screenshot + vision, else window list."""
    shot = await take_screenshot()
    if shot and client:
        try:
            response = await llm.fast(
                client,
                [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": shot}},
                    {"type": "text", "text": "What's on my screen right now?"},
                ]}],
                system=(
                    "You are KIAROS looking at a screenshot of the user's Linux desktop. "
                    "Say which apps are open and what the user appears to be working on. "
                    "Be specific about files, URLs, code or documents visible. 2-3 sentences, no markdown."
                ),
                max_tokens=1024,
            )
            text = llm.text_of(response)
            if text:
                return text
        except Exception as e:
            log.warning(f"Vision call failed: {e}")

    windows = await get_active_windows()
    if windows:
        active = next((w for w in windows if w["frontmost"]), None)
        apps = sorted({w["app"] for w in windows})
        result = f"You have {', '.join(apps)} open."
        if active:
            result += f" Focused on {active['app']}: {active['title']}."
        return result
    if not screenshot_tool():
        return "I can't see the screen yet. Install gnome-screenshot and I'll be able to look."
    return "I couldn't capture the screen just now."


def format_windows_for_context(windows: list[dict]) -> str:
    if not windows:
        return ""
    lines = ["Currently open on the laptop:"]
    for w in windows:
        lines.append(f"  - {w['app']}: {w['title']}" + (" (active)" if w["frontmost"] else ""))
    return "\n".join(lines)
