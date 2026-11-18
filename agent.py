"""
KIAROS task agent — carries out a multi-step goal end to end with tools.

The model plans and calls tools (apps, system controls, shell, files, a
browser KIAROS drives, WhatsApp Web, phone actions, timers, notes) in a loop
until the goal is done, then reports in one or two spoken sentences.

Safety is enforced here, not by the model:
  - sending messages, non-read-only shell commands, overwriting files and
    power actions pause the task and wait for the user's spoken "yes";
  - sudo / destructive system commands are refused outright.
A paused task is resumed (or cancelled) by the next thing the user says.
"""

import asyncio
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

import llm

log = logging.getLogger("kiaros.agent")

MAX_STEPS = 15
HOME = Path.home()

SYSTEM = """You are KIAROS's task engine on the user's Linux laptop (Ubuntu, GNOME, Wayland). You carry out the
user's goal END TO END using the tools, then reply with a one- or two-sentence spoken summary of what you did.

How to work:
- Act, don't describe. Use tools until the goal is actually done, then stop and summarise.
- For anything on a website (WhatsApp Web, Gmail, YouTube, shopping, forms, search), use the browser_* tools:
  browser_open, then browser_read to see the page and its numbered elements, then browser_click / browser_type.
  Re-read after each change. The browser keeps the user's logins.
- To message someone on WhatsApp, use whatsapp_send with their name as it appears in WhatsApp. Write the message
  the way the user would send it (first person, natural, correct punctuation) — e.g. "send Somesh that how was he"
  means a message like "How was it?" or "How are you doing?" only if clearly intended; otherwise keep their words.
- Use run_command for files, system info, git, installs that don't need sudo. Never use sudo.
- If a tool reports NEEDS_LOGIN or NEEDS_CONFIRMATION, stop and tell the user exactly what is needed.
- If you get stuck after a few attempts, say what blocked you. Never claim something happened unless a tool
  result confirms it.
- Final reply: plain speech, no markdown, no lists, at most two sentences."""

# Commands that only read state — run without asking.
_READ_ONLY = {
    "ls", "cat", "head", "tail", "wc", "df", "du", "free", "uptime", "date", "whoami", "uname", "ps", "pwd",
    "which", "whereis", "file", "stat", "find", "grep", "rg", "echo", "lsblk", "lscpu", "lsusb", "hostname",
    "env", "printenv", "id", "groups", "cal", "nproc", "sensors", "tree", "realpath", "basename", "dirname",
    "sort", "uniq", "cut", "awk", "sed", "less", "more", "diff", "md5sum", "sha256sum", "xdg-user-dir",
}
_READ_ONLY_SUB = {"git": {"status", "log", "diff", "branch", "show", "remote"},
                  "systemctl": {"status", "list-units", "is-active"}, "nmcli": {"general", "device", "connection"},
                  "ip": {"addr", "a", "route", "link"}, "pip": {"list", "show"}, "npm": {"ls", "list"},
                  "apt": {"list", "show", "search"}, "snap": {"list"}, "flatpak": {"list"}}
_FORBIDDEN = re.compile(r"(^|[\s;&|])(sudo|su|mkfs\S*|fdisk|parted|shred|dd)(\s|$)|rm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)(\s|$)|:\(\)\s*\{", re.I)


def classify_command(cmd: str) -> str:
    """'forbidden', 'read' (safe to run) or 'confirm'."""
    if _FORBIDDEN.search(cmd):
        return "forbidden"
    if re.search(r"[><`]|\$\(|&&|\|\||;", cmd):
        return "confirm"
    for segment in cmd.split("|"):
        try:
            words = shlex.split(segment)
        except ValueError:
            return "confirm"
        if not words:
            return "confirm"
        head = os.path.basename(words[0])
        if head in _READ_ONLY:
            if head == "sed" and any(w.startswith("-i") for w in words):
                return "confirm"
            if head == "find" and any(w in ("-delete", "-exec", "-execdir") for w in words):
                return "confirm"
            continue
        subs = _READ_ONLY_SUB.get(head)
        if subs and len(words) > 1 and words[1] in subs:
            continue
        return "confirm"
    return "read"


def _tool(name, description, props=None, required=None):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props or {}, "required": required or []}}


