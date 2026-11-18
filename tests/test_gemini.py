"""
Google Gemini provider: model choice, request translation and response parsing,
against a fake Gemini API (no key or network needed). One test checks the real
endpoint's error contract using a deliberately invalid key.
"""

import json

import httpx
import pytest

import llm
import llm_gemini
from llm_gemini import GeminiAuthError, GeminiClient, pick_models


MODELS = [
    "gemini-1.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-2.5-flash",
    "gemini-2.5-flash-preview-05-20", "gemini-2.5-flash-image", "gemini-2.5-pro",
    "gemini-2.5-flash-preview-tts", "text-embedding-004", "gemma-3-27b-it",
]


def test_picks_newest_stable_flash_and_pro():
    assert pick_models(MODELS) == ("gemini-2.5-flash", "gemini-2.5-pro")
    assert pick_models(MODELS + ["gemini-3-flash", "gemini-3-pro"]) == ("gemini-3-flash", "gemini-3-pro")
    assert pick_models(["gemini-2.5-flash"]) == ("gemini-2.5-flash", "gemini-2.5-flash")
    assert pick_models(["text-embedding-004"]) == (None, None)


def fake_api(seen: list, reply: dict | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [
                {"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]} for m in MODELS]})
        seen.append({"url": str(request.url), "key": request.headers.get("x-goog-api-key"),
                     "body": json.loads(request.content)})
        if status != 200:
            return httpx.Response(status, json={"error": {"code": status, "message": "API key not valid. Please pass a valid API key.",
                                                          "status": "INVALID_ARGUMENT"}})
        return httpx.Response(200, json=reply or {
            "candidates": [{"content": {"role": "model", "parts": [
                {"text": "thinking...", "thought": True}, {"text": "Hello "}, {"text": "there."}]},
                "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3, "thoughtsTokenCount": 5},
        })
    return httpx.MockTransport(handler)


def client_with(transport) -> GeminiClient:
    c = GeminiClient("test-key")
    c._http = httpx.AsyncClient(transport=transport, headers={"x-goog-api-key": "test-key"})
    return c


@pytest.mark.asyncio
async def test_conversation_call_is_translated():
    seen = []
    c = client_with(fake_api(seen))
    history = [
        {"role": "assistant", "content": "Good morning."},          # leading model turn is dropped
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "are you there?"},              # merged with previous user turn
        {"role": "assistant", "content": "Yes."},
        {"role": "user", "content": "what's up"},
    ]
    resp = await llm.fast(c, history, system="Be brief.")
    assert llm.text_of(resp) == "Hello there."                      # thought parts are excluded
    assert resp.usage.input_tokens == 12 and resp.usage.output_tokens == 8
    req = seen[0]
    assert req["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert req["key"] == "test-key"
    assert req["body"]["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert [m["role"] for m in req["body"]["contents"]] == ["user", "model", "user"]
    assert req["body"]["contents"][0]["parts"] == [{"text": "hi"}, {"text": "are you there?"}]
    assert "tools" not in req["body"]


@pytest.mark.asyncio
async def test_screen_vision_sends_inline_image():
    seen = []
    c = client_with(fake_api(seen))
    await llm.fast(c, [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
        {"type": "text", "text": "What's on my screen?"},
    ]}])
    parts = seen[0]["body"]["contents"][0]["parts"]
    assert parts == [{"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}}, {"text": "What's on my screen?"}]


@pytest.mark.asyncio
async def test_research_uses_pro_model_with_google_search():
    seen = []
    c = client_with(fake_api(seen))
    text = await llm.research(c, "best laptops 2026", system="Research.")
    assert text == "Hello there."
    assert seen[0]["url"].endswith("/models/gemini-2.5-pro:generateContent")
    assert seen[0]["body"]["tools"] == [{"googleSearch": {}}]


@pytest.mark.asyncio
async def test_blocked_answer_reads_as_refusal():
    seen = []
    c = client_with(fake_api(seen, reply={"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]}))
    resp = await llm.fast(c, [{"role": "user", "content": "x"}])
    assert resp.stop_reason == "refusal" and llm.text_of(resp) == ""


@pytest.mark.asyncio
async def test_bad_key_raises_auth_error():
    c = client_with(fake_api([], status=400))
    with pytest.raises(GeminiAuthError):
        await llm.fast(c, [{"role": "user", "content": "hi"}])
    assert issubclass(GeminiAuthError, llm.AUTH_ERRORS)


def test_provider_selection(monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    assert llm.active_provider() is None and llm.client_from_env() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert llm.active_provider() == "anthropic"
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
    assert llm.active_provider() == "gemini"                      # Gemini wins when both are set
    assert isinstance(llm.client_from_env(), GeminiClient)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert llm.active_provider() == "anthropic"


@pytest.mark.asyncio
async def test_real_endpoint_rejects_invalid_key():
    """Checks the live Gemini API contract (URL, auth header, error shape) with a fake key."""
    c = GeminiClient("AIzaSy-invalid-key-for-kiaros-tests")
    try:
        await c.list_models()
    except GeminiAuthError:
        return
    except httpx.HTTPError as e:
        pytest.skip(f"no network: {e}")
    pytest.fail("an invalid key was accepted")


def test_key_errors_are_explained():
    from llm_gemini import explain_key_error
    assert "isn't valid" in explain_key_error("API key not valid. Please pass a valid API key.")
    assert "hasn't enabled the Gemini API" in explain_key_error(
        "Generative Language API has not been used in project 1 before or it is disabled.")
    assert "restricted to other Google APIs" in explain_key_error(
        "Requests to this API generativelanguage.googleapis.com method x are blocked.")
    assert explain_key_error("something new").startswith("Google refused the key:")


def overloaded_api(calls: list, busy: set, flaky_once: set = frozenset()):
    """Fake Gemini where models in `busy` always return 503 and `flaky_once` fail only the first time."""
    failed = set()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [
                {"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]}
                for m in ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.5-flash", "gemini-3.1-pro-preview"]]})
        model = request.url.path.split("/models/")[1].split(":")[0]
        calls.append(model)
        if model in busy or (model in flaky_once and model not in failed):
            failed.add(model)
            return httpx.Response(503, json={"error": {"code": 503, "message": "This model is currently experiencing high demand.",
                                                       "status": "UNAVAILABLE"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": f"answer from {model}"}]},
                                                         "finishReason": "STOP"}]})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_overloaded_model_falls_back_and_sticks(monkeypatch):
    monkeypatch.setattr(llm_gemini, "_RETRYABLE", llm_gemini._RETRYABLE)
    calls = []
    c = client_with(overloaded_api(calls, busy={"gemini-3.8-flash"}))
    r1 = await llm.fast(c, [{"role": "user", "content": "hi"}])
    assert llm.text_of(r1) == "answer from gemini-3.5-flash"
    assert calls == ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.5-flash"]  # one retry, then next model
    calls.clear()
    r2 = await llm.fast(c, [{"role": "user", "content": "again"}])
    assert llm.text_of(r2) == "answer from gemini-3.5-flash" and calls == ["gemini-3.5-flash"]  # sticks


@pytest.mark.asyncio
async def test_transient_blip_is_retried_on_same_model():
    calls = []
    c = client_with(overloaded_api(calls, busy=set(), flaky_once={"gemini-3.8-flash"}))
    r = await llm.fast(c, [{"role": "user", "content": "hi"}])
    assert llm.text_of(r) == "answer from gemini-3.8-flash" and calls == ["gemini-3.8-flash", "gemini-3.8-flash"]


@pytest.mark.asyncio
async def test_everything_busy_raises_busy_error():
    from llm_gemini import GeminiBusyError
    c = client_with(overloaded_api([], busy={"gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.5-flash", "gemini-3.1-pro-preview"}))
    with pytest.raises(GeminiBusyError):
        await llm.fast(c, [{"role": "user", "content": "hi"}])
    assert issubclass(GeminiBusyError, llm.BUSY_ERRORS)


@pytest.mark.asyncio
async def test_quota_exhausted_models_are_skipped_without_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [{"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]}
                                                        for m in ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview",
                                                                  "gemini-omni-1.2-flash"]]})
        model = request.url.path.split("/models/")[1].split(":")[0]
        calls.append(model)
        if model != "gemini-3-flash-preview":
            return httpx.Response(429, json={"error": {"code": 429, "message": "You exceeded your current quota", "status": "RESOURCE_EXHAUSTED"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})

    c = client_with(httpx.MockTransport(handler))
    r = await llm.fast(c, [{"role": "user", "content": "hi"}])
    assert llm.text_of(r) == "ok"
    assert calls == ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview"]  # no retries, omni skipped
    assert c.fast_model == "gemini-3-flash-preview"
