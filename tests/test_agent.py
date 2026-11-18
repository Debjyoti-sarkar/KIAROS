"""
Task agent and WhatsApp automation.

- WhatsApp Web automation runs in headless Chrome against a local stand-in page
  (tests/fixtures/whatsapp.html) — it never touches your real WhatsApp.
- The agent loop is driven by a scripted fake model, so the safety rules
  (confirm before sending / changing things, refuse sudo) are checked exactly.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

import agent as agent_mod
import web_agent
from agent import TaskAgent, classify_command

FIXTURES = Path(__file__).parent / "fixtures"
needs_chrome = pytest.mark.skipif(not shutil.which("google-chrome"), reason="Google Chrome not installed")


# ---------------------------------------------------------------------------
# WhatsApp Web
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def fake_whatsapp(monkeypatch, tmp_path):
    monkeypatch.setenv("KIAROS_BROWSER_HEADLESS", "1")
    monkeypatch.setattr(web_agent, "PROFILE_DIR", tmp_path / "profile")
    w = web_agent.WebAgent()

    async def use(page_name):
        url = (FIXTURES / page_name).as_uri()
        monkeypatch.setattr(web_agent, "WHATSAPP_URL", url)
        return w
    yield use
    await w.close()


@needs_chrome
@pytest.mark.asyncio
async def test_whatsapp_send_end_to_end(fake_whatsapp):
    w = await fake_whatsapp("whatsapp.html")
    result = await w.whatsapp_send("somesh", "How was he?")
    assert result == 'Sent to Somesh on WhatsApp: "How was he?"'   # exact match beats "Somesh Office"
    page = await w.page()
    assert await page.evaluate("window.sent") == [{"to": "Somesh", "text": "How was he?"}]


@needs_chrome
@pytest.mark.asyncio
async def test_whatsapp_unknown_contact(fake_whatsapp):
    w = await fake_whatsapp("whatsapp.html")
    assert await w.whatsapp_send("Zebedee", "hi") == "I couldn't find a WhatsApp chat named Zebedee."
    assert await (await w.page()).evaluate("window.sent || []") == []


@needs_chrome
@pytest.mark.asyncio
async def test_whatsapp_needs_login(fake_whatsapp):
    w = await fake_whatsapp("whatsapp_login.html")
    result = await w.whatsapp_send("Somesh", "hi")
    assert result.startswith("NEEDS_LOGIN") and "Linked devices" in result


@needs_chrome
@pytest.mark.asyncio
async def test_generic_browser_tools(fake_whatsapp):
    w = await fake_whatsapp("whatsapp.html")
    await w.open((FIXTURES / "whatsapp.html").as_uri())
    snapshot = await w.read()
    assert "Somesh Office" in snapshot and "INTERACTIVE ELEMENTS" in snapshot
    await w.type("Search input textbox", "mom")
    assert "College Friends" not in (await w.read()).split("INTERACTIVE")[0]


# ---------------------------------------------------------------------------
# Agent loop with a scripted model
# ---------------------------------------------------------------------------

@dataclass
class Use:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class Text:
    text: str
    type: str = "text"


@dataclass
class Resp:
    content: list
    stop_reason: str = "end_turn"


class ScriptedModel:
    """Plays back a fixed sequence of model turns and records what the agent sent."""
    provider = "gemini"

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.messages = self

    async def create(self, **kw):
        self.requests.append(kw["messages"][:])
        return self.turns.pop(0)


def hooks():
    calls = []

    async def phone(target, ws):
        calls.append(("phone", target))
        return None

    async def timer(seconds, label):
        calls.append(("timer", seconds, label))

    async def note(title, body):
        calls.append(("note", title))
        return True
    return {"phone": phone, "timer": timer, "note": note}, calls


@pytest.mark.asyncio
async def test_agent_asks_before_sending_then_finishes(monkeypatch):
    sent = []

    async def fake_send(contact, message):
        sent.append((contact, message))
        return f'Sent to {contact} on WhatsApp: "{message}"'
    monkeypatch.setattr(web_agent.web, "whatsapp_send", fake_send)

    model = ScriptedModel([
        Resp([Use("whatsapp_send::0", "whatsapp_send", {"contact": "Somesh", "message": "How was it?"})], "tool_use"),
        Resp([Text("I sent Somesh your message on WhatsApp.")]),
    ])
    h, _ = hooks()
    a = TaskAgent(h)
    first = await a.run(model, "message Somesh how was it", ws=None)
    assert first == 'Send "How was it?" to Somesh on WhatsApp? Say yes to go ahead.'
    assert sent == [] and a.pending is not None                         # nothing sent before approval

    final = await a.resume(model, approved=True, ws=None)
    assert sent == [("Somesh", "How was it?")]
    assert final == "I sent Somesh your message on WhatsApp."
    # The model saw the real tool result in its next request.
    last_user = model.requests[-1][-1]["content"][0]
    assert last_user["type"] == "tool_result" and "Sent to Somesh" in last_user["content"]


@pytest.mark.asyncio
async def test_agent_declined_step_is_not_run(monkeypatch):
    sent = []

    async def fake_send(contact, message):
        sent.append(contact)
        return "sent"
    monkeypatch.setattr(web_agent.web, "whatsapp_send", fake_send)
    model = ScriptedModel([Resp([Use("whatsapp_send::0", "whatsapp_send", {"contact": "Boss", "message": "I quit"})], "tool_use")])
    a = TaskAgent(hooks()[0])
    await a.run(model, "tell my boss I quit", ws=None)
    assert await a.resume(model, approved=False, ws=None) == "Okay, I won't do that."
    assert sent == []


@pytest.mark.asyncio
async def test_agent_runs_read_only_commands_and_refuses_sudo(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "HOME", tmp_path)
    (tmp_path / "report.txt").write_text("hello")
    model = ScriptedModel([
        Resp([Use("run_command::0", "run_command", {"command": "ls"}),
              Use("run_command::1", "run_command", {"command": "sudo rm -rf /var"})], "tool_use"),
        Resp([Text("Your home folder has report.txt; I won't run sudo.")]),
    ])
    a = TaskAgent(hooks()[0])
    result = await a.run(model, "list my files then clean /var", ws=None)
    assert result.startswith("Your home folder has report.txt")
    tool_results = model.requests[-1][-1]["content"]
    assert "report.txt" in tool_results[0]["content"]
    assert tool_results[1]["content"].startswith("REFUSED")


@pytest.mark.asyncio
async def test_agent_confirms_changes_but_not_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "HOME", tmp_path)
    model = ScriptedModel([Resp([Use("run_command::0", "run_command", {"command": f"mkdir {tmp_path}/new"})], "tool_use"),
                           Resp([Text("Created the folder.")])])
    a = TaskAgent(hooks()[0])
    assert (await a.run(model, "make a folder", ws=None)).startswith("Run this on the laptop: mkdir")
    assert not (tmp_path / "new").exists()
    assert await a.resume(model, approved=True, ws=None) == "Created the folder."
    assert (tmp_path / "new").is_dir()


def test_command_classifier():
    assert classify_command("df -h | grep sda") == "read"
    assert classify_command("git log --oneline -5") == "read"
    assert classify_command("rm notes.txt") == "confirm"
    assert classify_command("echo hi > f") == "confirm"
    assert classify_command("sudo reboot") == "forbidden"
    assert classify_command("rm -rf ~") == "forbidden"


# ---------------------------------------------------------------------------
# Gemini function calling translation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gemini_tool_calls_round_trip():
    import json
    import httpx
    from llm_gemini import GeminiClient
    seen = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [{"name": "models/gemini-3.8-flash",
                                                         "supportedGenerationMethods": ["generateContent"]}]})
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [
                {"functionCall": {"name": "list_dir", "args": {"path": "~"}}, "thoughtSignature": "SIG123"}]},
                "finishReason": "STOP"}]})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "You have 3 folders."}]},
                                                         "finishReason": "STOP"}]})

    c = GeminiClient("k")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tools = [agent_mod._tool("list_dir", "List a directory", {"path": {"type": "string"}}, ["path"])]
    r1 = await c.messages.create(messages=[{"role": "user", "content": "what's in my home?"}], tools=tools)
    assert r1.stop_reason == "tool_use"
    use = r1.content[0]
    assert (use.type, use.name, use.input) == ("tool_use", "list_dir", {"path": "~"})
    assert seen[0]["tools"][0]["functionDeclarations"][0]["name"] == "list_dir"

    msgs = [{"role": "user", "content": "what's in my home?"},
            {"role": "assistant", "content": r1.content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id, "content": "a\nb\nc"}]}]
    r2 = await c.messages.create(messages=msgs, tools=tools)
    assert r2.content[0].text == "You have 3 folders."
    model_turn, result_turn = seen[1]["contents"][1], seen[1]["contents"][2]
    assert model_turn["parts"][0]["thoughtSignature"] == "SIG123"          # signature echoed back verbatim
    assert result_turn["parts"][0]["functionResponse"] == {"name": "list_dir", "response": {"result": "a\nb\nc"}}