S = {"type": "string"}
TOOLS = [
    _tool("open_app", "Open a desktop application by name (e.g. 'files', 'vs code', 'spotify', 'terminal', 'settings').",
          {"name": S}, ["name"]),
    _tool("open_in_browser", "Open a URL or file in the user's normal browser/viewer for them to look at (not automated).",
          {"url": S}, ["url"]),
    _tool("system_control", "Laptop controls. command is one of: volume_up, volume_down, volume_set, volume_get, mute, "
          "unmute, play_pause, play, pause, next, previous, lock, brightness_up, brightness_down, battery, status, "
          "notify, open_folder, suspend, restart, shutdown. arg is optional (e.g. a percentage or folder name).",
          {"command": S, "arg": S}, ["command"]),
    _tool("run_command", "Run a bash command on the laptop (home directory, 60s limit) and get its output. "
          "Read-only commands run immediately; anything that changes files or the system asks the user first. No sudo.",
          {"command": S}, ["command"]),
    _tool("read_file", "Read a text file (path may start with ~).", {"path": S}, ["path"]),
    _tool("write_file", "Create or overwrite a text file with the given content.", {"path": S, "content": S},
          ["path", "content"]),
    _tool("list_dir", "List a directory (path may start with ~).", {"path": S}, ["path"]),
    _tool("browser_open", "Open a URL in the browser KIAROS controls (keeps the user's logins).", {"url": S}, ["url"]),
    _tool("browser_read", "Get the current page's URL, visible text and numbered interactive elements."),
    _tool("browser_click", "Click an element: its number from browser_read, or its visible text/label.",
          {"target": S}, ["target"]),
    _tool("browser_type", "Type text into a field (number from browser_read, or its label/placeholder). "
          "Set enter to 'yes' to press Enter after typing.", {"target": S, "text": S, "enter": S}, ["target", "text"]),
    _tool("whatsapp_send", "Send a WhatsApp message via WhatsApp Web to a contact or group by name.",
          {"contact": S, "message": S}, ["contact", "message"]),
    _tool("phone_action", "Do something on the user's phone: action is call, sms, whatsapp, navigate or open_url; "
          "target is a contact name, number, place or URL; message is for sms/whatsapp.",
          {"action": S, "target": S, "message": S}, ["action", "target"]),
    _tool("set_timer", "Set a timer/reminder that rings on all devices.", {"seconds": {"type": "integer"}, "label": S},
          ["seconds"]),
    _tool("create_note", "Save a Markdown note for the user.", {"title": S, "body": S}, ["title", "body"]),
]


@dataclass
class Pending:
    """A task paused on a step that needs the user's go-ahead."""
    goal: str
    messages: list
    uses: list             # tool_use blocks from the paused turn
    results: list          # results gathered before the paused step
    index: int             # which use is waiting
    question: str
    created: float = field(default_factory=time.time)


