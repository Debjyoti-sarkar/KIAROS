"""
KIAROS LLM helpers — one place for providers, model IDs and call shapes.

Two providers are supported:
  - Google Gemini (GEMINI_API_KEY / GOOGLE_API_KEY) via llm_gemini.GeminiClient
  - Anthropic Claude (ANTHROPIC_API_KEY)
If both keys are set, Gemini is used unless LLM_PROVIDER=anthropic.
  - A local model via Ollama (llm_ollama) — used as an automatic backup when the
    cloud is out of quota or unreachable, or as the only brain with no cloud key
    (or LLM_PROVIDER=ollama). Disable the backup with OLLAMA_BACKUP=0.
Every module goes through here so callers never care which one is active.
"""

import logging
import os

import anthropic

import httpx

import llm_ollama
from llm_gemini import GeminiAuthError, GeminiBusyError, GeminiClient, GeminiError

log = logging.getLogger("kiaros.llm")

# Exceptions that mean "the API key was rejected", for either provider.
AUTH_ERRORS = (anthropic.AuthenticationError, GeminiAuthError)
# Exceptions that mean "the provider is overloaded / rate limited — try again shortly".
BUSY_ERRORS = (anthropic.RateLimitError, anthropic.InternalServerError, GeminiBusyError)
# Anything that should send a request to the local backup instead.
FALLBACK_ERRORS = BUSY_ERRORS + AUTH_ERRORS + (anthropic.APIConnectionError, GeminiError, httpx.HTTPError)

# Fast model for the voice loop (low latency); deep model for research.
FAST_MODEL = os.getenv("KIAROS_FAST_MODEL", "claude-haiku-5-5")
DEEP_MODEL = os.getenv("KIAROS_DEEP_MODEL", "claude-opus-5-5")

# Haiku 5.5 thinks by default; "low" effort keeps voice replies snappy.
FAST_EFFORT = os.getenv("KIAROS_FAST_EFFORT", "low")


def gemini_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def anthropic_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


def _backup_enabled() -> bool:
    return os.getenv("OLLAMA_BACKUP", "1").strip() not in ("0", "false", "no", "off")


def active_provider() -> str | None:
    """'gemini', 'anthropic', 'ollama', or None when nothing is configured."""
    pref = os.getenv("LLM_PROVIDER", "").strip().lower()
    if pref == "ollama":
        return "ollama" if llm_ollama.available() else None
    have = {"gemini": bool(gemini_key()), "anthropic": bool(anthropic_key())}
    if pref in have and have[pref]:
        return pref
    if have["gemini"]:
        return "gemini"
    if have["anthropic"]:
        return "anthropic"
    if _backup_enabled() and llm_ollama.available():
        return "ollama"
    return None


def make_client(api_key: str, provider: str = "anthropic"):
    if provider == "gemini":
        return GeminiClient(api_key)
    return anthropic.AsyncAnthropic(api_key=api_key)


def client_from_env():
    """The active client: cloud (with the local backup if Ollama is ready), local only, or None."""
    provider = active_provider()
    if provider == "ollama":
        return llm_ollama.OllamaClient()
    if provider == "gemini":
        cloud = GeminiClient(gemini_key())
    elif provider == "anthropic":
        cloud = anthropic.AsyncAnthropic(api_key=anthropic_key())
    else:
        return None
    if _backup_enabled() and llm_ollama.available():
        log.info(f"Offline backup ready: {llm_ollama.OLLAMA_MODEL} via Ollama")
        return llm_ollama.FallbackClient(cloud, llm_ollama.OllamaClient())
    return cloud


def provider_of(client) -> str:
    return getattr(client, "provider", "anthropic") if client else ""


def describe(client) -> str:
    """Human-readable provider/model, e.g. 'Google Gemini (gemini-2.5-flash) + offline backup'."""
    if isinstance(client, llm_ollama.FallbackClient):
        now_local = " (using it now)" if client.last_used_backup else ""
        return f"{describe(client.primary)} + offline backup {client.backup.model}{now_local}"
    if provider_of(client) == "ollama":
        return f"Local model {client.model} (offline)"
    if provider_of(client) == "gemini":
        model = getattr(client, "fast_model", None)
        return f"Google Gemini ({model})" if model else "Google Gemini"
    if client:
        return f"Anthropic Claude ({FAST_MODEL})"
    return "not configured"


async def warm_up(client) -> None:
    """Find a working model before the user's first question (Gemini model discovery + fallback)."""
    if provider_of(client) not in ("gemini", "ollama"):
        return
    try:
        await fast_text(client, "Reply with the word ok.")
        log.info(f"AI ready: {describe(client)}")
    except Exception as e:
        log.warning(f"AI warm-up failed: {e}")


def text_of(response) -> str:
    """Join the text blocks of a Messages response.

    Current models can return thinking blocks before the text, so never
    read ``response.content[0].text`` directly.
    """
    if getattr(response, "stop_reason", None) == "refusal":
        return ""
    parts = [b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"]
    return "".join(parts).strip()


async def fast(
    client: anthropic.AsyncAnthropic,
    messages: list[dict],
    system: str | None = None,
    max_tokens: int = 1024,
    backup_system: str | None = None,
):
    """Low-latency call on the fast model. Returns the raw response."""
    kwargs = {
        "model": FAST_MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
        "output_config": {"effort": FAST_EFFORT},
    }
    if system:
        kwargs["system"] = system
    if backup_system and isinstance(client, llm_ollama.FallbackClient):
        kwargs["backup_system"] = backup_system  # shorter instructions for the small local model
    elif backup_system and provider_of(client) == "ollama":
        kwargs["system"] = backup_system
    return await client.messages.create(**kwargs)


async def fast_text(
    client: anthropic.AsyncAnthropic,
    prompt: str,
    system: str | None = None,
    max_tokens: int = 1024,
) -> str:
    """Single-turn convenience wrapper: prompt in, text out."""
    response = await fast(client, [{"role": "user", "content": prompt}], system=system, max_tokens=max_tokens)
    return text_of(response)


async def research(client: anthropic.AsyncAnthropic, brief: str, system: str) -> str:
    """Deep research on the deep model with live web search.

    Uses server-side refusal fallbacks so a classifier decline is retried on a
    fallback model instead of returning nothing. Handles ``pause_turn`` from
    the server-side web search loop by resuming up to a few times.
    """
    messages: list = [{"role": "user", "content": brief}]
    response = None
    for _ in range(4):
        response = await client.beta.messages.create(
            model=DEEP_MODEL,
            max_tokens=16000,
            system=system,
            messages=messages,
            output_config={"effort": "high"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": response.content}]
    if response is None:
        return ""
    if response.stop_reason == "refusal":
        log.warning("Research request was declined by the model")
        return ""
    return text_of(response)
