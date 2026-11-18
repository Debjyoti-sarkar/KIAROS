"""
KIAROS LLM helpers — one place for model IDs and Claude API call shapes.

Every module that talks to Claude goes through here so model upgrades are a
one-line change and response parsing is consistent.
"""

import logging
import os

import anthropic

log = logging.getLogger("kiaros.llm")

# Fast model for the voice loop (low latency); deep model for research.
FAST_MODEL = os.getenv("KIAROS_FAST_MODEL", "claude-haiku-5-5")
DEEP_MODEL = os.getenv("KIAROS_DEEP_MODEL", "claude-opus-5-5")

# Haiku 5.5 thinks by default; "low" effort keeps voice replies snappy.
FAST_EFFORT = os.getenv("KIAROS_FAST_EFFORT", "low")


def make_client(api_key: str) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=api_key)


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
