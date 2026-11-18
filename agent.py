"""
KIAROS task agent — carries out a multi-step goal end to end with tools.

The model plans and calls tools (apps, system controls, shell, files, a
browser KIAROS drives, WhatsApp Web, phone actions, timers, notes) in a loop
until the goal is done, then reports in one or two spoken sentences.

It works autonomously and stops only for MAJOR permissions (enforced here,
not by the model):
  - deleting files, overwriting existing files, killing processes, pushing or
    resetting git, changing system services, piping internet scripts to a shell;
  - payments / orders / account deletion and similar irreversible website clicks;
  - messaging someone the user didn't name in their request;
  - shutdown / restart / suspend / logout.
sudo and disk-destroying commands are refused outright. When a step needs the
user to act (scan a QR code, type a password), the agent waits or pauses and
continues by itself when the user says "continue".
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import llm

log = logging.getLogger("kiaros.agent")

MAX_STEPS = 30
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
- You are autonomous: finish the whole job without asking the user questions. Pick sensible defaults yourself.
  The system itself will pause for the user's permission on major or irreversible steps — just call the tool.
- If a tool reports NEEDS_USER or NEEDS_LOGIN, stop and tell the user exactly what to do; you'll be resumed after.
- Verify your work (e.g. re-read the page, list the folder) before saying it's done.
- If you get stuck after a few attempts, say what blocked you. Never claim something happened unless a tool
  result confirms it.
- Final reply: plain speech, no markdown, no lists, at most two sentences."""

_FORBIDDEN = re.compile(
    r"(^|[\s;&|(])(sudo|su|pkexec|doas|mkfs\S*|fdisk|sfdisk|parted|wipefs|shred|dd)(\s|$)"
    r"|rm\s+(-\S*\s+)*(/|~/?|\$HOME/?|/home/?\S*|/\*)(\s|$)"      # rm on / or the whole home folder
    r"|:\(\)\s*\{|>\s*/dev/sd|chmod\s+-R\s+\S+\s+/(\s|$)", re.I)

# Major (irreversible or system-changing) — the user is asked first. Everything else just runs.
_MAJOR = [
    r"(^|[\s;&|(])(rm|rmdir|unlink|trash|gio\s+trash)\s",                 # deleting
    r"(^|[\s;&|(])(kill|pkill|killall|xkill)\s",                          # killing processes
    r"(^|[\s;&|(])(shutdown|reboot|poweroff|halt|systemctl\s+(poweroff|reboot|suspend|hibernate))\b",
    r"systemctl\s+(--user\s+)?(stop|disable|mask|restart|enable)\s",
    r"\bgit\s+(push|reset\s+--hard|clean\s+-\S*f|checkout\s+--\s|restore|rebase|branch\s+-D|stash\s+(drop|clear))",
    r"(curl|wget)\s[^|]*\|\s*(ba|z|)sh\b",                                # piping internet scripts to a shell
    r"\bfind\b.*\s-(delete|exec\s+rm)",
    r"\bsed\s+-i\b.*\s(~/\.|/home/\S+/\.|/etc/)",                        # editing dotfiles / system config in place
    r">\s*(~/\.|\$HOME/\.|/home/\S+/\.|/etc/)",                            # overwriting dotfiles / system config
    r"\bcrontab\s+-r\b|\bchmod\s+-R\b|\bchown\s+-R\b",
    r"\b(docker|podman)\s+(rm|rmi|system\s+prune|volume\s+rm)\b",
    r"\btruncate\s",
    r"\bmv\s",                                                               # moving/renaming can overwrite files
]


def classify_command(cmd: str) -> str:
    """'forbidden', 'major' (ask the user first) or 'ok' (just run it)."""
    if _FORBIDDEN.search(cmd):
        return "forbidden"
    if any(re.search(p, cmd, re.I) for p in _MAJOR):
        return "major"
    # "> file" replaces an existing file's contents (">>" appends, which is fine).
    for target in re.findall(r"(?<![>&\d])>(?!>)\s*([^\s;&|]+)", cmd):
        path = Path(os.path.expanduser(target.strip("'\""))) if not target.startswith("/") else Path(target)
        if not path.is_absolute():
            path = HOME / path
        if path.exists() and path.stat().st_size > 0 and str(path) != "/dev/null":
            return "major"
    return "ok"


