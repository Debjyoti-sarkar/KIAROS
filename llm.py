"""
KIAROS LLM helpers — one place for providers, model IDs and call shapes.

Two providers are supported:
  - Google Gemini (GEMINI_API_KEY / GOOGLE_API_KEY) via llm_gemini.GeminiClient
  - Anthropic Claude (ANTHROPIC_API_KEY)
If both keys are set, Gemini is used unless LLM_PROVIDER=anthropic.
Every module goes through here so callers never care which one is active.
"""

import logging
import os

import anthropic

from llm_gemini import GeminiAuthError, GeminiBusyError, GeminiClient

log = logging.getLogger("kiaros.llm")

# Exceptions that mean "the API key was rejected", for either provider.
AUTH_ERRORS = (anthropic.AuthenticationError, GeminiAuthError)
# Exceptions that mean "the provider is overloaded / rate limited — try again shortly".
BUSY_ERRORS = (anthropic.RateLimitError, anthropic.InternalServerError, GeminiBusyError)

# Fast model for the voice loop (low latency); deep model for research.
FAST_MODEL = os.getenv("KIAROS_FAST_MODEL", "claude-haiku-5-5")
DEEP_MODEL = os.getenv("KIAROS_DEEP_MODEL", "claude-opus-5-5")

# Haiku 5.5 thinks by default; "low" effort keeps voice replies snappy.
FAST_EFFORT = os.getenv("KIAROS_FAST_EFFORT", "low")


def gemini_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def anthropic_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


def active_provider() -> str | None:
    """'gemini', 'anthropic', or None when no key is configured."""
    pref = os.getenv("LLM_PROVIDER", "").strip().lower()
    have = {"gemini": bool(gemini_key()), "anthropic": bool(anthropic_key())}
    if pref in have and have[pref]:
        return pref
    if have["gemini"]:
        return "gemini"
    if have["anthropic"]:
        return "anthropic"
    return None


def make_client(api_key: str, provider: str = "anthropic"):
    if provider == "gemini":
        return GeminiClient(api_key)
    return anthropic.AsyncAnthropic(api_key=api_key)


def client_from_env():
    """A client for the active provider, or None if no key is set."""
    provider = active_provider()
    if provider == "gemini":
        return GeminiClient(gemini_key())
    if provider == "anthropic":
        return anthropic.AsyncAnthropic(api_key=anthropic_key())
    return None


def provider_of(client) -> str:
    return getattr(client, "provider", "anthropic") if client else ""


def describe(client) -> str:
    """Human-readable provider/model, e.g. 'Google Gemini (gemini-2.5-flash)'."""
    if provider_of(client) == "gemini":
        model = getattr(client, "fast_model", None)
        return f"Google Gemini ({model})" if model else "Google Gemini"
    if client:
        return f"Anthropic Claude ({FAST_MODEL})"
    return "not configured"


async def warm_up(client) -> None:
    """Find a working model before the user's first question (Gemini model discovery + fallback)."""
    if provider_of(client) != "gemini":
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
