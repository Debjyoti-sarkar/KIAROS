"""
KIAROS Server — voice AI assistant for your Linux laptop and Android phone.

Handles:
1. WebSocket voice interface (laptop browser / phone app <-> LLM <-> TTS)
2. Laptop control (apps, volume, media, lock, power) and phone actions (call, SMS, maps)
3. Claude Code task orchestration (spawn/manage claude -p sessions)
4. REST API for settings, tasks, pairing
"""

import asyncio
import base64
import json
import logging
import os
import re
import socket
import sys
import time
from pathlib import Path

# Load .env file if present
from paths import DATA_DIR, ENV_FILE  # noqa: E402
_env_path = ENV_FILE
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import anthropic
import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import auth
import llm
import system_control
from actions import (
    open_terminal, open_browser, open_path, open_claude_in_project, run_claude_visible,
    _generate_project_name, project_path, search_url, claude_available, PROJECTS_DIR,
)
from work_mode import WorkSession, is_casual_question
from screen import get_active_windows, describe_screen, format_windows_for_context
from calendar_access import (
    get_todays_events, format_events_for_context, format_schedule_summary,
    refresh_cache as refresh_calendar_cache, is_configured as calendar_configured,
)
from mail_access import (
    get_unread_count, get_unread_messages, format_unread_summary, is_configured as mail_configured,
)
from memory import (
    remember, get_open_tasks, create_task, complete_task,
    create_note, build_memory_context, format_tasks_for_voice, extract_memories,
    get_important_memories, save_contact, find_contact,
)
from notes_access import read_note, create_local_note, notes_dir
from dispatch_registry import DispatchRegistry
from planner import TaskPlanner, BYPASS_PHRASES
from qa import QAAgent
from tracking import SuccessTracker
from suggestions import suggest_followup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("kiaros")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FISH_API_KEY = os.getenv("FISH_API_KEY", "")
FISH_VOICE_ID = os.getenv("FISH_VOICE_ID", "")  # empty = Fish Audio's default voice
FISH_API_URL = "https://api.fish.audio/v1/tts"
USER_NAME = os.getenv("USER_NAME", "")
HONORIFIC = os.getenv("HONORIFIC", "sir")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
HTTP_PORT = int(os.getenv("KIAROS_PORT", "8340"))
HTTPS_PORT = int(os.getenv("KIAROS_HTTPS_PORT", "8341"))
REPORTS_DIR = DATA_DIR / "reports"