# Website clicks that spend money or can't be undone.
_MAJOR_CLICK = re.compile(
    r"\b(pay|payment|buy|purchase|place (your )?order|checkout|confirm (order|booking|purchase)|book now|"
    r"subscribe|donate|transfer|send money|delete|remove account|deactivate|close account|unsubscribe all|"
    r"submit (application|payment)|sign contract|accept and pay)\b", re.I)


def _named_in(name: str, text: str) -> bool:
    """Did the user's own request mention this person/contact?"""
    name, text = (name or "").lower().strip(), (text or "").lower()
    if not name:
        return False
    if name in text:
        return True
    first = re.split(r"\s+", name)[0]
    return len(first) >= 3 and re.search(rf"\b{re.escape(first)}\b", text) is not None


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
        self.waiting: dict | None = None  # paused because the user must act (login, password)

    # -- tool execution -----------------------------------------------------

    async def _needs_confirmation(self, name: str, args: dict, goal: str) -> str | None:
        """A question for the user if this step is a MAJOR permission, else None (just do it)."""
        if name == "whatsapp_send" and not _named_in(args.get("contact", ""), goal):
            return f"Send \"{args.get('message', '')}\" to {args.get('contact', '')} on WhatsApp? You didn't mention them."
        if name == "phone_action" and str(args.get("action", "")).lower() in ("sms", "whatsapp", "call") \
                and not _named_in(args.get("target", ""), goal):
            return f"{args.get('action', '').capitalize()} {args.get('target', '')} from your phone? You didn't mention them."
        if name == "run_command":
            kind = classify_command(args.get("command", ""))
            if kind == "major":
                return f"This step needs your permission: {args.get('command', '')}. Go ahead?"
        if name == "write_file":
            p = Path(os.path.expanduser(args.get("path", "")))
            if p.exists() and p.stat().st_size > 0 and str(p).lower() not in goal.lower():
                return f"Overwrite the existing file {p.name}?"
        if name == "system_control" and args.get("command") in ("shutdown", "restart", "suspend", "logout"):
            return f"{args['command'].capitalize()} the laptop?"
        if name == "browser_click":
            from web_agent import web
            info = await web.describe(str(args.get("target", "")))
            if _MAJOR_CLICK.search(info.get("label", "")) or _MAJOR_CLICK.search(str(args.get("target", ""))):
                return f"Click \"{info.get('label') or args.get('target')}\" on the website? That can't be undone."
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
                info = await web.describe(str(args.get("target", "")))
                if info.get("password"):
                    return ("NEEDS_USER: This site wants a password or code. Please type it yourself in the KIAROS "
                            "browser window and sign in, then say continue.")
                enter = str(args.get("enter", "")).lower() in ("yes", "true", "1")
                return await web.type(str(args.get("target", "")), args.get("text", ""), enter)
            if name == "whatsapp_send":
                say = self.hooks.get("say")
                return await web.whatsapp_send(args.get("contact", ""), args.get("message", ""),
                                               on_wait=(lambda t: say(ws, t)) if say else None)
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
        self.waiting = None
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
            question = await self._needs_confirmation(u.name, args, goal)
            if question:
                self.pending = Pending(goal, messages, uses, results, i, question)
                return question + " Say yes to go ahead."
            log.info(f"Agent tool {u.name}({str(args)[:120]})")
            out = await self.execute(u.name, args, ws)
            if out.startswith(("NEEDS_LOGIN", "NEEDS_USER")):
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": out})
                for rest in uses[i + 1:]:
                    results.append({"type": "tool_result", "tool_use_id": rest.id, "content": "Not run yet."})
                messages.append({"role": "user", "content": results})
                self.waiting = {"goal": goal, "messages": messages, "time": time.time()}
                return re.sub(r"^NEEDS_(LOGIN|USER):\s*", "", out)
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": out[:6000]})
        messages.append({"role": "user", "content": results})
        return None

    async def continue_after_user(self, client, ws) -> str:
        """The user did what was needed (scanned the QR code, logged in): carry on with the task."""
        w, self.waiting = self.waiting, None
        if w is None:
            return "There's no task waiting on you."
        w["messages"].append({"role": "user", "content": "I've done that. Continue the task from where you stopped."})
        return await self._loop(client, w["goal"], w["messages"], ws, start_step=1)

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
