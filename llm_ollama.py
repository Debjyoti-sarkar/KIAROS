"""
KIAROS local model provider (Ollama) — the offline backup brain.

OllamaClient mimics the part of the Anthropic client KIAROS uses
(``messages.create`` / ``beta.messages.create`` returning ``.content`` blocks,
``.stop_reason``, ``.usage``), including tool calling, so the voice loop, task
agent, memory and planner all run unchanged on a model on this laptop.

FallbackClient wraps a cloud client (Gemini/Claude) and the local one: it uses
the cloud normally and switches to the local model when the cloud is out of
quota, overloaded or unreachable — staying on the backup until the quota should
have reset, so it doesn't waste time retrying.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("kiaros.ollama")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")


class OllamaError(Exception):
    pass


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _ToolUse:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _Response:
    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)
    model: str = ""
    via_backup: bool = False


def available(url: str | None = None, model: str | None = None) -> bool:
    """Is Ollama running here with the model pulled? (Short, synchronous check.)"""
    url, model = url or OLLAMA_URL, model or OLLAMA_MODEL
    try:
        r = httpx.get(f"{url}/api/tags", timeout=2.0)
        names = {m.get("name", "") for m in r.json().get("models", [])}
        return model in names or f"{model}:latest" in names
    except (httpx.HTTPError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Anthropic-style messages -> Ollama chat messages
# ---------------------------------------------------------------------------

def _system_text(system) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))


def _convert(messages: list, system: str) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        text, images, calls = [], [], []
        for b in content or []:
            kind = b.get("type") if isinstance(b, dict) else getattr(b, "type", "")
            if kind == "text":
                text.append(b["text"] if isinstance(b, dict) else b.text)
            elif kind == "image" and isinstance(b, dict) and b.get("source", {}).get("type") == "base64":
                images.append(b["source"]["data"])
            elif kind == "tool_use":
                name = b["name"] if isinstance(b, dict) else b.name
                args = b.get("input", {}) if isinstance(b, dict) else b.input
                calls.append({"function": {"name": name, "arguments": args}})
            elif kind == "tool_result" and isinstance(b, dict):
                result = b.get("content", "")
                if isinstance(result, list):
                    result = "\n".join(x.get("text", "") for x in result if isinstance(x, dict))
                out.append({"role": "tool", "content": str(result),
                            "tool_name": str(b.get("tool_use_id", "")).split("::")[0]})
        if text or images or calls:
            msg: dict = {"role": role, "content": "\n".join(t for t in text if t)}
            if images:
                msg["images"] = images
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def _tools(tools: list) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", ""),
        "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
        for t in tools or [] if isinstance(t, dict) and "input_schema" in t]


_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class _Messages:
    def __init__(self, client: "OllamaClient"):
        self._c = client

    async def create(self, *, max_tokens: int = 1024, messages=None, system=None, tools=None, **_ignored) -> _Response:
        return await self._c.chat(messages or [], _system_text(system), max_tokens, tools or [])


class _Beta:
    def __init__(self, client):
        self.messages = _Messages(client)


def _options(max_tokens: int) -> dict:
    opts = {"num_predict": min(max(max_tokens, 1), 2048), "num_ctx": 8192, "temperature": 0.4}
    # Integrated laptop GPUs (Intel via Vulkan) are slower than the CPU and can crash
    # mid-answer ("device lost"), so run on the CPU unless OLLAMA_GPU=1.
    if os.getenv("OLLAMA_GPU", "0") != "1":
        opts["num_gpu"] = 0
    return opts


class OllamaClient:
    provider = "ollama"

    def __init__(self, url: str | None = None, model: str | None = None, timeout: float = 420.0):
        self.url, self.model = url or OLLAMA_URL, model or OLLAMA_MODEL
        self.fast_model = self.deep_model = model
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=3.0))
        self.messages = _Messages(self)
        self.beta = _Beta(self)

    async def prewarm(self, system: str, tools: list | None = None) -> None:
        """Make the model read a fixed prompt now, so later requests that start with it reuse the cache."""
        try:
            t = time.time()
            await self.chat([{"role": "user", "content": "ok"}], system, 1, tools or [])
            log.info(f"Local model pre-loaded a {len(system) // 4}-token prompt in {time.time() - t:.0f}s")
        except Exception as e:
            log.warning(f"Local model pre-load failed: {e}")

    async def chat(self, messages: list, system: str, max_tokens: int, tools: list) -> _Response:
        body = {
            "model": self.model,
            "messages": _convert(messages, system),
            "stream": False,
            "keep_alive": "30m",
            "options": _options(max_tokens),
        }
        if _tools(tools):
            body["tools"] = _tools(tools)
        try:
            r = await self._http.post(f"{self.url}/api/chat", json=body)
        except httpx.HTTPError as e:
            raise OllamaError(f"Local model unreachable: {e}") from e
        if r.status_code != 200:
            raise OllamaError(f"Local model error {r.status_code}: {r.text[:200]}")
        data = r.json()
        msg = data.get("message", {})
        blocks: list = []
        text = _THINK.sub("", msg.get("content") or "").strip()
        if text:
            blocks.append(_Text(text))
        for i, call in enumerate(msg.get("tool_calls") or []):
            fn = call.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            blocks.append(_ToolUse(id=f"{fn.get('name', '')}::{i}", name=fn.get("name", ""), input=args))
        has_calls = any(isinstance(b, _ToolUse) for b in blocks)
        return _Response(
            content=blocks,
            stop_reason="tool_use" if has_calls else ("max_tokens" if data.get("done_reason") == "length" else "end_turn"),
            usage=_Usage(data.get("prompt_eval_count", 0), data.get("eval_count", 0)),
            model=self.model,
            via_backup=True,
        )


# ---------------------------------------------------------------------------
# Cloud first, local backup
# ---------------------------------------------------------------------------

class _FallbackMessages:
    def __init__(self, owner: "FallbackClient", beta: bool):
        self._o, self._beta = owner, beta

    async def create(self, **kw):
        return await self._o.call(self._beta, kw)


class _FallbackBeta:
    def __init__(self, owner):
        self.messages = _FallbackMessages(owner, beta=True)


def _recent(messages: list, n: int) -> list:
    recent = list(messages[-n:])
    while recent and recent[0].get("role") != "user":
        recent.pop(0)
    return recent or list(messages[-1:])


def backup_of(client) -> "OllamaClient | None":
    """The local model behind a client (the backup, or the client itself), if any."""
    if isinstance(client, OllamaClient):
        return client
    return getattr(client, "backup", None) if isinstance(client, FallbackClient) else None


class FallbackClient:
    """Use `primary` (cloud); on quota/overload/outage use `backup` (local) until the cloud should be back."""

    def __init__(self, primary, backup: OllamaClient):
        self.primary, self.backup = primary, backup
        self.provider = getattr(primary, "provider", "anthropic")
        self.messages = _FallbackMessages(self, beta=False)
        self.beta = _FallbackBeta(self)
        self.backup_until = 0.0
        self.last_used_backup = False
        self.switch_reason = ""

    @property
    def fast_model(self):
        return getattr(self.primary, "fast_model", None)

    @property
    def deep_model(self):
        return getattr(self.primary, "deep_model", None)

    async def call(self, beta: bool, kw: dict):
        import llm  # late import: llm imports this module
        backup_system = kw.pop("backup_system", None)
        now = time.time()
        if now >= self.backup_until:
            target = self.primary.beta.messages if beta else self.primary.messages
            try:
                resp = await target.create(**kw)
                if self.last_used_backup:
                    log.info("Cloud model is back; leaving the local backup")
                self.last_used_backup = False
                return resp
            except llm.FALLBACK_ERRORS as e:
                wait = float(getattr(e, "reset_in", 0) or 0) or 120.0
                self.backup_until = now + min(wait, 3600)   # re-check the cloud at least hourly
                self.switch_reason = str(e)
                log.warning(f"Cloud model unavailable ({type(e).__name__}: {str(e)[:120]}); using local {self.backup.model}")
        backup_kw = {k: v for k, v in kw.items() if k in ("max_tokens", "messages", "system", "tools")}
        if backup_system:
            backup_kw["system"] = backup_system
        if not backup_kw.get("tools"):
            # Voice turns: a CPU model reads every token, so keep only the recent conversation.
            backup_kw["messages"] = _recent(backup_kw.get("messages") or [], 6)
        # Server-side web search isn't available offline; the local model answers from what it knows.
        backup_kw["tools"] = [t for t in backup_kw.get("tools") or [] if isinstance(t, dict) and "input_schema" in t]
        resp = await self.backup.messages.create(**backup_kw)
        self.last_used_backup = True
        return resp
