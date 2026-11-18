"""
KIAROS Google Gemini provider.

GeminiClient exposes the small part of the Anthropic client interface KIAROS
uses — ``client.messages.create(...)`` and ``client.beta.messages.create(...)``
returning objects with ``.content`` text blocks, ``.stop_reason`` and
``.usage`` — so every caller (voice loop, research, screen vision, memory,
planner) works unchanged with a Google API key.

Talks to the Gemini REST API (generativelanguage.googleapis.com) with httpx.
Models are discovered from the key's model list unless GEMINI_FAST_MODEL /
GEMINI_DEEP_MODEL are set, so new Gemini releases are picked up automatically.
"""

import logging
import os
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("kiaros.gemini")

API = "https://generativelanguage.googleapis.com/v1beta"


class GeminiAuthError(Exception):
    """The Google API key was rejected."""


class GeminiError(Exception):
    pass


class GeminiBusyError(GeminiError):
    """Every candidate model was overloaded, rate-limited or timed out."""


class GeminiQuotaError(GeminiBusyError):
    """The key's quota is used up for every model; `reset_in` is seconds until the earliest reset."""

    def __init__(self, message: str, reset_in: float = 0, daily: bool = False):
        super().__init__(message)
        self.reset_in = reset_in
        self.daily = daily


def parse_quota(message: str) -> tuple[bool, float]:
    """(is_daily_limit, seconds_until_retry) from Google's 429 text, e.g. 'per day … retry in 18h7m19s'."""
    daily = bool(re.search(r"per ?day|PerDay", message or "", re.I))
    m = re.search(r"retry in\s+(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", message or "", re.I)
    seconds = 0.0
    if m and any(m.groups()):
        h, mi, se = m.groups()
        seconds = int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)
    if not seconds:
        seconds = 6 * 3600 if daily else 60
    return daily, seconds


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        h, m = divmod(seconds // 60, 60)
        return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minutes" if m and h < 3 else "")
    if seconds >= 60:
        return f"{seconds // 60} minutes"
    return f"{seconds} seconds"


# Statuses worth retrying / falling back on: rate limit, server error, overloaded, gateway timeout.
_RETRYABLE = {429, 500, 502, 503, 504}


def explain_key_error(message: str) -> str:
    """Turn Google's error text into an actionable sentence (never includes the key)."""
    m = message or ""
    low = m.lower()
    if "api key not valid" in low or "api_key_invalid" in low:
        return ("Google says this key isn't valid. Copy it again from aistudio.google.com/apikey "
                "(the whole key, starting with AIza) and paste it with nothing before or after.")
    if "has not been used in project" in low or "is disabled" in low or "service_disabled" in low:
        link = re.search(r"https://console\.developers\.google\.com/\S+", m)
        return ("This key's Google Cloud project hasn't enabled the Gemini API (Generative Language API). "
                "Enable it" + (f" here: {link.group(0).rstrip('.')}" if link else " in Google Cloud Console → APIs & Services") +
                ", wait a minute, then test again. Or create a key at aistudio.google.com/apikey, which is enabled automatically.")
    if "are blocked" in low or "api_key_service_blocked" in low or "blocked" in low:
        return ("This key is restricted to other Google APIs. In Google Cloud Console → Credentials → this key → "
                "API restrictions, allow 'Generative Language API' (or choose 'Don't restrict key').")
    if "expired" in low:
        return "This API key has expired. Create a new one at aistudio.google.com/apikey."
    if "location is not supported" in low:
        return "Google doesn't offer the Gemini API from your current location."
    if "referer" in low or "http_referrer" in low or "ip address" in low or "android" in low and "restrict" in low:
        return ("This key only works from certain websites, IP addresses or apps. Remove the 'Application restrictions' "
                "on the key in Google Cloud Console → Credentials, or create an unrestricted key at aistudio.google.com/apikey.")
    return f"Google refused the key: {m[:240]}"


@dataclass
class _Text:
    text: str
    type: str = "text"
    raw: dict | None = None  # original Gemini part (carries thoughtSignature)


@dataclass
class _ToolUse:
    id: str
    name: str
    input: dict
    type: str = "tool_use"
    raw: dict | None = None


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


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

_SKIP = re.compile(r"lite|image|tts|audio|live|embedding|vision|learnlm|aqa|gemma|robotics|computer|exp|preview-tts|native|omni|customtools", re.I)


def _version_key(name: str) -> tuple:
    """Sort key: higher Gemini version first, stable before preview."""
    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
    major, minor = (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)
    stable = 0 if ("preview" in name or "exp" in name) else 1
    dated = 0 if re.search(r"-\d{2}-\d{2}$|-\d{3}$", name) else 1  # prefer the alias over dated snapshots
    return (major, minor, stable, dated)


def rank_models(names: list[str]) -> tuple[list[str], list[str]]:
    """(flash models, pro models), best first: newest version, stable before preview."""
    usable = [n for n in names if n.startswith("gemini-") and not _SKIP.search(n)]
    flash = sorted((n for n in usable if "flash" in n), key=_version_key, reverse=True)
    pro = sorted((n for n in usable if "pro" in n), key=_version_key, reverse=True)
    return flash, pro


def pick_models(names: list[str]) -> tuple[str | None, str | None]:
    """Choose (fast, deep) from a list like ['gemini-2.5-flash', 'gemini-2.5-pro', ...]."""
    flash, pro = rank_models(names)
    fast = flash[0] if flash else (pro[0] if pro else None)
    deep = pro[0] if pro else fast
    return fast, deep


# ---------------------------------------------------------------------------
# Request translation (Anthropic-style → Gemini)
# ---------------------------------------------------------------------------

def _tool_name(tool_use_id: str) -> str:
    return tool_use_id.split("::", 1)[0]


def _parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content}]
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            # Our own response objects: echo the original part so Gemini gets its thoughtSignature back.
            raw = getattr(block, "raw", None)
            if raw:
                parts.append(raw)
            elif getattr(block, "type", "") == "tool_use":
                parts.append({"functionCall": {"name": block.name, "args": block.input}})
            elif getattr(block, "text", None):
                parts.append({"text": block.text})
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            parts.append({"text": block["text"]})
        elif kind == "tool_use":
            parts.append({"functionCall": {"name": block["name"], "args": block.get("input", {})}})
        elif kind == "tool_result":
            result = block.get("content", "")
            if isinstance(result, list):
                result = "\n".join(b.get("text", "") for b in result if isinstance(b, dict))
            parts.append({"functionResponse": {"name": _tool_name(block.get("tool_use_id", "")),
                                               "response": {"result": str(result)}}})
        elif kind == "image" and block.get("source", {}).get("type") == "base64":
            src = block["source"]
            parts.append({"inlineData": {"mimeType": src.get("media_type", "image/png"), "data": src["data"]}})
    return parts


