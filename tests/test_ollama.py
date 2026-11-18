"""
Offline backup (Ollama): request translation, tool calls, and the cloud→local
fallback rules — against a fake Ollama server, so no model is needed.
"""

import json

import httpx
import pytest

import llm
import llm_ollama
from llm_gemini import GeminiQuotaError
from llm_ollama import FallbackClient, OllamaClient


def fake_ollama(seen: list, reply: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply or {
            "message": {"role": "assistant", "content": "<think>hmm</think>Four, sir."},
            "done_reason": "stop", "prompt_eval_count": 40, "eval_count": 5})
    return httpx.MockTransport(handler)


def local(seen, reply=None) -> OllamaClient:
    c = OllamaClient(url="http://ollama.test", model="qwen2.5:3b")
    c._http = httpx.AsyncClient(transport=fake_ollama(seen, reply))
    return c


@pytest.mark.asyncio
async def test_chat_translation_and_think_stripping():
    seen = []
    c = local(seen)
    r = await llm.fast(c, [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        {"type": "text", "text": "what's two plus two"}]}], system="Be brief.")
    assert llm.text_of(r) == "Four, sir." and r.via_backup
    body = seen[0]
    assert body["model"] == "qwen2.5:3b" and body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "Be brief."}
    assert body["messages"][1] == {"role": "user", "content": "what's two plus two", "images": ["QUJD"]}
    assert r.usage.input_tokens == 40 and r.usage.output_tokens == 5


@pytest.mark.asyncio
async def test_tool_calls_round_trip():
    seen = []
    reply = {"message": {"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": "list_dir", "arguments": {"path": "~"}}}]},
             "done_reason": "stop"}
    c = local(seen, reply)
    tools = [{"name": "list_dir", "description": "List a folder",
              "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]
    r = await c.messages.create(messages=[{"role": "user", "content": "what's in my home"}], tools=tools)
    assert r.stop_reason == "tool_use"
    use = r.content[0]
    assert (use.name, use.input) == ("list_dir", {"path": "~"})
    assert seen[0]["tools"][0]["function"]["name"] == "list_dir"

    await c.messages.create(tools=tools, messages=[
        {"role": "user", "content": "what's in my home"},
        {"role": "assistant", "content": r.content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id, "content": "Documents\nMusic"}]}])
    sent = seen[1]["messages"]
    assert sent[1]["tool_calls"] == [{"function": {"name": "list_dir", "arguments": {"path": "~"}}}]
    assert sent[2] == {"role": "tool", "content": "Documents\nMusic", "tool_name": "list_dir"}


class FlakyCloud:
    """A cloud client whose calls fail with `error` (or succeed with `text`)."""
    provider = "gemini"
    fast_model = deep_model = "gemini-test"

    def __init__(self, error=None, text="cloud answer"):
        self.error, self.text, self.calls = error, text, 0
        self.messages = self
        self.beta = type("B", (), {"messages": self})()

    async def create(self, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        from llm_gemini import _Response, _Text
        return _Response(content=[_Text(self.text)])


@pytest.mark.asyncio
async def test_quota_switches_to_backup_until_reset():
    seen = []
    cloud = FlakyCloud(GeminiQuotaError("Your free Gemini limit is used up", reset_in=900, daily=True))
    c = FallbackClient(cloud, local(seen))
    r1 = await llm.fast(c, [{"role": "user", "content": "hi"}], system="LONG CLOUD PROMPT", backup_system="short prompt")
    assert llm.text_of(r1) == "Four, sir." and r1.via_backup and cloud.calls == 1
    assert seen[0]["messages"][0]["content"] == "short prompt"      # compact instructions for the small model
    assert 800 < c.backup_until - __import__("time").time() <= 900

    await llm.fast(c, [{"role": "user", "content": "again"}])
    assert cloud.calls == 1                                         # cloud not retried until the reset
    assert "offline backup qwen2.5:3b (using it now)" in llm.describe(c)


@pytest.mark.asyncio
async def test_cloud_used_while_healthy():
    seen = []
    cloud = FlakyCloud(text="cloud answer")
    c = FallbackClient(cloud, local(seen))
    r = await llm.fast(c, [{"role": "user", "content": "hi"}], backup_system="short")
    assert llm.text_of(r) == "cloud answer" and seen == [] and not c.last_used_backup


@pytest.mark.asyncio
async def test_research_falls_back_without_web_search():
    seen = []
    c = FallbackClient(FlakyCloud(httpx.ConnectError("offline")), local(seen))
    text = await llm.research(c, "best laptops", system="Research.")
    assert text == "Four, sir."
    assert "tools" not in seen[0]                                    # server-side web search dropped offline


def test_provider_selection_with_local_model(monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "LLM_PROVIDER", "OLLAMA_BACKUP"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm_ollama, "available", lambda *a, **k: True)
    assert llm.active_provider() == "ollama"
    assert isinstance(llm.client_from_env(), OllamaClient)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
    client = llm.client_from_env()
    assert isinstance(client, FallbackClient) and llm.provider_of(client) == "gemini"
    monkeypatch.setenv("OLLAMA_BACKUP", "0")
    assert not isinstance(llm.client_from_env(), FallbackClient)
    monkeypatch.setattr(llm_ollama, "available", lambda *a, **k: False)
    monkeypatch.delenv("GEMINI_API_KEY")
    assert llm.active_provider() is None