KIAROS_SYSTEM_PROMPT = """\
You are KIAROS — Knowledge-Integrated Autonomous Responsive Operating System — {user_name}'s personal AI assistant. You live on {user_name}'s Linux laptop and are reachable from their Android phone.

VOICE & PERSONALITY:
- Calm, precise, quietly witty. Composed like a seasoned chief of staff.
- Address the user as "{honorific}" now and then — not every sentence.
- Never say "How can I help you?" or "Is there anything else?" — just act.
- Deliver bad news calmly and plainly. Economy of language; no filler, no corporate-speak.
- When things go wrong, get calmer, not more alarmed.

TIME & WEATHER:
- Current time: {current_time}
- {weather_info}

WHERE THE USER IS:
{device_context}

YOUR CAPABILITIES (these are REAL and ACTIVE):
- LAPTOP CONTROL via [ACTION:SYSTEM]: open apps and folders, volume, mute, media play/pause/next, brightness, lock screen, battery and system status, desktop notifications, and (with confirmation) suspend, restart, shut down.
- PHONE ACTIONS via [ACTION:PHONE]: place a call, send an SMS, open WhatsApp, navigate in Maps, open a URL — executed on the user's phone.
- Open the browser and search or visit any URL.
- Open a terminal and spawn Claude Code to build or change software projects.
- Deep research with live web search, delivered as a report.
- Timers and reminders that ring on every connected device.
- Read the calendar (read-only), read email (READ-ONLY — you can never send, delete or modify email).
- Create Markdown notes and read existing ones.
- Manage tasks, remember facts, and store contacts for calling/texting.
- See the laptop screen (screenshot + vision) when asked.
- You are the KIAROS project at {project_dir}. If asked about your own code, use [ACTION:PROMPT_PROJECT] kiaros ||| <question>.

DAY PLANNING:
When asked to plan the day: use the SCHEDULE and ACTIVE TASKS below, ask about priorities, suggest time blocks, then use [ACTION:ADD_TASK] for agreed tasks and [ACTION:CREATE_NOTE] to save the plan. Keep it conversational.

BUILD PLANNING:
- When the user wants to BUILD something new, ask 1-2 quick questions first unless they say "just build it" (then default to React + Tailwind).
- Confirm the plan in one sentence, then dispatch [ACTION:BUILD] with a detailed description.
- The DISPATCHES section shows builds in progress and recently finished. For "status"/"where are we", read DISPATCHES — don't re-dispatch.
- NEVER invent progress or guess localhost ports; use the URL from DISPATCHES. To "show me", use [ACTION:BROWSE] with that URL.

RESPONSE LENGTH — CRITICAL:
One sentence is ideal; two is the maximum for the spoken part. No markdown, lists or code. Action tags at the end don't count.

BANNED PHRASES: "Absolutely", "Great question", "I'd be happy to", "Of course", "How can I help", "Is there anything else", "I apologize", "As an AI", "Let me know if", "Feel free to".
If something is genuinely beyond your reach, say so plainly in one sentence. Never pretend to have done something.

SPEECH-TO-TEXT CORRECTIONS: "cloud code"/"clock code" = Claude Code; "chaos", "kairos", "cairo's", "Kyros" = KIAROS.

ACTION SYSTEM — put exactly ONE tag at the END of your reply when something must be DONE:
- [ACTION:SYSTEM] command ||| argument — laptop control. Commands:
  volume_up / volume_down (arg: step %, optional), volume_set (arg: %), volume_get, mute, unmute,
  play_pause, play, pause, next, previous, stop,
  open_app (arg: app name, e.g. "spotify", "files", "vs code"), open_folder (arg: downloads/documents/pictures/music/videos/desktop/home or a path),
  lock, brightness_up, brightness_down, battery, status, notify (arg: title ||| body),
  suspend, restart, shutdown, logout (these ask the user to confirm first — just emit the tag).
  "turn it up a bit" → [ACTION:SYSTEM] volume_up ||| 10
  "open spotify on my laptop" → [ACTION:SYSTEM] open_app ||| spotify
- [ACTION:PHONE] action ||| target ||| message — runs on the phone. Actions: call, sms, whatsapp, navigate, open_url.
  target is a contact name or a phone number (or a place for navigate, a URL for open_url).
  "call mom" → [ACTION:PHONE] call ||| Mom
  "text Rahul I'm running late" → [ACTION:PHONE] sms ||| Rahul ||| I'm running late
  "take me to the airport" → [ACTION:PHONE] navigate ||| airport
- [ACTION:ADD_CONTACT] name ||| phone number — save a contact. "save mom's number 98765 43210" → [ACTION:ADD_CONTACT] Mom ||| 9876543210
- [ACTION:TIMER] seconds ||| label — set a timer/reminder. "remind me in 10 minutes to check the oven" → [ACTION:TIMER] 600 ||| check the oven
- [ACTION:BROWSE] url or search query — open the browser on the device the user is using.
- [ACTION:RESEARCH] detailed research brief — real research with live web search; a report opens when done.
- [ACTION:BUILD] description — build a new project with Claude Code on the laptop.
- [ACTION:OPEN_TERMINAL] — open a terminal with Claude Code on the laptop.
- [ACTION:PROMPT_PROJECT] project_name ||| prompt — work on, check, or resume an EXISTING project via Claude Code. Your most powerful action for code.
  "jump into my-app" → [ACTION:PROMPT_PROJECT] my-app ||| Summarize the current state and what to focus on next.
- [ACTION:SCREEN] — look at the laptop screen. Never use PROMPT_PROJECT for screen requests.
- [ACTION:ADD_TASK] priority ||| title ||| description ||| due_date (YYYY-MM-DD or empty)
- [ACTION:COMPLETE_TASK] task_id
- [ACTION:ADD_NOTE] topic ||| content — a quick internal memory note.
- [ACTION:CREATE_NOTE] title ||| body — create a Markdown note file the user can open.
- [ACTION:READ_NOTE] title search — read an existing note.
- [ACTION:REMEMBER] content — store an important fact about the user.

You use Claude Code as your hands, but YOU do the work: say "I built", "I found" — never "Claude Code did".
Do NOT use action tags for casual conversation, while the user is still explaining, or just because a URL was mentioned.

SCREEN:
{screen_context}

SCHEDULE:
{calendar_context}

EMAIL:
{mail_context}

ACTIVE TASKS:
{active_tasks}

DISPATCHES (if a recent result exists for a project, use it instead of re-dispatching):
{dispatch_context}

KNOWN PROJECTS:
{known_projects}
"""


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ClaudeTask:
    id: str
    prompt: str
    status: str = "pending"  # pending, running, completed, failed, cancelled
    working_dir: str = "."
    pid: Optional[int] = None
    result: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        d["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        d["elapsed_seconds"] = self.elapsed_seconds
        return d

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class TaskRequest(BaseModel):
    prompt: str
    working_dir: str = "."


# ---------------------------------------------------------------------------
# Claude Task Manager
# ---------------------------------------------------------------------------

class ClaudeTaskManager:
    """Manages background claude -p subprocesses."""

    def __init__(self, max_concurrent: int = 3):
        self._tasks: dict[str, ClaudeTask] = {}
        self._max_concurrent = max_concurrent
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._websockets: list[WebSocket] = []  # for push notifications

    def register_websocket(self, ws: WebSocket):
        if ws not in self._websockets:
            self._websockets.append(ws)

    def unregister_websocket(self, ws: WebSocket):
        if ws in self._websockets:
            self._websockets.remove(ws)

    async def _notify(self, message: dict):
        """Push a message to all connected WebSocket clients."""
        dead = []
        for ws in self._websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._websockets.remove(ws)

    async def spawn(self, prompt: str, working_dir: str = ".") -> str:
        """Spawn a claude -p subprocess. Returns task_id. Non-blocking."""
        active = await self.get_active_count()
        if active >= self._max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self._max_concurrent}) reached. "
                f"Wait for a task to complete or cancel one."
            )

        task_id = str(uuid.uuid4())[:8]
        task = ClaudeTask(
            id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            status="pending",
        )
        self._tasks[task_id] = task

        # Fire and forget — the background coroutine updates the task
        asyncio.create_task(self._run_task(task))
        log.info(f"Spawned task {task_id}: {prompt[:80]}...")

        await self._notify({
            "type": "task_spawned",
            "task_id": task_id,
            "prompt": prompt,
        })

        return task_id

    async def _run_task(self, task: ClaudeTask):
        """Open a terminal window and run Claude Code visibly."""
        task.status = "running"
        task.started_at = datetime.now()

        work_dir = task.working_dir
        if work_dir == "." or not work_dir:
            work_dir = str(project_path(_generate_project_name(task.prompt)))
            task.working_dir = work_dir

        prompt_file = Path(work_dir) / ".kiaros_prompt.md"
        prompt_file.write_text(task.prompt)
        output_file = Path(work_dir) / ".kiaros_output.txt"
        output_file.unlink(missing_ok=True)

        launched = await run_claude_visible(work_dir, str(prompt_file), str(output_file))
        if not launched["success"]:
            task.status = "failed"
            task.error = launched["confirmation"]
            task.completed_at = datetime.now()
            await self._notify({"type": "task_complete", "task_id": task.id, "status": task.status, "summary": task.error})
            return

        # Monitor the output file for completion
        output_file = Path(work_dir) / ".kiaros_output.txt"
        start = time.time()
        timeout = 600  # 10 minutes

        while time.time() - start < timeout:
            await asyncio.sleep(5)
            if output_file.exists():
                content = output_file.read_text()
                if "--- KIAROS TASK COMPLETE ---" in content:
                    task.result = content.replace("--- KIAROS TASK COMPLETE ---", "").strip()
                    task.status = "completed"
                    break
        else:
            task.status = "timed_out"
            task.error = f"Task timed out after {timeout}s"

        task.completed_at = datetime.now()

        # Notify via WebSocket
        await self._notify({
            "type": "task_complete",
            "task_id": task.id,
            "status": task.status,
            "summary": task.result[:200] if task.result else task.error,
        })

        # Clean up prompt file
        try:
            prompt_file.unlink()
        except:
            pass

        # Auto-QA on completed tasks
        if task.status == "completed":
            asyncio.create_task(self._run_qa(task))

    async def _run_qa(self, task: ClaudeTask, attempt: int = 1):
        """Run QA verification on a completed task, auto-retry on failure."""
        try:
            qa_result = await qa_agent.verify(task.prompt, task.result, task.working_dir)
            duration = task.elapsed_seconds

            if qa_result.passed:
                log.info(f"Task {task.id} passed QA: {qa_result.summary}")
                success_tracker.log_task("dev", task.prompt, True, attempt - 1, duration)
                await self._notify({
                    "type": "qa_result",
                    "task_id": task.id,
                    "passed": True,
                    "summary": qa_result.summary,
                })

                # Proactive suggestion after successful task
                suggestion = suggest_followup(
                    task_type="dev",
                    task_description=task.prompt,
                    working_dir=task.working_dir,
                    qa_result=qa_result,
                )
                if suggestion:
                    success_tracker.log_suggestion(task.id, suggestion.text)
                    await self._notify({
                        "type": "suggestion",
                        "task_id": task.id,
                        "text": suggestion.text,
                        "action_type": suggestion.action_type,
                        "action_details": suggestion.action_details,
                    })
            else:
                log.warning(f"Task {task.id} failed QA: {qa_result.issues}")
                if attempt < 3:
                    log.info(f"Auto-retrying task {task.id} (attempt {attempt + 1}/3)")
                    retry_result = await qa_agent.auto_retry(
                        task.prompt, qa_result.issues, task.working_dir, attempt,
                    )
                    if retry_result["status"] == "completed":
                        task.result = retry_result["result"]
                        # Re-verify
                        await self._run_qa(task, attempt + 1)
                    else:
                        success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                        await self._notify({
                            "type": "qa_result",
                            "task_id": task.id,
                            "passed": False,
                            "summary": f"Failed after {attempt + 1} attempts: {qa_result.issues}",
                        })
                else:
                    success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                    await self._notify({
                        "type": "qa_result",
                        "task_id": task.id,
                        "passed": False,
                        "summary": f"Failed QA after {attempt} attempts: {qa_result.issues}",
                    })
        except Exception as e:
            log.error(f"QA error for task {task.id}: {e}")

    async def get_status(self, task_id: str) -> Optional[ClaudeTask]:
        return self._tasks.get(task_id)

    async def list_tasks(self) -> list[ClaudeTask]:
        return list(self._tasks.values())

    async def get_active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("pending", "running"))

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False

        process = self._processes.get(task_id)
        if process:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
            except ProcessLookupError:
                pass

        task.status = "cancelled"
        task.completed_at = datetime.now()
        self._processes.pop(task_id, None)
        log.info(f"Cancelled task {task_id}")
        return True

    def get_active_tasks_summary(self) -> str:
        """Format active tasks for injection into the system prompt."""
        active = [t for t in self._tasks.values() if t.status in ("pending", "running")]
        completed_recent = [
            t for t in self._tasks.values()
            if t.status == "completed"
            and t.completed_at
            and (datetime.now() - t.completed_at).total_seconds() < 300
        ]

        if not active and not completed_recent:
            return "No active or recent tasks."

        lines = []
        for t in active:
            elapsed = f"{t.elapsed_seconds:.0f}s" if t.started_at else "queued"
            lines.append(f"- [{t.id}] RUNNING ({elapsed}): {t.prompt[:100]}")
        for t in completed_recent:
            lines.append(f"- [{t.id}] COMPLETED: {t.prompt[:60]} -> {t.result[:80]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------

async def scan_projects() -> list[dict]:
    """Quick scan of the projects folder for git repos (depth 1)."""
    projects = []
    desktop = PROJECTS_DIR

    if not desktop.exists():
        return projects

    try:
        for entry in sorted(desktop.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            git_dir = entry / ".git"
            if git_dir.exists():
                branch = "unknown"
                head_file = git_dir / "HEAD"
                try:
                    head_content = head_file.read_text().strip()
                    if head_content.startswith("ref: refs/heads/"):
                        branch = head_content.replace("ref: refs/heads/", "")
                except Exception:
                    pass

                projects.append({
                    "name": entry.name,
                    "path": str(entry),
                    "branch": branch,
                })
    except PermissionError:
        pass

    return projects


def format_projects_for_prompt(projects: list[dict]) -> str:
    if not projects:
        return "No projects found."
    lines = []
    for p in projects:
        lines.append(f"- {p['name']} ({p['branch']}) @ {p['path']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Speech-to-Text Corrections
# ---------------------------------------------------------------------------

STT_CORRECTIONS = {
    r"\bcloud code\b": "Claude Code",
    r"\bclock code\b": "Claude Code",
    r"\bquad code\b": "Claude Code",
    r"\bclawed code\b": "Claude Code",
    r"\bclod code\b": "Claude Code",
    r"\bcloud\b": "Claude",
    r"\b(kairos|kyros|chiros|cairo's|kiros|ki ross)\b": "KIAROS",
}


def apply_speech_corrections(text: str) -> str:
    """Fix common speech-to-text errors before processing."""
    result = text
    for pattern, replacement in STT_CORRECTIONS.items():
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result


# ---------------------------------------------------------------------------
# LLM Intent Classifier (kept for tests / tooling)
# ---------------------------------------------------------------------------

async def classify_intent(text: str, client: anthropic.AsyncAnthropic) -> dict:
    """Classify a voice command. Returns {"action": "open_terminal|browse|build|chat", "target": str}."""
    try:
        raw = await llm.fast_text(
            client,
            text,
            system=(
                "Classify this voice command. The user is talking to KIAROS, an AI assistant that can:\n"
                "- Open a terminal and run Claude Code (coding AI tool)\n"
                "- Open the browser for web searches and URLs\n"
                "- Build software projects via Claude Code\n\n"
                "Note: speech-to-text may produce errors like \"Cloud\" for \"Claude\".\n\n"
                "Return ONLY valid JSON: {\"action\": \"open_terminal|browse|build|chat\", "
                "\"target\": \"description of what to do\"}\n"
                "If unclear, default to \"chat\"."
            ),
        )
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        return {"action": data.get("action", "chat"), "target": data.get("target", text)}
    except Exception as e:
        log.warning(f"Intent classification failed: {e}")
        return {"action": "chat", "target": text}


# ---------------------------------------------------------------------------
# Markdown Stripping for TTS
# ---------------------------------------------------------------------------

_BANNED = ["my apologies", "i apologize", "absolutely", "great question",
           "i'd be happy to", "of course", "how can i help",
           "is there anything else", "i should clarify", "let me know if",
           "feel free to"]


def strip_markdown_for_tts(text: str) -> str:
    """Strip markdown and banned phrases before speech."""
    result = re.sub(r"```[\s\S]*?```", "", text)
    result = result.replace("`", "").replace("**", "").replace("*", "")
    result = re.sub(r"^#{1,6}\s*", "", result, flags=re.MULTILINE)
    result = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", result)
    result = re.sub(r"^\s*[-*+]\s+", "", result, flags=re.MULTILINE)
    result = re.sub(r"^\s*\d+\.\s+", "", result, flags=re.MULTILINE)
    result = re.sub(r"\n{2,}", ". ", result).replace("\n", " ")
    result = re.sub(r"\s{2,}", " ", result)
    lower = result.lower()
    for phrase in _BANNED:
        idx = lower.find(phrase)
        while idx != -1:
            end = idx + len(phrase)
            if end < len(result) and result[end] in " ,—-":
                end += 1
            result = result[:idx] + result[end:]
            lower = result.lower()
            idx = lower.find(phrase)
    if HONORIFIC.lower() != "sir":
        result = re.sub(r"\bsir\b", HONORIFIC, result) if HONORIFIC else re.sub(r",?\s*\bsir\b", "", result)
    return result.strip().strip(",").strip("—").strip("-").strip()


# ---------------------------------------------------------------------------
# Action Tag Extraction
# ---------------------------------------------------------------------------

ACTION_TYPES = (
    "BUILD|BROWSE|RESEARCH|OPEN_TERMINAL|PROMPT_PROJECT|ADD_TASK|ADD_NOTE|COMPLETE_TASK|"
    "REMEMBER|CREATE_NOTE|READ_NOTE|SCREEN|SYSTEM|PHONE|ADD_CONTACT|TIMER"
)


def extract_action(response: str) -> tuple[str, dict | None]:
    """Extract an [ACTION:X] tag. Returns (clean_text_for_tts, action_or_none)."""
    match = re.search(rf'\[ACTION:({ACTION_TYPES})\]\s*(.*?)$', response, re.DOTALL)
    if match:
        return response[:match.start()].strip(), {"action": match.group(1).lower(), "target": match.group(2).strip()}
    return response, None


# ---------------------------------------------------------------------------
# Connected clients (laptop browser, phone app) and speech output
# ---------------------------------------------------------------------------

# ws -> {"device": "laptop"|"phone", "platform": str, "native": bool, "host": str}
clients: dict[WebSocket, dict] = {}


# One conversation shared by every device, persisted so it survives restarts.
CONVO_FILE = DATA_DIR / "conversation.json"
CONVO_KEEP = 60
shared: dict = {"history": [], "summary": "", "last_response": "", "pending_confirm": None}


def _load_conversation():
    try:
        data = json.loads(CONVO_FILE.read_text())
        shared["history"] = [m for m in data.get("history", []) if m.get("role") in ("user", "assistant")][-CONVO_KEEP:]
        shared["summary"] = data.get("summary", "")
    except (OSError, ValueError):
        pass


def _save_conversation():
    try:
        shared["history"][:] = shared["history"][-CONVO_KEEP:]
        CONVO_FILE.write_text(json.dumps({"history": shared["history"], "summary": shared["summary"]}))
    except OSError as e:
        log.debug(f"Conversation save failed: {e}")


def add_turn(role: str, content: str, device: str = ""):
    shared["history"].append({"role": role, "content": content, "device": device, "ts": time.time()})


def devices_online() -> list[dict]:
    return [{"device": i["device"], "platform": i["platform"]} for i in clients.values()]


async def send_others(ws, payload: dict):
    """Send to every connected device except ws (used to mirror the conversation)."""
    for other in list(clients):
        if other is ws:
            continue
        try:
            await other.send_json(payload)
        except Exception:
            pass


async def broadcast_presence():
    payload = {"type": "presence", "devices": devices_online()}
    for w in list(clients):
        try:
            await w.send_json(payload)
        except Exception:
            pass


def device_of(ws) -> str:
    return clients.get(ws, {}).get("device", "laptop") if ws else "laptop"


def phone_clients() -> list[WebSocket]:
    return [w for w, info in clients.items() if info.get("device") == "phone"]


def describe_devices(ws) -> str:
    here = device_of(ws)
    phones = len(phone_clients())
    lines = [f"The user is talking to you from their {here.upper()}."]
    if here == "phone":
        lines.append("Laptop actions (SYSTEM, BUILD, OPEN_TERMINAL, SCREEN) run on the laptop remotely; PHONE actions run in their hand.")
    else:
        lines.append(f"Phone connected: {'yes' if phones else 'no'} — PHONE actions need it." )
    return "\n".join(lines)


async def send_device_action(ws, payload: dict) -> bool:
    """Send a phone-side action to the requesting phone, or any connected phone."""
    targets = [ws] if ws is not None and device_of(ws) == "phone" else phone_clients()
    for target in targets:
        try:
            await target.send_json({"type": "device_action", **payload})
            return True
        except Exception:
            continue
    return False


async def speak(ws, text: str, sync: bool = True):
    """Speak text on one device (Fish Audio, else the device's own TTS).

    With sync=True the other devices get the same line as a silent caption.
    """
    if ws is None or not text:
        return
    tts = strip_markdown_for_tts(text)
    audio = await synthesize_speech(tts) if tts else None
    try:
        await ws.send_json({"type": "status", "state": "speaking"})
        if audio:
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": tts})
        else:
            await ws.send_json({"type": "text", "text": tts, "speak": True})
    except Exception:
        pass  # client went away
    if sync:
        await send_others(ws, {"type": "sync", "role": "assistant", "text": tts, "device": device_of(ws)})
    log.info(f"KIAROS: {tts}")


async def broadcast_speech(text: str):
    for ws in list(clients):
        await speak(ws, text, sync=False)


# ---------------------------------------------------------------------------
# Action executors
# ---------------------------------------------------------------------------

async def _execute_browse(target: str, ws=None):
    """Open a URL or search, on whichever device the user is using."""
    target = target.strip()
    first = target.split()[0] if target else ""
    if target.startswith("http") or ("." in first and " " not in target):
        url = target if target.startswith("http") else "https://" + target
    else:
        url = search_url(target)
    if device_of(ws) == "phone":
        await send_device_action(ws, {"action": "open_url", "target": url})
    else:
        await open_browser(url)


_PHONE_ACTIONS = {"call", "sms", "whatsapp", "navigate", "open_url"}


async def _execute_phone(target: str, ws) -> str | None:
    """Resolve and send a phone action. Returns a spoken override on failure."""
    parts = [p.strip() for p in target.split("|||")]
    action = parts[0].lower() if parts else ""
    who = parts[1] if len(parts) > 1 else ""
    message = parts[2] if len(parts) > 2 else ""
    if action not in _PHONE_ACTIONS:
        return "That's not something I can do on the phone yet."
    payload = {"action": action, "target": who, "message": message}
    if action in ("call", "sms", "whatsapp"):
        if re.fullmatch(r"[+\d\s().-]{5,}", who):
            number = who
        else:
            contact = find_contact(who) if who else None
            if not contact:
                return f"I don't have a number for {who or 'that person'}. Tell me the number and I'll save it."
            number = contact["phone"]
        payload["number"] = re.sub(r"[^\d+]", "", number)
    if not await send_device_action(ws, payload):
        return "Your phone isn't connected to me right now. Open the KIAROS app on it first."
    return None


async def _execute_system(target: str, ws, session: dict) -> str:
    """Run a laptop control command; power actions wait for confirmation."""
    name, _, arg = target.partition("|||")
    name, arg = name.strip().lower(), arg.strip()
    if name not in system_control.COMMANDS:
        return "That's not something I can control on the laptop."
    if name in system_control.CONFIRM_REQUIRED:
        session["pending_confirm"] = {"name": name, "arg": arg, "time": time.time()}
        what = {"shutdown": "shut down", "restart": "restart", "suspend": "suspend", "logout": "log out of"}[name]
        return f"Shall I {what} the laptop? Say yes to confirm."
    return await system_control.run_command(name, arg)


_timers: list[dict] = []


async def _run_timer(seconds: int, label: str):
    entry = {"label": label, "ends": time.time() + seconds}
    _timers.append(entry)
    try:
        await asyncio.sleep(seconds)
        msg = f"Time's up: {label}." if label else "Time's up."
        await system_control.notify("KIAROS", msg)
        for ws in list(clients):
            try:
                await ws.send_json({"type": "device_action", "action": "alert", "message": msg})
            except Exception:
                pass
        await broadcast_speech(msg)
    finally:
        _timers.remove(entry)


def _timers_summary() -> str:
    if not _timers:
        return ""
    return "; ".join(f"{t['label'] or 'timer'} in {max(0, int(t['ends'] - time.time()))}s" for t in _timers)


def _render_report_html(title: str, markdown: str) -> str:
    import html as _html
    safe_md = markdown.replace("</script", "<\\/script")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(title[:60])}</title>
<style>
:root{{--bg:#0a0d12;--fg:#e4e8ee;--muted:#8b95a3;--accent:#4cc9f0;--card:#121822}}
body{{font-family:system-ui,sans-serif;max-width:820px;margin:0 auto;padding:24px 16px;background:var(--bg);color:var(--fg);line-height:1.7}}
h1{{color:var(--accent);font-size:1.5em}} h2,h3{{color:var(--accent)}} a{{color:var(--accent)}}
pre,code{{background:var(--card);border-radius:6px}} pre{{padding:12px;overflow-x:auto}} code{{padding:2px 5px}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #243041;padding:6px 8px}}
footer{{color:var(--muted);font-size:.85em;margin-top:40px;border-top:1px solid #243041;padding-top:12px}}
</style></head><body>
<h1>{_html.escape(title[:120])}</h1>
<article id="report"><pre style="white-space:pre-wrap">{_html.escape(markdown)}</pre></article>
<footer>Researched by KIAROS · {datetime.now().strftime('%B %d, %Y %I:%M %p')}</footer>
<script id="md" type="text/markdown">{safe_md}</script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script>try{{document.getElementById('report').innerHTML=marked.parse(document.getElementById('md').textContent)}}catch(e){{}}</script>
</body></html>"""


async def _execute_research(brief: str, ws=None, history: list | None = None):
    """Research with live web search on the deep model, save a report, open it."""
    if not anthropic_client:
        await speak(ws, "Research needs an Anthropic API key. Add one in settings.")
        return
    try:
        text = await llm.research(
            anthropic_client, brief,
            system=(f"You are KIAROS, researching for {USER_NAME or 'the user'}. Use web search to find real, "
                    "current information. Write a well-organized Markdown report with headings, concrete facts, "
                    "and a Sources section with links."),
        )
        if not text:
            await speak(ws, "That research came back empty. Try rephrasing the request.")
            return
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{_generate_project_name(brief)[:50]}-{datetime.now():%Y%m%d-%H%M%S}.html"
        (REPORTS_DIR / name).write_text(_render_report_html(brief.split("\n")[0][:100], text))
        recently_built.append({"name": name, "path": str(REPORTS_DIR / name), "time": time.time()})

        if device_of(ws) == "phone":
            await send_device_action(ws, {"action": "open_url", "target": f"/reports/{name}", "server_relative": True})
        else:
            await open_browser(f"http://localhost:{HTTP_PORT}/reports/{name}")

        summary = await llm.fast_text(anthropic_client, text[:4000],
                                      system="Summarize this research in ONE spoken sentence. No markdown.")
        msg = (summary or "Research is complete.") + " The full report is open."
        if history is not None:
            history.append({"role": "assistant", "content": f"[Research result]: {summary}"})
        await speak(ws, msg)
    except Exception as e:
        log.error(f"Research failed: {e}", exc_info=True)
        await speak(ws, "The research ran into a problem. I've opened a search instead.")
        await _execute_browse(brief[:200], ws)


def _find_project_dir(project_name: str) -> str | None:
    """Find a project directory by name from cached projects or the projects folder."""
    q = project_name.lower().strip()
    if q in ("kiaros", "yourself", "you", "self"):
        return PROJECT_DIR
    for p in cached_projects:
        if q in p.get("name", "").lower():
            return p.get("path")
    try:
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir() and q in d.name.lower():
                return str(d)
    except OSError:
        pass
    return None


async def _execute_prompt_project(project_name: str, prompt: str, work_session: WorkSession, ws,
                                  dispatch_id: int = None, history: list[dict] = None, voice_state: dict = None):
    """Dispatch a prompt to Claude Code in a project, in the background, and report back."""
    try:
        project_dir = _find_project_dir(project_name)
        if dispatch_id is None:
            dispatch_id = dispatch_registry.register(project_name, project_dir or "", prompt)

        if not project_dir:
            await speak(ws, f"Couldn't find the {project_name} project directory.")
            return
        if not claude_available():
            dispatch_registry.update_status(dispatch_id, "failed", response="claude CLI missing")
            await speak(ws, "The Claude Code CLI isn't installed on the laptop, so I can't work on projects yet.")
            return

        dispatch = WorkSession()
        await dispatch.start(project_dir, project_name)
        log.info(f"Dispatching to {project_name} in {project_dir}: {prompt[:80]}")
        dispatch_registry.update_status(dispatch_id, "building")

        full_response = await dispatch.send(prompt)
        await dispatch.stop()

        running_match = re.search(r'RUNNING_AT=(https?://localhost:\d+)', full_response or "") \
            or re.search(r'https?://localhost:\d+', full_response or "")
        if running_match:
            url = running_match.group(1) if running_match.lastindex else running_match.group(0)
            if device_of(ws) == "laptop":
                asyncio.create_task(open_browser(url))
            dispatch_registry.update_status(dispatch_id, "completed", response=full_response[:2000], summary=f"Running at {url}")

        if not full_response or full_response.startswith(("Hit a problem", "That's taking")):
            dispatch_registry.update_status(dispatch_id, "failed" if full_response else "timeout", response=full_response or "")
            msg = f"I ran into an issue with {project_name}. {full_response[:150] if full_response else 'No response received.'}"
        elif anthropic_client:
            try:
                msg = await llm.fast_text(
                    anthropic_client,
                    f"Project: {project_name}\nClaude Code reported:\n{full_response[:3000]}",
                    system=(
                        "You are KIAROS reporting back on work you did in a project. First person — 'I found', "
                        "'I built'. Be specific but concise; if there are several items give the count and the top "
                        "2-3. End by asking how to proceed. Never read URLs aloud, never say 'Claude Code'. "
                        "2-3 sentences, no markdown."
                    ),
                ) or f"{project_name} is done."
            except Exception:
                msg = f"{project_name} finished. The gist: {full_response[:200]}"
        else:
            msg = f"{project_name} is done. {full_response[:200]}"

        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping dispatch audio for {project_name} — user spoke recently")
        else:
            await speak(ws, msg)

        if history is not None:
            history.append({"role": "assistant", "content": f"[Dispatch result for {project_name}]: {msg}"})
        dispatch_registry.update_status(dispatch_id, "completed", response=(full_response or "")[:2000], summary=msg[:200])
    except Exception as e:
        log.error(f"Prompt project failed: {e}", exc_info=True)
        await speak(ws, f"Had trouble connecting to {project_name}.")


def _write_build_brief(path: str, target: str):
    Path(path, "CLAUDE.md").write_text(
        f"# Task\n\n{target}\n\n"
        "## Instructions\n"
        "- BUILD THIS NOW. Do not ask clarifying questions.\n"
        "- Use your best judgment for any design/architecture decisions.\n"
        "- Write complete, working code files — not plans or specs.\n"
        "- If it's a web app: use React + Vite + Tailwind unless specified otherwise.\n"
        "- Make it look polished and professional. Modern UI, clean layout.\n"
        "- Ensure it runs with a single command (npm run dev or similar).\n"
        "- Use realistic mock data, not Lorem Ipsum.\n"
        "- After building, start the dev server and verify the app loads without errors.\n"
        "- IMPORTANT: Your LAST line of output MUST be exactly: RUNNING_AT=http://localhost:PORT\n"
    )


_last_greeting_time: float = 0


# ---------------------------------------------------------------------------
# TTS (Fish Audio, optional — devices fall back to their own voices)
# ---------------------------------------------------------------------------

async def synthesize_speech(text: str) -> Optional[bytes]:
    """Generate MP3 speech with Fish Audio, or None to let the device speak."""
    if not FISH_API_KEY:
        return None
    payload = {"text": text, "format": "mp3"}
    if FISH_VOICE_ID:
        payload["reference_id"] = FISH_VOICE_ID
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(
                FISH_API_URL,
                headers={"Authorization": f"Bearer {FISH_API_KEY}", "Content-Type": "application/json"},
                json=payload,
            )
            if response.status_code == 200:
                _session_tokens["tts_calls"] += 1
                _append_usage_entry(0, 0, "tts")
                return response.content
            log.error(f"TTS error: {response.status_code}")
    except Exception as e:
        log.error(f"TTS error: {e}")
    return None


# ---------------------------------------------------------------------------
# LLM Response
# ---------------------------------------------------------------------------

async def generate_response(
    text: str,
    client: anthropic.AsyncAnthropic,
    task_mgr: "ClaudeTaskManager",
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
    device_context: str = "",
) -> str:
    """Generate a KIAROS reply with the fast model."""
    current_time = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    system = KIAROS_SYSTEM_PROMPT.format(
        current_time=current_time,
        weather_info=_ctx_cache.get("weather", "Weather data unavailable."),
        device_context=device_context or "The user is on their laptop.",
        screen_context=_ctx_cache["screen"] or "Not checked yet.",
        calendar_context=_ctx_cache["calendar"],
        mail_context=_ctx_cache["mail"],
        active_tasks=task_mgr.get_active_tasks_summary(),
        dispatch_context=dispatch_registry.format_for_prompt(),
        known_projects=format_projects_for_prompt(projects),
        user_name=USER_NAME or "the user",
        honorific=HONORIFIC or "by name",
        project_dir=PROJECT_DIR,
    )
    lookup_status = get_lookup_status()
    if lookup_status:
        system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."
    timers = _timers_summary()
    if timers:
        system += f"\n\nRUNNING TIMERS: {timers}"
    memory_ctx = build_memory_context(text)
    if memory_ctx:
        system += f"\n\nKIAROS MEMORY:\n{memory_ctx}"
    if session_summary:
        system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"
    if last_response:
        system += f'\n\nYOUR LAST RESPONSE (do not repeat this):\n"{last_response[:150]}"'

    messages = [{"role": m["role"], "content": m["content"]} for m in conversation_history[-20:]]
    if not messages or messages[-1].get("content") != text:
        messages = messages + [{"role": "user", "content": text}]
    # The API requires the first message to be from the user.
    while messages and messages[0]["role"] != "user":
        messages = messages[1:]

    try:
        response = await llm.fast(client, messages, system=system, max_tokens=1024)
        track_usage(response)
        return llm.text_of(response) or "I didn't catch a usable answer there. Could you say that again?"
    except anthropic.AuthenticationError:
        return "My API key was rejected. Please check it in settings."
    except anthropic.RateLimitError:
        return "I'm being rate limited. Give me a moment and try again."
    except anthropic.APIConnectionError:
        return "I can't reach my language model. Is the laptop online?"
    except Exception as e:
        log.error(f"LLM error: {e}")
        return "I'm having trouble with my language systems right now."


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

# Shared state
task_manager = ClaudeTaskManager(max_concurrent=3)
anthropic_client: Optional[anthropic.AsyncAnthropic] = None
cached_projects: list[dict] = []
recently_built: list[dict] = []  # [{"name": str, "path": str, "time": float}]
dispatch_registry = DispatchRegistry()
shared_work_session = WorkSession()
shared_planner = TaskPlanner()
shared_voice_state: dict = {"last_user_time": 0.0}
qa_agent = QAAgent()
success_tracker = SuccessTracker()

# Usage tracking — logs every call with timestamp, persists to disk
_USAGE_FILE = DATA_DIR / "usage_log.jsonl"
_session_start = time.time()
_session_tokens = {"input": 0, "output": 0, "api_calls": 0, "tts_calls": 0}


def _append_usage_entry(input_tokens: int, output_tokens: int, call_type: str = "api"):
    """Append a usage entry with timestamp to the log file."""
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        entry = {
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        with open(_USAGE_FILE, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


def _get_usage_for_period(seconds: float | None = None) -> dict:
    """Sum usage from the log file for a time period. None = all time."""
    import json as _json
    totals = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "tts_calls": 0}
    cutoff = (time.time() - seconds) if seconds else 0
    try:
        if _USAGE_FILE.exists():
            for line in _USAGE_FILE.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = _json.loads(line)
                if entry["ts"] >= cutoff:
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    if entry.get("type") == "tts":
                        totals["tts_calls"] += 1
                    else:
                        totals["api_calls"] += 1
    except Exception:
        pass
    return totals


# Fast-model list prices (USD per 1M tokens): claude-haiku-5-5 = $0.10 in / $0.50 out.
_PRICE_IN, _PRICE_OUT = 0.10, 0.50


def _cost_from_tokens(input_t: int, output_t: int) -> float:
    return (input_t / 1_000_000) * _PRICE_IN + (output_t / 1_000_000) * _PRICE_OUT


def track_usage(response):
    """Track token usage from an Anthropic API response."""
    inp = getattr(response.usage, "input_tokens", 0) if hasattr(response, "usage") else 0
    out = getattr(response.usage, "output_tokens", 0) if hasattr(response, "usage") else 0
    _session_tokens["input"] += inp
    _session_tokens["output"] += out
    _session_tokens["api_calls"] += 1
    _append_usage_entry(inp, out, "api")


def get_usage_summary() -> str:
    """Get a voice-friendly usage summary with time breakdowns."""
    uptime_min = int((time.time() - _session_start) / 60)

    session = _session_tokens
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    all_time = _get_usage_for_period(None)

    session_cost = _cost_from_tokens(session["input"], session["output"])
    today_cost = _cost_from_tokens(today["input_tokens"], today["output_tokens"])
    all_cost = _cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"])

    parts = [f"This session: {uptime_min} minutes, {session['api_calls']} calls, ${session_cost:.2f}."]

    if today["api_calls"] > session["api_calls"]:
        parts.append(f"Today total: {today['api_calls']} calls, ${today_cost:.2f}.")

    if all_time["api_calls"] > today["api_calls"]:
        parts.append(f"All time: {all_time['api_calls']} calls, ${all_cost:.2f}.")

    return " ".join(parts)

# Background context cache — never blocks responses
_ctx_cache = {
    "screen": "",
    "calendar": "Calendar not checked yet.",
    "mail": "Email not checked yet.",
    "weather": "Weather data unavailable.",
}


def _refresh_context_sync():
    """Background thread: refreshes weather (and window list on X11) every 60s."""
    import threading
    import urllib.request

    def _worker():
        while True:
            try:
                loc = os.getenv("WEATHER_LOCATION", "").strip().replace(" ", "+")
                url = f"https://wttr.in/{loc}?format=%l:+%C,+%t&m"
                req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    text = resp.read().decode().strip()
                    if text and "Unknown" not in text and len(text) < 120:
                        _ctx_cache["weather"] = f"Current weather — {text}"
            except Exception:
                pass
            time.sleep(600)

    threading.Thread(target=_worker, daemon=True).start()
    log.info("Context refresh thread started")


async def _refresh_context_async():
    """Async loop: calendar, mail and window context, every 5 minutes."""
    while True:
        try:
            windows = await get_active_windows()
            _ctx_cache["screen"] = format_windows_for_context(windows)
        except Exception:
            pass
        if calendar_configured():
            try:
                await refresh_calendar_cache()
                _ctx_cache["calendar"] = format_events_for_context(await get_todays_events())
            except Exception as e:
                log.debug(f"Calendar refresh failed: {e}")
        else:
            _ctx_cache["calendar"] = "Calendar not connected."
        if mail_configured():
            try:
                _ctx_cache["mail"] = format_unread_summary(await get_unread_count())
            except Exception as e:
                log.debug(f"Mail refresh failed: {e}")
                _ctx_cache["mail"] = "Mail unreachable right now."
        else:
            _ctx_cache["mail"] = "Email not connected."
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(application: FastAPI):
    global anthropic_client, cached_projects
    auth.ensure_token()
    if ANTHROPIC_API_KEY:
        anthropic_client = llm.make_client(ANTHROPIC_API_KEY)
    else:
        log.warning("ANTHROPIC_API_KEY not set — conversation disabled until you add it in settings")
    cached_projects = []
    _load_conversation()
    _refresh_context_sync()
    refresher = asyncio.create_task(_refresh_context_async())
    log.info("KIAROS server starting")
    yield
    refresher.cancel()


app = FastAPI(title="KIAROS Server", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_PUBLIC_API = {"/api/health"}


@app.middleware("http")
async def require_token(request: Request, call_next):
    """Everything under /api and /reports needs the pairing token unless it's from this laptop."""
    path = request.url.path
    protected = (path.startswith("/api/") and path not in _PUBLIC_API) or path.startswith("/reports/")
    if protected and request.method != "OPTIONS":
        host = request.client.host if request.client else None
        if not auth.authorized(host, request.headers, request.query_params):
            return JSONResponse(status_code=401, content={"error": "unauthorized", "hint": "pair this device first"})
    return await call_next(request)


# -- REST Endpoints --------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "online", "name": "KIAROS", "version": "1.0.0"}


@app.get("/api/tts-test")
async def tts_test():
    """Generate a test audio clip for debugging."""
    audio = await synthesize_speech("Testing audio.")
    if audio:
        return {"audio": base64.b64encode(audio).decode()}
    return {"audio": None, "error": "TTS failed"}


@app.get("/api/usage")
async def api_usage():
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    month = _get_usage_for_period(86400 * 30)
    all_time = _get_usage_for_period(None)
    return {
        "session": {**_session_tokens, "uptime_seconds": uptime},
        "today": {**today, "cost_usd": round(_cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4)},
        "week": {**week, "cost_usd": round(_cost_from_tokens(week["input_tokens"], week["output_tokens"]), 4)},
        "month": {**month, "cost_usd": round(_cost_from_tokens(month["input_tokens"], month["output_tokens"]), 4)},
        "all_time": {**all_time, "cost_usd": round(_cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"]), 4)},
    }


@app.get("/api/tasks")
async def api_list_tasks():
    tasks = await task_manager.list_tasks()
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    task = await task_manager.get_status(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {"task": task.to_dict()}


@app.post("/api/tasks")
async def api_create_task(req: TaskRequest):
    try:
        task_id = await task_manager.spawn(req.prompt, req.working_dir)
        return {"task_id": task_id, "status": "spawned"}
    except RuntimeError as e:
        return JSONResponse(status_code=429, content={"error": str(e)})


@app.delete("/api/tasks/{task_id}")
async def api_cancel_task(task_id: str):
    cancelled = await task_manager.cancel(task_id)
    if not cancelled:
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or not cancellable"},
        )
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/projects")
async def api_list_projects():
    global cached_projects
    cached_projects = await scan_projects()
    return {"projects": cached_projects}


# -- Fast Action Detection (no LLM call) -----------------------------------

def _scan_projects_sync() -> list[dict]:
    """Synchronous projects-folder scan — runs in executor."""
    projects = []
    try:
        for entry in PROJECTS_DIR.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                projects.append({"name": entry.name, "path": str(entry), "branch": ""})
    except Exception:
        pass
    return projects


def _has(t: str, phrases) -> bool:
    return any(p in t for p in phrases)


def detect_system_command(text: str, device: str) -> dict | None:
    """Instant laptop controls for short, unambiguous commands."""
    t = re.sub(r"[^\w\s%]", "", text.lower()).strip()
    t = re.sub(r"^(hey |ok |okay )?kiaros\s*", "", t)
    t = re.sub(r"\s*(please|now)$", "", t)
    if len(t.split()) > 8:
        return None
    on_laptop = device == "laptop" or _has(t, ["laptop", "computer", "pc", "desktop"])
    n = re.search(r"(\d+)\s*(percent|%)?", t)

    if re.search(r"\b(set )?volume (to )?\d+", t):
        return {"command": "volume_set", "arg": n.group(1) if n else "50"}
    if _has(t, ["volume up", "turn it up", "turn up the volume", "louder", "increase the volume", "increase volume"]):
        return {"command": "volume_up", "arg": n.group(1) if n else ""}
    if _has(t, ["volume down", "turn it down", "turn down the volume", "quieter", "decrease the volume", "lower the volume", "decrease volume"]):
        return {"command": "volume_down", "arg": n.group(1) if n else ""}
    if re.fullmatch(r"(mute|mute (the )?(sound|audio|volume|laptop))", t):
        return {"command": "mute"}
    if re.fullmatch(r"unmute( (the )?(sound|audio|volume|laptop))?", t):
        return {"command": "unmute"}
    if re.fullmatch(r"(pause|pause (the )?(music|song|video|playback|media))", t):
        return {"command": "pause"}
    if re.fullmatch(r"(resume|play|play music|resume (the )?(music|song|video|playback))", t):
        return {"command": "play"}
    if re.fullmatch(r"(next|skip|next (song|track)|skip (this )?(song|track))", t):
        return {"command": "next"}
    if re.fullmatch(r"(previous|previous (song|track)|go back a (song|track))", t):
        return {"command": "previous"}
    if _has(t, ["lock my laptop", "lock the laptop", "lock my computer", "lock the screen", "lock my screen", "lock the computer"]):
        return {"command": "lock"}
    if _has(t, ["battery"]) and on_laptop or re.fullmatch(r"(how much )?battery( (left|level|status))?", t):
        return {"command": "battery"}
    if _has(t, ["brightness up", "brighter", "increase brightness", "increase the brightness"]):
        return {"command": "brightness_up"}
    if _has(t, ["brightness down", "dimmer", "dim the screen", "decrease brightness", "lower the brightness"]):
        return {"command": "brightness_down"}
    if _has(t, ["shut down", "shutdown", "power off"]) and on_laptop:
        return {"command": "shutdown"}
    if _has(t, ["restart", "reboot"]) and on_laptop and "server" not in t:
        return {"command": "restart"}
    if _has(t, ["go to sleep", "suspend", "sleep mode"]) and on_laptop:
        return {"command": "suspend"}

    looks_like_url = re.search(r"\w\.(com|org|net|io|ai|in|dev|app|co)\b|https?://|www\.", text.lower())
    m = re.fullmatch(r"(open|launch|start)( up)? (the |my )?(.+?)( app)?( on (my |the )?(laptop|computer|pc))?", t)
    if m and on_laptop and not looks_like_url:
        name = m.group(4).strip()
        folders = {"downloads", "documents", "pictures", "photos", "music", "videos", "desktop", "home folder"}
        if name in folders or name.endswith(" folder") and name[:-7] in folders:
            return {"command": "open_folder", "arg": name.replace(" folder", "")}
        if name not in {"terminal", "claude", "claude code", "a terminal", "browser"} and "." not in name \
                and system_control.find_app(name):
            return {"command": "open_app", "arg": name}
    return None


_NUMBER_RE = r"\+?\d[\d\s().-]{4,}\d"


def _strip_filler(text: str) -> str:
    t = text.strip().rstrip(".!?")
    return re.sub(r"^(hey |ok |okay )?kiaros[,\s]*|^(please |can you |could you )", "", t, flags=re.I).strip()


def detect_phone_command(text: str) -> dict | None:
    """Instant contact/phone commands. Only fires for numbers and SAVED contacts,
    so phrases like "call it a day" still go to the LLM."""
    t = _strip_filler(text)

    m = re.match(rf"^(?:save|add|store)\s+(?:contact\s+)?(.+?)(?:'s|s')?\s+(?:phone\s+)?(?:number|contact|mobile)\s+(?:as\s+|is\s+)?({_NUMBER_RE})$", t, re.I) \
        or re.match(rf"^(?:save|add)\s+(?:contact\s+)?(.+?)\s+(?:as|with number|number|on)\s+({_NUMBER_RE})$", t, re.I)
    if m:
        return {"kind": "add_contact", "name": m.group(1).strip().title() if m.group(1).islower() else m.group(1).strip(),
                "number": m.group(2)}

    m = re.match(r"^(?:call|phone|dial|ring)\s+(.+)$", t, re.I)
    if m:
        target = re.sub(r"\s+(?:now|for me|please|on (?:my )?(?:phone|mobile))$", "", m.group(1).strip(), flags=re.I)
        if re.fullmatch(_NUMBER_RE, target) or find_contact(target, exact=True):
            return {"kind": "phone", "target": f"call ||| {target}"}
        return None

    m = re.match(r"^(text|message|sms|whatsapp)\s+(.+)$", t, re.I)
    if m:
        action = "whatsapp" if m.group(1).lower() == "whatsapp" else "sms"
        rest = m.group(2).strip()
        num = re.match(rf"^({_NUMBER_RE})(?:\s+(?:saying|that|:)?\s*(.*))?$", rest, re.I)
        if num:
            return {"kind": "phone", "target": f"{action} ||| {num.group(1)} ||| {num.group(2) or ''}"}
        words = rest.split()
        for n in range(min(3, len(words)), 0, -1):  # longest saved name first ("Mom", "Rahul Sharma")
            name = " ".join(words[:n])
            if find_contact(name, exact=True):
                msg = re.sub(r"^(?:saying|that|:)\s*", "", " ".join(words[n:]), flags=re.I)
                return {"kind": "phone", "target": f"{action} ||| {name} ||| {msg}"}
    return None


def detect_action_fast(text: str) -> dict | None:
    """Keyword detection ONLY for short, obvious requests; everything else goes to the LLM."""
    t = text.lower().strip()
    if len(t.split()) > 12:
        return None

    if _has(t, ["look at my screen", "what's on my screen", "whats on my screen", "what am i looking at",
                "what do you see", "see my screen", "check my screen", "what's open on my laptop"]):
        return {"action": "describe_screen"}
    if _has(t, ["open claude", "start claude", "launch claude", "run claude", "open a terminal", "open terminal"]):
        return {"action": "open_terminal"}
    if _has(t, ["show me what you built", "pull up what you made", "open what you built", "open the report"]):
        return {"action": "show_recent"}
    if _has(t, ["what's my schedule", "whats my schedule", "what's on my calendar", "whats on my calendar",
                "do i have any meetings", "any meetings", "my schedule today", "what do i have today",
                "my calendar", "upcoming meetings", "next meeting", "what's my next meeting"]):
        return {"action": "check_calendar"}
    if _has(t, ["check my email", "check my mail", "any new emails", "any new mail", "unread emails",
                "unread mail", "what's in my inbox", "whats in my inbox", "read my email", "read my mail",
                "any emails", "email update"]):
        return {"action": "check_mail"}
    if _has(t, ["where are we", "where were we", "project status", "how's the build", "hows the build",
                "status update", "status report", "is it done", "is that done"]):
        return {"action": "check_dispatch"}
    if _has(t, ["what's on my list", "whats on my list", "my tasks", "my to do", "my todo",
                "what do i need to do", "open tasks", "task list"]):
        return {"action": "check_tasks"}
    if _has(t, ["how much have you cost", "how much am i spending", "api cost", "token usage", "api usage"]):
        return {"action": "check_usage"}
    if _has(t, ["system status", "how's my laptop", "hows my laptop", "laptop status", "how is my laptop"]):
        return {"action": "system", "command": "status"}
    return None


# -- Action Handlers -------------------------------------------------------

async def handle_open_terminal() -> str:
    if claude_available():
        return (await open_terminal("claude --dangerously-skip-permissions", cwd=str(PROJECTS_DIR)))["confirmation"]
    return (await open_terminal(cwd=str(PROJECTS_DIR)))["confirmation"]


async def handle_show_recent(ws=None) -> str:
    if not recently_built:
        return "Nothing built recently."
    last = recently_built[-1]
    path = Path(last["path"])
    if path.is_file() and path.parent == REPORTS_DIR:
        if device_of(ws) == "phone":
            await send_device_action(ws, {"action": "open_url", "target": f"/reports/{path.name}", "server_relative": True})
        else:
            await open_browser(f"http://localhost:{HTTP_PORT}/reports/{path.name}")
        return "Here's the latest report."
    for name in ["report.html", "index.html"]:
        f = path / name
        if f.exists():
            await open_browser(f"file://{f}")
            return f"Opened {name} from {last['name']} on the laptop."
    html_files = list(path.glob("*.html")) if path.is_dir() else []
    if html_files:
        await open_browser(f"file://{html_files[0]}")
        return f"Opened {html_files[0].name} from {last['name']} on the laptop."
    return (await open_path(str(path)))["confirmation"]


# ---------------------------------------------------------------------------
# Background lookups — slow work runs off the main path, then reports back
# ---------------------------------------------------------------------------

_active_lookups: dict[str, dict] = {}


async def _lookup_and_report(lookup_type: str, lookup_fn, ws, history: list[dict] = None, voice_state: dict = None):
    lookup_id = str(uuid.uuid4())[:8]
    _active_lookups[lookup_id] = {"type": lookup_type, "status": "working", "started": time.time()}
    try:
        result_text = await asyncio.wait_for(lookup_fn(), timeout=45)
        _active_lookups[lookup_id]["status"] = "done"
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping lookup audio for {lookup_type} — user spoke recently")
        else:
            await speak(ws, result_text)
        if history is not None:
            history.append({"role": "assistant", "content": f"[{lookup_type} check]: {result_text}"})
    except asyncio.TimeoutError:
        _active_lookups[lookup_id]["status"] = "timeout"
        await speak(ws, f"That {lookup_type} check is taking too long.")
    except Exception as e:
        _active_lookups[lookup_id]["status"] = "error"
        log.warning(f"Lookup {lookup_type} failed: {e}")
        await speak(ws, f"I couldn't complete the {lookup_type} check.")
    finally:
        await asyncio.sleep(60)
        _active_lookups.pop(lookup_id, None)


async def _do_calendar_lookup() -> str:
    await refresh_calendar_cache()
    events = await get_todays_events()
    _ctx_cache["calendar"] = format_events_for_context(events)
    return format_schedule_summary(events)


async def _do_mail_lookup() -> str:
    if not mail_configured():
        return "Email isn't connected yet. Add your IMAP details in settings."
    unread_info = await get_unread_count()
    _ctx_cache["mail"] = format_unread_summary(unread_info)
    if unread_info["total"] == 0:
        return "Inbox is clear. No unread messages."
    unread_msgs = await get_unread_messages(count=3)
    summary = format_unread_summary(unread_info)
    if unread_msgs:
        details = ". ".join(f"{_short_sender(m['sender'])} regarding {m['subject']}" for m in unread_msgs)
        return f"{summary} Most recent: {details}."
    return summary


async def _do_screen_lookup() -> str:
    return await describe_screen(anthropic_client)


def get_lookup_status() -> str:
    active = [v for v in _active_lookups.values() if v["status"] == "working"]
    if not active:
        return ""
    return "Currently working on: " + ", ".join(f"{l['type']} check ({int(time.time() - l['started'])}s)" for l in active)


def _short_sender(sender: str) -> str:
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"')
    if "@" in sender:
        return sender.split("@")[0]
    return sender


# -- Session Summary (Three-Tier Memory) -----------------------------------

async def _update_session_summary(
    old_summary: str,
    rotated_messages: list[dict],
    client: anthropic.AsyncAnthropic,
) -> str:
    """Background Haiku call to update the rolling session summary."""
    prompt = f"""Update this conversation summary to include the new messages.

Current summary: {old_summary or '(start of conversation)'}

New messages to incorporate:
{chr(10).join(f'{m["role"]}: {m["content"][:200]}' for m in rotated_messages)}

Write an updated summary in 2-4 sentences capturing the key topics, decisions, and context. Be concise."""

    try:
        response = await client.messages.create(
            model=llm.FAST_MODEL, output_config={"effort": llm.FAST_EFFORT},
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return llm.text_of(response).strip()
    except Exception as e:
        log.warning(f"Summary update failed: {e}")
        return old_summary  # Keep old summary on failure


# -- WebSocket Voice Handler -----------------------------------------------

@app.websocket("/ws/voice")
async def voice_handler(ws: WebSocket):
    """
    WebSocket protocol:

    Client -> Server:
        {"type": "hello", "device": "laptop"|"phone", "platform": "...", "native": bool}
        {"type": "transcript", "text": "...", "isFinal": true}
        {"type": "fix_self"}

    Server -> Client:
        {"type": "audio", "data": "<base64 mp3>", "text": "spoken text"}
        {"type": "text", "text": "...", "speak": true}      (device TTS)
        {"type": "status", "state": "thinking"|"speaking"|"idle"|"working"}
        {"type": "transcript_echo", "text": "..."}
        {"type": "device_action", "action": "call"|"sms"|"whatsapp"|"navigate"|"open_url"|"alert", ...}
        {"type": "task_spawned" | "task_complete" | "qa_result" | "suggestion", ...}
    """
    host = ws.client.host if ws.client else None
    if not auth.authorized(host, ws.headers, ws.query_params):
        # Accept, then close with 4401: rejecting before accept shows up in browsers
        # as a generic failure, and the app couldn't tell the user to pair.
        await ws.accept()
        await ws.close(code=4401, reason="pairing token required")
        log.warning(f"Rejected unauthenticated WebSocket from {host}")
        return

    await ws.accept()
    clients[ws] = {"device": "laptop" if auth.is_loopback(host) else "phone", "platform": "", "native": False, "host": host}
    task_manager.register_websocket(ws)
    # One user, many devices: conversation, work session, planner and pending
    # confirmations are shared, so you can start on the laptop and finish on the phone.
    history: list[dict] = shared["history"]
    work_session = shared_work_session
    planner = shared_planner
    session = shared
    voice_state = shared_voice_state

    log.info(f"Voice WebSocket connected from {host}")

    async def finish(response_text: str, user_text: str):
        """Record the turn, mirror it to other devices, kick off memory work, and speak."""
        dev = device_of(ws)
        add_turn("user", user_text, dev)
        add_turn("assistant", response_text, dev)
        _save_conversation()

        shared_voice_state["turns"] = shared_voice_state.get("turns", 0) + 1
        if shared_voice_state["turns"] % 5 == 0 and len(history) > 20 and anthropic_client \
                and not shared_voice_state.get("summarizing"):
            shared_voice_state["summarizing"] = True
            rotated = [{"role": m["role"], "content": m["content"]} for m in history[:-20]]

            async def _do_summary():
                try:
                    shared["summary"] = await _update_session_summary(shared["summary"], rotated, anthropic_client)
                    _save_conversation()
                finally:
                    shared_voice_state["summarizing"] = False
            asyncio.create_task(_do_summary())

        if anthropic_client and len(user_text) > 15:
            asyncio.create_task(extract_memories(user_text, response_text, anthropic_client))

        await speak(ws, response_text)
        shared["last_response"] = response_text

    try:
        # ── Greeting ──
        global _last_greeting_time
        if time.time() - _last_greeting_time > 60:
            _last_greeting_time = time.time()
            hour = datetime.now().hour
            part = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
            greeting = f"Good {part}, {HONORIFIC}." if HONORIFIC else f"Good {part}{', ' + USER_NAME if USER_NAME else ''}."
            if not anthropic_client:
                greeting += " I need an Anthropic API key before I can talk properly. Open settings to add one."

            async def _greet():
                await asyncio.sleep(0.8)  # let the client's hello arrive first
                await speak(ws, greeting, sync=False)
            asyncio.create_task(_greet())

        await ws.send_json({"type": "status", "state": "idle"})
        recent = [{"role": m["role"], "text": m["content"], "device": m.get("device", "")}
                  for m in history[-8:] if not m["content"].startswith("[")]
        await ws.send_json({"type": "history", "messages": recent})
        await broadcast_presence()

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")

            if mtype == "hello":
                device = msg.get("device")
                if device in ("laptop", "phone"):
                    clients[ws]["device"] = device
                clients[ws]["platform"] = str(msg.get("platform", ""))[:40]
                clients[ws]["native"] = bool(msg.get("native"))
                log.info(f"Client identified: {clients[ws]}")
                await broadcast_presence()
                continue

            if mtype == "fix_self":
                await work_session.start(PROJECT_DIR, "kiaros")
                await speak(ws, "Work mode active in my own code. Tell me what needs fixing.")
                continue

            if mtype != "transcript" or not msg.get("isFinal"):
                continue

            user_text = apply_speech_corrections(str(msg.get("text", "")).strip())[:2000]
            if not user_text:
                continue

            voice_state["last_user_time"] = time.time()
            log.info(f"User ({device_of(ws)}): {user_text}")
            await ws.send_json({"type": "status", "state": "thinking"})
            await send_others(ws, {"type": "sync", "role": "user", "text": user_text, "device": device_of(ws)})

            global cached_projects
            if not cached_projects:
                try:
                    cached_projects = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(None, _scan_projects_sync), timeout=3)
                except Exception:
                    cached_projects = []

            try:
                t_lower = user_text.lower()
                response_text = ""

                # ── Pending power-action confirmation ──
                pending = session.get("pending_confirm")
                if pending:
                    session["pending_confirm"] = None
                    fresh = time.time() - pending["time"] < 60
                    if fresh and re.match(r"^(yes|yeah|yep|confirm|do it|go ahead|sure|affirmative)\b", t_lower):
                        await finish(await system_control.run_command(pending["name"], pending["arg"]), user_text)
                        continue
                    if re.match(r"^(no|nope|cancel|don't|stop|never ?mind)\b", t_lower) or not fresh:
                        await finish("Cancelled.", user_text)
                        continue
                    # Anything else: treat as a new request and drop the pending action.

                # ── Instant phone / contact commands ──
                phone_cmd = None if (planner.is_planning or work_session.active) else detect_phone_command(user_text)
                if phone_cmd:
                    if phone_cmd["kind"] == "add_contact":
                        save_contact(phone_cmd["name"], phone_cmd["number"])
                        response_text = f"Saved {phone_cmd['name']}."
                    else:
                        failure = await _execute_phone(phone_cmd["target"], ws)
                        action, _, rest = phone_cmd["target"].partition("|||")
                        who = rest.split("|||")[0].strip()
                        verb = {"call": "Calling", "sms": "Texting", "whatsapp": "Opening WhatsApp for"}[action.strip()]
                        where = "" if device_of(ws) == "phone" else " on your phone"
                        response_text = failure or f"{verb} {who}{where}."
                    await finish(response_text, user_text)
                    continue

                # ── Instant laptop controls ──
                sys_cmd = None if (planner.is_planning or work_session.active) else detect_system_command(user_text, device_of(ws))
                if sys_cmd:
                    response_text = await _execute_system(f"{sys_cmd['command']} ||| {sys_cmd.get('arg', '')}", ws, session)
                    await finish(response_text, user_text)
                    continue

                # ── PLANNING MODE ──
                if planner.is_planning:
                    if any(p in t_lower for p in BYPASS_PHRASES) or (
                        planner.active_plan and planner.active_plan.confirmed is False
                        and planner.active_plan.current_question_index >= len(planner.active_plan.pending_questions)
                    ):
                        go = True
                        if not any(p in t_lower for p in BYPASS_PHRASES):
                            result = await planner.handle_confirmation(user_text)
                            go = result["confirmed"]
                            if result["cancelled"]:
                                planner.reset()
                                response_text = "Cancelled."
                            elif not go:
                                response_text = result.get("modification_question", "How shall I adjust the plan?")
                        else:
                            plan = planner.active_plan
                            if plan:
                                plan.skipped = True
                                for q in plan.pending_questions[plan.current_question_index:]:
                                    if q.get("default") is not None and q["key"] not in plan.answers:
                                        plan.answers[q["key"]] = q["default"]
                        if go:
                            prompt = await planner.build_prompt()
                            name = _generate_project_name(prompt)
                            path = str(project_path(name))
                            Path(path, "CLAUDE.md").write_text(prompt)
                            did = dispatch_registry.register(name, path, prompt[:200])
                            asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                            planner.reset()
                            response_text = "Building it now."
                    else:
                        result = await planner.process_answer(user_text, cached_projects)
                        if result["plan_complete"]:
                            response_text = result.get("confirmation_summary", "Ready to build. Shall I proceed?")
                        else:
                            response_text = result.get("next_question", "What else?")

                elif any(w in t_lower for w in ["quit work mode", "exit work mode", "go back to chat", "regular mode", "stop working"]):
                    if work_session.active:
                        await work_session.stop()
                        response_text = "Back to conversation mode."
                    else:
                        response_text = "Already in conversation mode."

                # ── WORK MODE: speech → claude -p → summary ──
                elif work_session.active and not is_casual_question(user_text):
                    await ws.send_json({"type": "status", "state": "working"})
                    full_response = await work_session.send(user_text)
                    stall_words = ["which option", "would you prefer", "would you like me to", "before i proceed",
                                   "should i", "do you want me to", "please confirm", "which approach"]
                    if full_response and work_session._message_count >= 2 and any(w in full_response.lower() for w in stall_words):
                        push = await work_session.send(
                            "Stop asking questions. Use your best judgment and start building now. "
                            "Write the actual code files. Go with the simplest reasonable approach.")
                        full_response = push or full_response
                    localhost_match = re.search(r'https?://localhost:\d+', full_response or "")
                    if localhost_match and device_of(ws) == "laptop":
                        asyncio.create_task(open_browser(localhost_match.group(0)))
                    if full_response and anthropic_client:
                        try:
                            response_text = await llm.fast_text(
                                anthropic_client, f"Claude Code said:\n{full_response[:2000]}",
                                system=(f"You are KIAROS reporting to the user ({USER_NAME or 'the user'}). Summarize what "
                                        "happened in 1-2 sentences, first person ('I built', 'I found'). Never give "
                                        "instructions meant for a coding tool, never say 'Claude Code', never read URLs, "
                                        "no markdown, no [ACTION] tags."),
                            ) or full_response[:200]
                        except Exception:
                            response_text = full_response[:200]
                    else:
                        response_text = full_response or "No response from the project session."

                # ── CHAT MODE ──
                else:
                    action = detect_action_fast(user_text)
                    if action:
                        kind = action["action"]
                        if kind == "open_terminal":
                            response_text = await handle_open_terminal()
                        elif kind == "show_recent":
                            response_text = await handle_show_recent(ws)
                        elif kind == "describe_screen":
                            response_text = "Taking a look at the laptop screen."
                            asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history, voice_state))
                        elif kind == "check_calendar":
                            response_text = "Checking your calendar."
                            asyncio.create_task(_lookup_and_report("calendar", _do_calendar_lookup, ws, history, voice_state))
                        elif kind == "check_mail":
                            response_text = "Checking your inbox."
                            asyncio.create_task(_lookup_and_report("mail", _do_mail_lookup, ws, history, voice_state))
                        elif kind == "check_dispatch":
                            recent = dispatch_registry.get_most_recent()
                            if not recent:
                                response_text = "No recent builds on record."
                            else:
                                name, status = recent["project_name"], recent["status"]
                                if status in ("building", "pending"):
                                    response_text = f"Still working on {name}. Been at it for {int(time.time() - recent['updated_at'])} seconds."
                                elif status == "completed":
                                    response_text = recent.get("summary") or f"{name} is complete."
                                elif status in ("failed", "timeout"):
                                    response_text = f"{name} ran into problems."
                                else:
                                    response_text = f"{name} is {status}."
                        elif kind == "check_tasks":
                            response_text = format_tasks_for_voice(get_open_tasks())
                        elif kind == "check_usage":
                            response_text = get_usage_summary()
                        elif kind == "system":
                            response_text = await system_control.run_command(action["command"])
                    elif not anthropic_client:
                        response_text = "My Anthropic API key isn't set. Open settings from the menu to add it."
                    else:
                        response_text = await generate_response(
                            user_text, anthropic_client, task_manager, cached_projects, history,
                            last_response=shared["last_response"], session_summary=shared["summary"],
                            device_context=describe_devices(ws),
                        )
                        clean, embedded = extract_action(response_text)
                        if embedded:
                            log.info(f"LLM embedded action: {embedded}")
                            response_text = clean or "Right away."
                            override = await _dispatch_embedded_action(embedded, ws, work_session, history, voice_state, session)
                            if override:
                                response_text = override

                await finish(response_text, user_text)

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                await speak(ws, "Something went wrong there.")

    except WebSocketDisconnect:
        log.info("Voice WebSocket disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        task_manager.unregister_websocket(ws)
        clients.pop(ws, None)
        await broadcast_presence()


async def _dispatch_embedded_action(embedded: dict, ws, work_session: WorkSession, history: list,
                                    voice_state: dict, session: dict) -> str | None:
    """Execute an [ACTION:X] from the LLM. Returns text to speak instead of the LLM's, if any."""
    kind, target = embedded["action"], embedded["target"]

    if kind == "system":
        return await _execute_system(target, ws, session)
    if kind == "phone":
        return await _execute_phone(target, ws)
    if kind == "add_contact":
        name, _, number = target.partition("|||")
        if name.strip() and re.search(r"\d{3,}", number):
            save_contact(name.strip(), number.strip())
            return None
        return "I need both a name and a number to save a contact."
    if kind == "timer":
        secs, _, label = target.partition("|||")
        m = re.search(r"\d+", secs)
        if not m or not 1 <= int(m.group()) <= 86400:
            return "I couldn't work out how long that timer should be."
        asyncio.create_task(_run_timer(int(m.group()), label.strip()))
        return None
    if kind == "build":
        if not claude_available():
            return "Building needs the Claude Code CLI on the laptop, and it isn't installed."
        name = _generate_project_name(target)
        path = str(project_path(name))
        _write_build_brief(path, target)
        recently_built.append({"name": name, "path": path, "time": time.time()})
        did = dispatch_registry.register(name, path, target)
        asyncio.create_task(_execute_prompt_project(name, target, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
        return None
    if kind == "browse":
        asyncio.create_task(_execute_browse(target, ws))
        return None
    if kind == "research":
        asyncio.create_task(_execute_research(target, ws, history))
        return None
    if kind == "open_terminal":
        return await handle_open_terminal()
    if kind == "prompt_project":
        if "|||" not in target:
            log.warning(f"PROMPT_PROJECT missing ||| delimiter: {target}")
            return None
        proj_name, _, prompt = target.partition("|||")
        proj_name, prompt = proj_name.strip(), prompt.strip()
        recent = dispatch_registry.get_recent_for_project(proj_name)
        if recent and recent.get("summary"):
            history.append({"role": "assistant", "content": f"[Previous dispatch result for {proj_name}]: {recent['summary']}"})
            return recent["summary"]
        asyncio.create_task(_execute_prompt_project(proj_name, prompt, work_session, ws, history=history, voice_state=voice_state))
        return None
    if kind == "add_task":
        parts = [p.strip() for p in target.split("|||")]
        if len(parts) >= 2:
            create_task(title=parts[1], description=parts[2] if len(parts) > 2 else "",
                        priority=parts[0] or "medium", due_date=parts[3] if len(parts) > 3 else "")
        return None
    if kind == "add_note":
        topic, sep, content = target.partition("|||")
        create_note(content=content.strip(), topic=topic.strip()) if sep else create_note(content=target)
        return None
    if kind == "complete_task":
        m = re.search(r"\d+", target)
        if m:
            complete_task(int(m.group()))
        return None
    if kind == "remember":
        remember(target.strip(), mem_type="fact", importance=7)
        return None
    if kind == "create_note":
        title, sep, body = target.partition("|||")
        ok = await create_local_note(title.strip(), body.strip()) if sep else await create_local_note("KIAROS Note", target)
        return None if ok else "I couldn't save that note."
    if kind == "screen":
        asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history, voice_state))
        return None
    if kind == "read_note":
        note = await read_note(target.strip())
        if not note:
            return f"I couldn't find a note matching {target.strip()}."
        body = re.sub(r"\s+", " ", note["body"])[:300]
        return f"Your note {note['title']} says: {body}"
    return None


# ---------------------------------------------------------------------------
# Settings / Configuration endpoints
# ---------------------------------------------------------------------------

def _env_file_path() -> Path:
    return ENV_FILE

def _env_example_path() -> Path:
    return Path(__file__).parent / ".env.example"

def _read_env() -> tuple[list[str], dict[str, str]]:
    """Read .env file. Returns (raw_lines, parsed_dict). Creates from .env.example if missing."""
    path = _env_file_path()
    if not path.exists():
        example = _env_example_path()
        if example.exists():
            import shutil as _shutil
            _shutil.copy2(str(example), str(path))
        else:
            path.write_text("")
    lines = path.read_text().splitlines()
    parsed: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = stripped.partition("=")
            parsed[k.strip()] = v.strip().strip('"').strip("'")
    return lines, parsed

def _write_env_key(key: str, value: str) -> None:
    """Update a single key in .env, preserving comments and order."""
    lines, _ = _read_env()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                new_lines.append(f"{key}={value}")
                found = True
                continue
        new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    _env_file_path().write_text("\n".join(new_lines) + "\n")
    os.environ[key] = value

class KeyUpdate(BaseModel):
    key_name: str
    key_value: str


class KeyTest(BaseModel):
    key_value: str | None = None


class PreferencesUpdate(BaseModel):
    user_name: str = ""
    honorific: str = "sir"
    weather_location: str = ""


SETTABLE_KEYS = {
    "ANTHROPIC_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID", "USER_NAME", "HONORIFIC",
    "CALENDAR_ICS_URLS", "MAIL_IMAP_HOST", "MAIL_ADDRESS", "MAIL_APP_PASSWORD",
    "NOTES_DIR", "WEATHER_LOCATION",
}


def _apply_runtime(key: str, value: str):
    """Make a saved setting take effect without restarting."""
    global anthropic_client, ANTHROPIC_API_KEY, FISH_API_KEY, FISH_VOICE_ID, USER_NAME, HONORIFIC
    if key == "ANTHROPIC_API_KEY":
        ANTHROPIC_API_KEY = value
        anthropic_client = llm.make_client(value) if value else None
    elif key == "FISH_API_KEY":
        FISH_API_KEY = value
    elif key == "FISH_VOICE_ID":
        FISH_VOICE_ID = value
    elif key == "USER_NAME":
        USER_NAME = value
    elif key == "HONORIFIC":
        HONORIFIC = value


def _set_key(key: str, value: str):
    _write_env_key(key, value)
    _apply_runtime(key, value)


@app.post("/api/settings/keys")
async def api_settings_keys(body: KeyUpdate):
    if body.key_name not in SETTABLE_KEYS:
        return JSONResponse({"success": False, "error": "Invalid key name"}, status_code=400)
    value = body.key_value.strip()
    if "\n" in value or "\r" in value:
        return JSONResponse({"success": False, "error": "Value must be a single line"}, status_code=400)
    _set_key(body.key_name, value)
    return {"success": True}


@app.post("/api/settings/test-anthropic")
async def api_test_anthropic(body: KeyTest):
    key = body.key_value or os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        await llm.fast_text(llm.make_client(key), "Reply with the word ok.", max_tokens=1024)
        return {"valid": True}
    except anthropic.AuthenticationError:
        return {"valid": False, "error": "Invalid API key"}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


@app.post("/api/settings/test-fish")
async def api_test_fish(body: KeyTest):
    key = body.key_value or os.getenv("FISH_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    payload = {"text": "test"}
    if FISH_VOICE_ID:
        payload["reference_id"] = FISH_VOICE_ID
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(FISH_API_URL, headers={"Authorization": f"Bearer {key}"}, json=payload)
        if resp.status_code in (200, 201):
            return {"valid": True}
        return {"valid": False, "error": "Invalid API key" if resp.status_code == 401 else f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


@app.post("/api/settings/test-calendar")
async def api_test_calendar():
    if not calendar_configured():
        return {"valid": False, "error": "No calendar link set"}
    try:
        await refresh_calendar_cache()
        events = await get_todays_events()
        return {"valid": True, "events_today": len(events)}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


@app.post("/api/settings/test-mail")
async def api_test_mail():
    if not mail_configured():
        return {"valid": False, "error": "Mail address or app password missing"}
    try:
        info = await get_unread_count()
        return {"valid": True, "unread": info["total"]}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


def _is_set(env: dict, key: str, placeholder: str = "") -> bool:
    v = env.get(key, "").strip()
    return bool(v) and v != placeholder


@app.get("/api/settings/status")
async def api_settings_status():
    from screen import screenshot_tool
    _, env = _read_env()
    memory_count = task_count = 0
    try:
        memory_count = len(get_important_memories(limit=9999))
        task_count = len(get_open_tasks())
    except Exception:
        pass
    return {
        "claude_code_installed": claude_available(),
        "calendar_configured": calendar_configured(),
        "mail_configured": mail_configured(),
        "notes_dir": str(notes_dir()),
        "screenshot_available": screenshot_tool() is not None,
        "voice": "fish" if FISH_API_KEY else "device",
        "memory_count": memory_count,
        "task_count": task_count,
        "devices": [{"device": i["device"], "platform": i["platform"]} for i in clients.values()],
        "server_port": HTTP_PORT,
        "uptime_seconds": int(time.time() - _session_start),
        "env_keys_set": {
            "anthropic": _is_set(env, "ANTHROPIC_API_KEY", "your-anthropic-api-key-here"),
            "fish_audio": _is_set(env, "FISH_API_KEY", "your-fish-audio-api-key-here"),
            "fish_voice_id": _is_set(env, "FISH_VOICE_ID"),
            "calendar": _is_set(env, "CALENDAR_ICS_URLS"),
            "mail_address": env.get("MAIL_ADDRESS", ""),
            "mail_password": _is_set(env, "MAIL_APP_PASSWORD"),
            "user_name": env.get("USER_NAME", ""),
        },
    }


@app.get("/api/settings/preferences")
async def api_get_preferences():
    _, env = _read_env()
    return {
        "user_name": env.get("USER_NAME", ""),
        "honorific": env.get("HONORIFIC", "sir"),
        "weather_location": env.get("WEATHER_LOCATION", ""),
    }


@app.post("/api/settings/preferences")
async def api_save_preferences(body: PreferencesUpdate):
    for key, value in (("USER_NAME", body.user_name), ("HONORIFIC", body.honorific),
                       ("WEATHER_LOCATION", body.weather_location)):
        value = value.replace("\n", " ").strip()
        _set_key(key, value)
    return {"success": True}


# ---------------------------------------------------------------------------
# Pairing (phone setup)
# ---------------------------------------------------------------------------

def _lan_ips() -> list[str]:
    """Best-effort list of this laptop's LAN/Tailscale IPv4 addresses."""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127.") and not ip.startswith("172.17."):
                ips.append(ip)
    except OSError:
        pass
    return ips


@app.get("/api/pairing")
async def api_pairing(request: Request):
    """Connection details for the phone. Only served to the laptop itself."""
    if not auth.is_loopback(request.client.host if request.client else None):
        return JSONResponse(status_code=403, content={"error": "Pairing info is only shown on the laptop."})
    https = (Path(__file__).parent / "cert.pem").exists()
    ips = _lan_ips()
    return {
        "token": os.getenv("KIAROS_TOKEN", ""),
        "ips": ips,
        "app_url": f"http://{ips[0]}:{HTTP_PORT}" if ips else "",
        "browser_url": f"https://{ips[0]}:{HTTPS_PORT}/?token={os.getenv('KIAROS_TOKEN', '')}" if ips and https else "",
        "https_enabled": https,
    }


# ---------------------------------------------------------------------------
# Control endpoints (restart, fix-self)
# ---------------------------------------------------------------------------

@app.post("/api/restart")
async def api_restart():
    """Restart the KIAROS server process in place."""
    log.info("Restart requested — restarting in 2 seconds")

    async def _restart():
        await asyncio.sleep(2)
        os.execv(sys.executable, [sys.executable, *sys.argv])
    asyncio.create_task(_restart())
    return {"status": "restarting"}


@app.post("/api/fix-self")
async def api_fix_self():
    """Open Claude Code in the KIAROS repo so it can fix itself."""
    result = await open_claude_in_project(PROJECT_DIR)
    return {"status": "work_mode_active" if result["success"] else "failed", "path": PROJECT_DIR,
            "message": result["confirmation"]}


# ---------------------------------------------------------------------------
# Static files: research reports and the frontend (PWA)
# ---------------------------------------------------------------------------

from starlette.staticfiles import StaticFiles


@app.get("/reports/{name}")
async def serve_report(name: str):
    path = (REPORTS_DIR / name).resolve()
    if path.parent != REPORTS_DIR.resolve() or not path.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(str(path), media_type="text/html")


FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    @app.get("/sw.js")
    async def service_worker():
        # Service workers must not be cached by the browser.
        return FileResponse(str(FRONTEND_DIST / "sw.js"), media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")


# ---------------------------------------------------------------------------
# Entrypoint — plain HTTP (laptop + Android app) and HTTPS (phone browser)
# ---------------------------------------------------------------------------

async def _serve(host: str, port: int, https_port: int | None, reload: bool):
    import uvicorn
    configs = [uvicorn.Config(app, host=host, port=port, log_level="info")]
    cert, key = Path(__file__).parent / "cert.pem", Path(__file__).parent / "key.pem"
    if https_port and cert.exists() and key.exists():
        configs.append(uvicorn.Config(app, host=host, port=https_port, log_level="info",
                                      ssl_certfile=str(cert), ssl_keyfile=str(key)))
    servers = [uvicorn.Server(c) for c in configs]
    # Only the first server drives the app lifespan; the second just serves.
    for extra in servers[1:]:
        extra.config.lifespan = "off"
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="KIAROS Server")
    parser.add_argument("--host", default=os.getenv("KIAROS_HOST", "0.0.0.0"), help="Bind host")
    parser.add_argument("--port", type=int, default=HTTP_PORT, help="HTTP port")
    parser.add_argument("--https-port", type=int, default=HTTPS_PORT, help="HTTPS port (needs cert.pem/key.pem)")
    parser.add_argument("--no-https", action="store_true", help="Disable the HTTPS listener")
    args = parser.parse_args()

    token = auth.ensure_token()
    ips = _lan_ips()
    has_cert = (Path(__file__).parent / "cert.pem").exists()
    print()
    print("  K.I.A.R.O.S. Server v1.0.0")
    print(f"  Laptop:        http://localhost:{args.port}")
    for ip in ips[:2]:
        print(f"  Android app:   server http://{ip}:{args.port}   token {token}")
        if has_cert and not args.no_https:
            print(f"  Phone browser: https://{ip}:{args.https_port}/?token={token}")
    print()

    asyncio.run(_serve(args.host, args.port, None if args.no_https else args.https_port, False))
