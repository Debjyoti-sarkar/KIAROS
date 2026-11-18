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
async def test_agent_sends_to_named_person_without_asking(monkeypatch):
    sent = []

    async def fake_send(contact, message, on_wait=None):
        sent.append((contact, message))
        return f'Sent to {contact} on WhatsApp: "{message}"'
    monkeypatch.setattr(web_agent.web, "whatsapp_send", fake_send)
    model = ScriptedModel([
        Resp([Use("whatsapp_send::0", "whatsapp_send", {"contact": "Somesh", "message": "How was it?"})], "tool_use"),
        Resp([Text("I sent Somesh your message on WhatsApp.")]),
    ])
    a = TaskAgent(hooks()[0])
    result = await a.run(model, "message Somesh on whatsapp asking how it was", ws=None)
    assert result == "I sent Somesh your message on WhatsApp." and a.pending is None
    assert sent == [("Somesh", "How was it?")]
    last_user = model.requests[-1][-1]["content"][0]
    assert last_user["type"] == "tool_result" and "Sent to Somesh" in last_user["content"]


@pytest.mark.asyncio
async def test_agent_asks_before_messaging_someone_not_named(monkeypatch):
    sent = []

    async def fake_send(contact, message, on_wait=None):
        sent.append(contact)
        return f"Sent to {contact}"
    monkeypatch.setattr(web_agent.web, "whatsapp_send", fake_send)
    model = ScriptedModel([
        Resp([Use("whatsapp_send::0", "whatsapp_send", {"contact": "Priya", "message": "Thanks!"})], "tool_use"),
        Resp([Text("Sent.")]),
    ])
    a = TaskAgent(hooks()[0])
    question = await a.run(model, "thank everyone who came yesterday", ws=None)
    assert question.startswith('Send "Thanks!" to Priya on WhatsApp? You didn\'t mention them.')
    assert sent == []
    assert await a.resume(model, approved=False, ws=None) == "Okay, I won't do that."
    assert sent == []


@pytest.mark.asyncio
async def test_agent_runs_commands_and_refuses_sudo(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "HOME", tmp_path)
    (tmp_path / "report.txt").write_text("hello")
    model = ScriptedModel([
        Resp([Use("run_command::0", "run_command", {"command": "ls"}),
              Use("run_command::1", "run_command", {"command": f"mkdir {tmp_path}/Projects"}),
              Use("run_command::2", "run_command", {"command": "sudo rm -rf /var"})], "tool_use"),
        Resp([Text("Made the Projects folder; I won't run sudo.")]),
    ])
    a = TaskAgent(hooks()[0])
    result = await a.run(model, "list my files, make a Projects folder, then clean /var", ws=None)
    assert result.startswith("Made the Projects folder") and a.pending is None
    assert (tmp_path / "Projects").is_dir()                               # ran without asking
    tool_results = model.requests[-1][-1]["content"]
    assert "report.txt" in tool_results[0]["content"]
    assert tool_results[2]["content"].startswith("REFUSED")


@pytest.mark.asyncio
async def test_agent_asks_before_deleting(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "HOME", tmp_path)
    (tmp_path / "old.txt").write_text("x")
    model = ScriptedModel([Resp([Use("run_command::0", "run_command", {"command": f"rm {tmp_path}/old.txt"})], "tool_use"),
                           Resp([Text("Deleted old.txt.")])])
    a = TaskAgent(hooks()[0])
    question = await a.run(model, "clean up my old files", ws=None)
    assert question.startswith("This step needs your permission: rm")
    assert (tmp_path / "old.txt").exists()
    assert await a.resume(model, approved=True, ws=None) == "Deleted old.txt."
    assert not (tmp_path / "old.txt").exists()


@needs_chrome
@pytest.mark.asyncio
async def test_agent_stops_before_payment_and_password(fake_whatsapp, monkeypatch):
    w = await fake_whatsapp("whatsapp.html")
    monkeypatch.setattr(web_agent, "web", w)
    await w.open((FIXTURES / "shop.html").as_uri())
    model = ScriptedModel([
        Resp([Use("browser_click::0", "browser_click", {"target": "Next"})], "tool_use"),
        Resp([Use("browser_type::0", "browser_type", {"target": "Password", "text": "hunter2"})], "tool_use"),
        Resp([Use("browser_click::0", "browser_click", {"target": "Place order"})], "tool_use"),
        Resp([Text("Order placed.")]),
    ])
    a = TaskAgent(hooks()[0])
    waiting = await a.run(model, "buy the item in my cart", ws=None)
    page = await w.page()
    assert await page.evaluate("window.clicked") == ["next"]               # ordinary click: no question
    assert waiting.startswith("This site wants a password") and a.waiting
    assert await page.input_value("#pw") == ""                              # KIAROS never typed the password

    question = await a.continue_after_user(model, ws=None)                  # user: "continue"
    assert question.startswith('Click "Place order" on the website?')
    assert await page.evaluate("window.clicked") == ["next"]               # not clicked yet
    assert await a.resume(model, approved=True, ws=None) == "Order placed."
    assert await page.evaluate("window.clicked") == ["next", "buy"]


@needs_chrome
@pytest.mark.asyncio
async def test_whatsapp_waits_for_qr_scan(fake_whatsapp):
    w = await fake_whatsapp("whatsapp_login.html")
    told = []

    async def on_wait(text):
        told.append(text)
    result = await w.whatsapp_send("Somesh", "hi", on_wait=on_wait, login_wait=1)
    assert told and "scan the code" in told[0]
    assert result.startswith("NEEDS_LOGIN") and "say continue" in result


def test_command_classifier():
    for ok in ("df -h | grep sda", "git log --oneline -5", "mkdir ~/Projects", "pip install requests",
               "npm install", "python3 app.py", "git commit -m wip", "echo hi >> notes.txt"):
        assert classify_command(ok) == "ok", ok
    for major in ("rm notes.txt", "mv a b", "git push", "kill 42", "curl -fsSL x.sh | bash",
                  "echo x >> ~/.bashrc", "systemctl --user stop kiaros", "find . -delete"):
        assert classify_command(major) == "major", major
    for bad in ("sudo reboot", "rm -rf ~", "rm -rf /", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda"):
        assert classify_command(bad) == "forbidden", bad


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