class TaskAgent:
    def __init__(self, hooks: dict):
        """hooks: async callables provided by the server — phone(target_str, ws), timer(seconds, label),
        note(title, body), progress(ws, text)."""
        self.hooks = hooks
        self.pending: Pending | None = None

    # -- tool execution -----------------------------------------------------

    def _needs_confirmation(self, name: str, args: dict) -> str | None:
        if name == "whatsapp_send":
            return f"Send \"{args.get('message', '')}\" to {args.get('contact', '')} on WhatsApp?"
        if name == "phone_action" and str(args.get("action", "")).lower() in ("sms", "whatsapp"):
            return f"Send \"{args.get('message', '')}\" to {args.get('target', '')} from your phone?"
        if name == "run_command" and classify_command(args.get("command", "")) == "confirm":
            return f"Run this on the laptop: {args.get('command', '')}?"
        if name == "write_file" and Path(os.path.expanduser(args.get("path", ""))).exists():
            return f"Overwrite {args.get('path')}?"
        if name == "system_control" and args.get("command") in ("shutdown", "restart", "suspend", "logout"):
            return f"{args['command'].capitalize()} the laptop?"
        return None

    async def execute(self, name: str, args: dict, ws) -> str:
        import system_control
        from actions import open_browser, open_path
        from web_agent import web
        try:
            if name == "open_app":
                return await system_control.run_command("open_app", args.get("name", ""))
            if name == "open_in_browser":
                url = args.get("url", "")
                if url.startswith(("/", "~")):
                    return (await open_path(os.path.expanduser(url)))["confirmation"]
                return (await open_browser(url if "://" in url else "https://" + url))["confirmation"]
            if name == "system_control":
                return await system_control.run_command(args.get("command", ""), str(args.get("arg", "")))
            if name == "run_command":
                return await self._run_shell(args.get("command", ""))
            if name == "read_file":
                p = Path(os.path.expanduser(args.get("path", "")))
                return p.read_text(errors="replace")[:8000] if p.is_file() else f"No file at {p}"
            if name == "write_file":
                p = Path(os.path.expanduser(args.get("path", "")))
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(args.get("content", ""))
                return f"Wrote {len(args.get('content', ''))} characters to {p}"
            if name == "list_dir":
                p = Path(os.path.expanduser(args.get("path", "~") or "~"))
                if not p.is_dir():
                    return f"No folder at {p}"
                items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:200]
                return "\n".join(f"{'[dir] ' if i.is_dir() else ''}{i.name}" for i in items) or "(empty)"
            if name == "browser_open":
                return await web.open(args.get("url", ""))
            if name == "browser_read":
                return await web.read()
            if name == "browser_click":
                return await web.click(str(args.get("target", "")))
            if name == "browser_type":
                enter = str(args.get("enter", "")).lower() in ("yes", "true", "1")
                return await web.type(str(args.get("target", "")), args.get("text", ""), enter)
            if name == "whatsapp_send":
                return await web.whatsapp_send(args.get("contact", ""), args.get("message", ""))
            if name == "phone_action":
                target = f"{args.get('action', '')} ||| {args.get('target', '')} ||| {args.get('message', '')}"
                failure = await self.hooks["phone"](target, ws)
                return failure or f"Sent {args.get('action')} to the phone."
            if name == "set_timer":
                await self.hooks["timer"](int(args.get("seconds", 60)), args.get("label", ""))
                return "Timer set."
            if name == "create_note":
                ok = await self.hooks["note"](args.get("title", "Note"), args.get("body", ""))
                return "Note saved." if ok else "Couldn't save the note."
            return f"Unknown tool {name}"
        except Exception as e:
            log.warning(f"Tool {name} failed: {e}")
            return f"ERROR: {type(e).__name__}: {str(e)[:300]}"

    async def _run_shell(self, cmd: str) -> str:
        kind = classify_command(cmd)
        if kind == "forbidden":
            return "REFUSED: that command needs sudo or could damage the system. Ask the user to run it themselves."
        proc = await asyncio.create_subprocess_exec(
            "bash", "-lc", cmd, cwd=str(HOME), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            return "Command timed out after 60 seconds."
        text = out.decode(errors="replace")
        return f"exit code {proc.returncode}\n{text[-6000:]}" if text else f"exit code {proc.returncode} (no output)"

    # -- the loop -----------------------------------------------------------

    async def run(self, client, goal: str, ws, context: str = "") -> str:
        self.pending = None
        messages = [{"role": "user", "content": f"{goal}\n\n(Context: {context})" if context else goal}]
        return await self._loop(client, goal, messages, ws)

    async def _loop(self, client, goal: str, messages: list, ws, start_step: int = 0) -> str:
        for step in range(start_step, MAX_STEPS):
            resp = await client.messages.create(
                model=llm.DEEP_MODEL if llm.provider_of(client) == "anthropic" else llm.FAST_MODEL,
                max_tokens=4096, system=SYSTEM, messages=messages, tools=TOOLS)
            messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if not uses:
                return llm.text_of(resp) or "Done."
            outcome = await self._run_uses(client, goal, messages, uses, [], 0, ws)
            if outcome is not None:
                return outcome  # paused for confirmation
        return "I took a lot of steps without finishing, so I stopped. Tell me how you'd like to continue."

    async def _run_uses(self, client, goal, messages, uses, results, start, ws) -> str | None:
        for i in range(start, len(uses)):
            u = uses[i]
            args = dict(u.input or {})
            question = self._needs_confirmation(u.name, args)
            if question:
                self.pending = Pending(goal, messages, uses, results, i, question)
                return question + " Say yes to go ahead."
            log.info(f"Agent tool {u.name}({str(args)[:120]})")
            out = await self.execute(u.name, args, ws)
            if out.startswith("NEEDS_LOGIN"):
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": out})
                for rest in uses[i + 1:]:
                    results.append({"type": "tool_result", "tool_use_id": rest.id, "content": "Not run."})
                messages.append({"role": "user", "content": results})
                return out.removeprefix("NEEDS_LOGIN: ")
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": out[:6000]})
        messages.append({"role": "user", "content": results})
        return None

    async def resume(self, client, approved: bool, ws) -> str:
        """Continue a paused task after the user said yes (or no)."""
        p, self.pending = self.pending, None
        if p is None:
            return "There's nothing waiting for approval."
        u = p.uses[p.index]
        if approved:
            log.info(f"Agent tool {u.name} approved by user")
            out = await self.execute(u.name, dict(u.input or {}), ws)
        else:
            out = "The user said no. Do not do this; adjust or stop."
        p.results.append({"type": "tool_result", "tool_use_id": u.id, "content": out[:6000]})
        paused = await self._run_uses(client, p.goal, p.messages, p.uses, p.results, p.index + 1, ws)
        if paused is not None:
            return paused
        if not approved:
            return "Okay, I won't do that."
        return await self._loop(client, p.goal, p.messages, ws, start_step=1)
