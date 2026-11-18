"""
KIAROS Action Executor — Linux desktop actions (terminal, browser, folders, Claude Code).

Each public coroutine returns {"success": bool, "confirmation": str}.
Programs are launched detached so the server never blocks on them.
"""

import asyncio
import logging
import os
import re
import shlex
import shutil
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("kiaros.actions")

DESKTOP_PATH = Path.home() / "Desktop"
PROJECTS_DIR = Path(os.getenv("KIAROS_PROJECTS_DIR", str(DESKTOP_PATH))).expanduser()


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

async def spawn_detached(*cmd: str, cwd: str | None = None) -> bool:
    """Start a GUI program detached from the server process."""
    if not cmd or not shutil.which(cmd[0]):
        log.warning(f"Program not found: {cmd[0] if cmd else '(empty)'}")
        return False
    try:
        await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as e:
        log.error(f"Failed to launch {cmd[0]}: {e}")
        return False


def _terminal_command(shell_cmd: str, cwd: str, title: str) -> list[str] | None:
    """Build an argv that opens a new terminal window running shell_cmd."""
    # Keep the window open after the command finishes so the user can read it.
    script = f"{shell_cmd}; echo; exec bash" if shell_cmd else "exec bash"
    preferred = os.getenv("KIAROS_TERMINAL", "").strip()
    candidates = [preferred] if preferred else []
    candidates += ["gnome-terminal", "ptyxis", "kgx", "konsole", "xfce4-terminal", "alacritty", "kitty", "xterm"]
    for term in candidates:
        if not term or not shutil.which(term):
            continue
        if term == "gnome-terminal":
            return [term, f"--title={title}", f"--working-directory={cwd}", "--", "bash", "-lc", script]
        if term == "ptyxis":
            return [term, "--new-window", "-d", cwd, "--", "bash", "-lc", script]
        if term == "kgx":
            return [term, f"--working-directory={cwd}", "--", "bash", "-lc", script]
        if term == "konsole":
            return [term, "--workdir", cwd, "-e", "bash", "-lc", script]
        if term == "xfce4-terminal":
            return [term, f"--working-directory={cwd}", f"--title={title}", "-x", "bash", "-lc", script]
        if term in ("alacritty", "kitty"):
            return [term, "--working-directory" if term == "alacritty" else "--directory", cwd, "-e", "bash", "-lc", script]
        return [term, "-T", title, "-e", "bash", "-lc", f"cd {shlex.quote(cwd)} && {script}"]
    return None


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

async def open_terminal(command: str = "", cwd: str | None = None, title: str = "KIAROS") -> dict:
    """Open a terminal window, optionally running a command."""
    cwd = cwd or str(Path.home())
    argv = _terminal_command(command, cwd, title)
    success = bool(argv) and await spawn_detached(*argv)
    return {
        "success": success,
        "confirmation": "Terminal is open." if success else "I couldn't find a terminal program to open.",
    }


# ---------------------------------------------------------------------------
# Browser / files
# ---------------------------------------------------------------------------

def _browser_argv(url: str, browser: str) -> list[str]:
    b = (browser or "").lower()
    if b == "firefox" and shutil.which("firefox"):
        return ["firefox", "--new-tab", url]
    if b in ("chrome", "google-chrome"):
        for exe in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            if shutil.which(exe):
                return [exe, url]
    return ["xdg-open", url]


async def open_browser(url: str, browser: str = "default") -> dict:
    """Open a URL in the requested browser (default: the system default browser)."""
    success = await spawn_detached(*_browser_argv(url, browser))
    name = {"firefox": "Firefox", "chrome": "Chrome"}.get(browser.lower(), "your browser")
    return {
        "success": success,
        "confirmation": f"Pulled that up in {name}." if success else f"{name} ran into a problem.",
    }


async def open_path(path: str) -> dict:
    """Open a file or folder with its default application."""
    p = Path(path).expanduser()
    if not p.exists():
        return {"success": False, "confirmation": f"I couldn't find {p.name}."}
    success = await spawn_detached("xdg-open", str(p))
    return {"success": success, "confirmation": f"Opened {p.name}." if success else f"Couldn't open {p.name}."}


def search_url(query: str) -> str:
    return f"https://www.google.com/search?q={quote(query)}"


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def claude_available() -> bool:
    return shutil.which("claude") is not None


async def open_claude_in_project(project_dir: str, prompt: str = "") -> dict:
    """Open a terminal in project_dir running Claude Code interactively.

    The task goes into CLAUDE.md, which Claude Code reads on startup, so no
    prompt escaping is needed.
    """
    if not claude_available():
        return {"success": False, "confirmation": "The Claude Code CLI isn't installed on this laptop."}
    if prompt:
        (Path(project_dir) / "CLAUDE.md").write_text(
            f"# Task\n\n{prompt}\n\nBuild this completely. If web app, make index.html work standalone.\n"
        )
    result = await open_terminal("claude --dangerously-skip-permissions", cwd=project_dir, title=Path(project_dir).name)
    result["confirmation"] = (
        "Claude Code is running in a terminal. You can watch the progress."
        if result["success"] else "Had trouble starting Claude Code."
    )
    return result


async def run_claude_visible(project_dir: str, prompt_file: str, output_file: str = "") -> dict:
    """Open a terminal that pipes prompt_file into `claude -p` so the user can watch."""
    if not claude_available():
        return {"success": False, "confirmation": "The Claude Code CLI isn't installed on this laptop."}
    cmd = f"cat {shlex.quote(prompt_file)} | claude -p --dangerously-skip-permissions"
    if output_file:
        cmd += f" | tee {shlex.quote(output_file)}; echo '--- KIAROS TASK COMPLETE ---' >> {shlex.quote(output_file)}"
    return await open_terminal(cmd, cwd=project_dir, title=Path(project_dir).name)


# ---------------------------------------------------------------------------
# Project naming
# ---------------------------------------------------------------------------

def _generate_project_name(prompt: str) -> str:
    """Generate a kebab-case project folder name from the prompt."""
    quoted = re.search(r'"([^"]+)"', prompt)
    if quoted:
        name = re.sub(r"[^a-zA-Z0-9\s-]", "", quoted.group(1)).strip()
        if name:
            return re.sub(r"\s+", "-", name.lower())

    called = re.search(r'(?:called|named)\s+(\S+(?:[-_]\S+)*)', prompt, re.IGNORECASE)
    if called:
        name = re.sub(r"[^a-zA-Z0-9-]", "", called.group(1))
        if len(name) > 3:
            return name.lower()

    words = re.sub(r"[^a-zA-Z0-9\s]", "", prompt.lower()).split()
    skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and",
            "to", "of", "i", "want", "need", "new", "project", "directory", "called",
            "on", "desktop", "that", "application", "app", "full", "stack", "simple",
            "web", "page", "site", "named"}
    meaningful = [w for w in words if w not in skip and len(w) > 2][:4]
    return "-".join(meaningful) if meaningful else "kiaros-project"


def project_path(name: str) -> Path:
    path = PROJECTS_DIR / name
    path.mkdir(parents=True, exist_ok=True)
    return path