def _contents(messages: list[dict]) -> list[dict]:
    """Gemini wants user/model turns, starting with the user; merge repeats."""
    out: list[dict] = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        parts = _parts(m.get("content"))
        if not parts:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["parts"].extend(parts)
        else:
            out.append({"role": role, "parts": parts})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _system_text(system) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))


def _clean_schema(schema: dict) -> dict:
    """JSON Schema -> the OpenAPI subset Gemini function declarations accept."""
    out = {}
    for k, v in schema.items():
        if k in ("additionalProperties", "$schema", "default", "examples"):
            continue
        if k == "properties":
            out[k] = {name: _clean_schema(prop) for name, prop in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _clean_schema(v)
        else:
            out[k] = v
    return out


def _declaration(tool: dict) -> dict:
    return {"name": tool["name"], "description": tool.get("description", ""),
            "parameters": _clean_schema(tool.get("input_schema", {"type": "object", "properties": {}}))}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class _Messages:
    def __init__(self, client: "GeminiClient"):
        self._c = client

    async def create(self, *, model: str = "", max_tokens: int = 1024, messages=None, system=None,
                     tools=None, **_ignored) -> _Response:
        tools = tools or []
        wants_search = any(isinstance(t, dict) and str(t.get("type", "")).startswith("web_search") for t in tools)
        functions = [t for t in tools if isinstance(t, dict) and "input_schema" in t]
        deep = wants_search or "opus" in (model or "")
        return await self._c.generate(messages or [], _system_text(system), max_tokens, deep=deep,
                                      search=wants_search, functions=functions)


class _Beta:
    def __init__(self, client: "GeminiClient"):
        self.messages = _Messages(client)


class GeminiClient:
    provider = "gemini"

    def __init__(self, api_key: str, timeout: float = 60.0):
        self.api_key = api_key
        self._http = httpx.AsyncClient(timeout=timeout, headers={"x-goog-api-key": api_key})
        self.messages = _Messages(self)
        self.beta = _Beta(self)
        self.fast_model = os.getenv("GEMINI_FAST_MODEL", "").strip() or None
        self.deep_model = os.getenv("GEMINI_DEEP_MODEL", "").strip() or None
        # Fallback chains, tried in order when a model is overloaded (filled on discovery).
        self.fast_chain: list[str] = [self.fast_model] if self.fast_model else []
        self.deep_chain: list[str] = [self.deep_model] if self.deep_model else []
        self._models_loaded = bool(self.fast_model and self.deep_model)
        self._cooldown: dict[str, float] = {}     # model -> busy cooldown end (tried last)
        self._quota_until: dict[str, float] = {}  # model -> quota reset time (skipped)
        self._quota_daily: dict[str, bool] = {}

    async def list_models(self) -> list[str]:
        names, token = [], None
        while True:
            r = await self._http.get(f"{API}/models", params={"pageSize": 1000, **({"pageToken": token} if token else {})})
            self._raise_for(r)
            data = r.json()
            for m in data.get("models", []):
                if "generateContent" in m.get("supportedGenerationMethods", []):
                    names.append(m["name"].removeprefix("models/"))
            token = data.get("nextPageToken")
            if not token:
                return names

    async def _ensure_models(self):
        if self._models_loaded:
            return
        flash, pro = rank_models(await self.list_models())
        if not flash and not pro:
            raise GeminiError("This Google API key has no Gemini text models available.")

        def chain(pinned, ranked, limit=6):
            out = [pinned] if pinned else []
            out += [m for m in ranked if m not in out]
            return out[: limit + (1 if pinned else 0)]

        self.fast_chain = chain(self.fast_model, flash or pro)
        # Research: Pro models first, then Flash as a last resort.
        self.deep_chain = chain(self.deep_model, pro + [m for m in flash if m not in pro], limit=8)
        self.fast_model, self.deep_model = self.fast_chain[0], self.deep_chain[0]
        self._models_loaded = True
        log.info(f"Gemini models: fast={self.fast_chain} deep={self.deep_chain}")

    @staticmethod
    def _raise_for(r: httpx.Response):
        if r.status_code < 400:
            return
        try:
            err = r.json().get("error", {})
            msg, status = err.get("message", r.text[:200]), err.get("status", "")
        except ValueError:
            msg, status = r.text[:200], ""
        if r.status_code in (401, 403) or "API key not valid" in msg or status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
            raise GeminiAuthError(msg)
        raise GeminiError(f"HTTP {r.status_code}: {msg}")

    async def generate(self, messages: list[dict], system: str, max_tokens: int,
                       deep: bool = False, search: bool = False, functions: list | None = None) -> _Response:
        await self._ensure_models()
        model = self.deep_model if deep else self.fast_model
        body: dict = {
            "contents": _contents(messages),
            # Thinking tokens count against the output budget on Gemini, so leave headroom.
            "generationConfig": {"maxOutputTokens": max(max_tokens, 2048)},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if functions:
            body["tools"] = [{"functionDeclarations": [_declaration(f) for f in functions]}]
        elif search:
            body["tools"] = [{"googleSearch": {}}]
        chain = self.deep_chain if deep else self.fast_chain
        data, model = await self._post_with_fallback(chain, body, deep)
        cand = (data.get("candidates") or [{}])[0]
        raw_parts = cand.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in raw_parts if not p.get("thought"))
        blocks: list = []
        for i, part in enumerate(raw_parts):
            if part.get("functionCall"):
                call = part["functionCall"]
                blocks.append(_ToolUse(id=f"{call.get('name')}::{i}", name=call.get("name", ""),
                                       input=call.get("args") or {}, raw=part))
            elif part.get("text") and not part.get("thought"):
                blocks.append(_Text(part["text"], raw=part))
            elif part.get("thoughtSignature"):
                blocks.append(_Text("", raw=part))  # keep signatures even on empty parts
        finish = cand.get("finishReason", "STOP")
        usage = data.get("usageMetadata", {})
        blocked = finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION") or (
            not cand and data.get("promptFeedback", {}).get("blockReason"))
        has_calls = any(isinstance(b, _ToolUse) for b in blocks)
        if not functions:
            blocks = [_Text(text)] if text else []
        return _Response(
            content=blocks,
            stop_reason="refusal" if blocked else ("tool_use" if has_calls else
                                                  "max_tokens" if finish == "MAX_TOKENS" else "end_turn"),
            usage=_Usage(usage.get("promptTokenCount", 0),
                         usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)),
            model=model,
        )

    async def _post_with_fallback(self, chain: list[str], body: dict, deep: bool) -> tuple[dict, str]:
        """Try each model in the chain; retry once on overload; promote whichever works."""
        import asyncio
        import time as _t
        timeout = httpx.Timeout(240.0 if deep else 25.0, connect=10.0)
        last_error = ""
        now = _t.time()
        # Skip models whose quota is known to be used up; busy-cooldown ones go last.
        available = [m for m in chain if self._quota_until.get(m, 0) <= now]
        if not available:
            reset = min(self._quota_until.get(m, now) for m in chain) - now
            daily = any(self._quota_daily.get(m) for m in chain)
            raise GeminiQuotaError(
                f"Your {'free ' if daily else ''}Gemini limit is used up; it resets in {human_duration(reset)}.",
                reset_in=reset, daily=daily)
        ordered = [m for m in available if self._cooldown.get(m, 0) <= now] + \
                  [m for m in available if self._cooldown.get(m, 0) > now]
        for model in ordered:
            for attempt in range(2):
                try:
                    r = await self._http.post(f"{API}/models/{model}:generateContent", json=body, timeout=timeout)
                except httpx.TimeoutException:
                    last_error = f"{model} timed out"
                    log.warning(last_error)
                    break  # a slow model rarely recovers within a voice turn: try the next one
                except httpx.HTTPError as e:
                    raise GeminiError(f"Couldn't reach Google: {e}") from e
                if r.status_code == 429:
                    # Quota for this model is used up (free-tier limits are per model): skip it until reset.
                    try:
                        msg = r.json().get("error", {}).get("message", "")
                    except ValueError:
                        msg = ""
                    daily, wait = parse_quota(msg)
                    self._quota_until[model] = _t.time() + wait
                    self._quota_daily[model] = daily
                    last_error = f"{model}: {'daily' if daily else 'rate'} quota exceeded (resets in {human_duration(wait)})"
                    log.warning(f"Gemini {last_error}, trying next model")
                    break
                if r.status_code in _RETRYABLE:
                    last_error = f"{model} HTTP {r.status_code}"
                    log.warning(f"Gemini {last_error}" + (", retrying" if attempt == 0 else ", trying next model"))
                    if attempt == 0:
                        await asyncio.sleep(0.8)
                        continue
                    self._cooldown[model] = _t.time() + 60
                    break
                if r.status_code == 404:  # model withdrawn for this key: drop it
                    last_error = f"{model} not available"
                    if model in chain and len(chain) > 1:
                        chain.remove(model)
                    break
                self._raise_for(r)
                if model != chain[0]:
                    # Keep using the model that works; the newest one is overloaded right now.
                    chain.remove(model)
                    chain.insert(0, model)
                    if deep:
                        self.deep_model = model
                    else:
                        self.fast_model = model
                    log.info(f"Gemini switched to {model}")
                return r.json(), model
        now = _t.time()
        if all(self._quota_until.get(m, 0) > now for m in chain):
            reset = min(self._quota_until[m] for m in chain) - now
            daily = any(self._quota_daily.get(m) for m in chain)
            raise GeminiQuotaError(
                f"Your {'free ' if daily else ''}Gemini limit is used up; it resets in {human_duration(reset)}.",
                reset_in=reset, daily=daily)
        raise GeminiBusyError(f"Gemini is busy right now ({last_error}).")
