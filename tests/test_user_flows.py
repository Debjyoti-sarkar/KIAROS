"""
End-user flows against the real server, over the same WebSocket/HTTP protocol the
apps use. The "laptop" connects over loopback; the "phone" connects from this
machine's LAN address, exactly like a phone on the same Wi-Fi.

Only read-only laptop commands run here (battery, status) — nothing changes your
volume, opens apps or powers anything off.
"""

import asyncio
import json

import httpx
import pytest
import websockets

pytestmark = pytest.mark.asyncio


class Device:
    """A connected KIAROS client that records everything the server sends."""

    def __init__(self, ws, kind: str):
        self.ws = ws
        self.kind = kind
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.backlog: list[dict] = []
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                await self.inbox.put(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    @classmethod
    async def connect(cls, url: str, kind: str) -> "Device":
        ws = await websockets.connect(url, open_timeout=5)
        dev = cls(ws, kind)
        await ws.send(json.dumps({"type": "hello", "device": kind, "platform": f"test-{kind}"}))
        return dev

    async def next(self, pred, timeout: float = 8.0) -> dict:
        """Wait for the first message matching pred; non-matching ones stay queued."""
        for i, msg in enumerate(self.backlog):
            if pred(msg):
                return self.backlog.pop(i)

        async def _wait():
            while True:
                msg = await self.inbox.get()
                if pred(msg):
                    return msg
                self.backlog.append(msg)
        return await asyncio.wait_for(_wait(), timeout)

    async def drain(self, seconds: float = 1.2):
        await asyncio.sleep(seconds)
        self.backlog.clear()
        while not self.inbox.empty():
            self.inbox.get_nowait()

    def pending(self) -> list[dict]:
        """Everything received and not yet consumed."""
        while not self.inbox.empty():
            self.backlog.append(self.inbox.get_nowait())
        return list(self.backlog)

    async def say(self, text: str) -> str:
        """Send an utterance and return what KIAROS says back to THIS device."""
        await self.ws.send(json.dumps({"type": "transcript", "text": text, "isFinal": True}))
        msg = await self.next(lambda m: m.get("type") in ("text", "audio"))
        return msg["text"]

    async def close(self):
        await self.ws.close()
        self._reader.cancel()


# ---------------------------------------------------------------------------
# Pairing & security
# ---------------------------------------------------------------------------

async def test_health_is_public(live_server):
    r = httpx.get(f"{live_server.local}/api/health")
    assert r.json() == {"status": "online", "name": "KIAROS", "version": "1.0.0"}


async def test_laptop_needs_no_token(live_server):
    assert httpx.get(f"{live_server.local}/api/settings/preferences").status_code == 200


@pytest.mark.phone
async def test_phone_api_requires_correct_token(needs_lan):
    url = f"{needs_lan.lan}/api/settings/preferences"
    assert httpx.get(url).status_code == 401
    assert httpx.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert httpx.get(url, headers={"Authorization": f"Bearer {needs_lan.token}"}).status_code == 200
    assert httpx.get(url, params={"token": needs_lan.token}).status_code == 200


@pytest.mark.phone
async def test_phone_voice_connection_requires_token(needs_lan):
    for bad in (None, "wrong"):
        with pytest.raises((websockets.InvalidStatus, websockets.ConnectionClosed)):
            ws = await websockets.connect(needs_lan.ws_lan(bad), open_timeout=5)
            await ws.recv()


@pytest.mark.phone
async def test_pairing_details_only_on_laptop(needs_lan):
    local = httpx.get(f"{needs_lan.local}/api/pairing").json()
    assert local["token"] == needs_lan.token
    assert local["app_url"].endswith(f":{needs_lan.port}")
    remote = httpx.get(f"{needs_lan.lan}/api/pairing", headers={"Authorization": f"Bearer {needs_lan.token}"})
    assert remote.status_code == 403


async def test_reports_cannot_escape_folder(live_server):
    for path in ("/reports/..%2Fserver.py", "/reports/../server.py", "/reports/nope.html"):
        r = httpx.get(live_server.local + path)
        assert r.status_code == 404
        assert "import" not in r.text


async def test_app_shell_is_served(live_server):
    assert "<title>KIAROS</title>" in httpx.get(live_server.local + "/").text
    assert httpx.get(live_server.local + "/manifest.webmanifest").json()["name"] == "KIAROS"
    sw = httpx.get(live_server.local + "/sw.js")
    assert sw.status_code == 200 and sw.headers["cache-control"] == "no-cache"


# ---------------------------------------------------------------------------
# Talking to KIAROS on the laptop
# ---------------------------------------------------------------------------

async def test_greeting_and_missing_key_guidance(live_server):
    laptop = await Device.connect(live_server.ws_local(), "laptop")
    try:
        msg = await laptop.next(lambda m: m.get("type") == "text")
        assert msg["text"].startswith("Good ")
        assert "Google Gemini or Anthropic key" in msg["text"]
        assert msg["speak"] is True  # no Fish key -> device speaks it
        reply = await laptop.say("tell me a joke")
        assert "AI key isn't set" in reply
    finally:
        await laptop.close()


async def test_battery_and_status(live_server):
    laptop = await Device.connect(live_server.ws_local(), "laptop")
    await laptop.drain()
    try:
        battery = await laptop.say("what's my battery")
        assert "battery" in battery.lower()
        status = await laptop.say("how's my laptop")
        assert "memory" in status.lower() and "percent" in status.lower()
    finally:
        await laptop.close()


async def test_power_actions_need_spoken_confirmation(live_server):
    laptop = await Device.connect(live_server.ws_local(), "laptop")
    await laptop.drain()
    try:
        assert "Say yes to confirm" in await laptop.say("shut down my laptop")
        assert await laptop.say("no") == "Cancelled."
        # A stray "yes" afterwards must NOT power anything off.
        assert "Shutting down" not in await laptop.say("yes")
        assert "Say yes to confirm" in await laptop.say("restart the laptop")
        assert await laptop.say("cancel") == "Cancelled."
    finally:
        await laptop.close()


# ---------------------------------------------------------------------------
# Phone + laptop working together
# ---------------------------------------------------------------------------

@pytest.mark.phone
async def test_call_from_laptop_rings_on_phone(needs_lan):
    phone = await Device.connect(needs_lan.ws_lan(), "phone")
    laptop = await Device.connect(needs_lan.ws_local(), "laptop")
    await asyncio.gather(phone.drain(), laptop.drain())
    try:
        assert await laptop.say("save Mom's number 98765 43210") == "Saved Mom."
        assert await laptop.say("call mom") == "Calling mom on your phone."
        action = await phone.next(lambda m: m.get("type") == "device_action")
        assert action["action"] == "call" and action["number"] == "9876543210"

        assert await laptop.say("text Mom I'm running late") == "Texting Mom on your phone."
        sms = await phone.next(lambda m: m.get("type") == "device_action")
        assert sms["action"] == "sms" and sms["message"] == "I'm running late"
    finally:
        await phone.close()
        await laptop.close()


@pytest.mark.phone
async def test_phone_can_call_directly(needs_lan):
    phone = await Device.connect(needs_lan.ws_lan(), "phone")
    await phone.drain()
    try:
        assert await phone.say("call 9876501234") == "Calling 9876501234."
        action = await phone.next(lambda m: m.get("type") == "device_action")
        assert action == {"type": "device_action", "action": "call", "target": "9876501234",
                          "message": "", "number": "9876501234"}
    finally:
        await phone.close()


async def test_call_without_phone_connected_explains(live_server):
    laptop = await Device.connect(live_server.ws_local(), "laptop")
    await laptop.drain()
    try:
        assert await laptop.say("call 9876501234") == \
            "Your phone isn't connected to me right now. Open the KIAROS app on it first."
    finally:
        await laptop.close()


@pytest.mark.phone
async def test_conversation_is_mirrored_between_devices(needs_lan):
    laptop = await Device.connect(needs_lan.ws_local(), "laptop")
    phone = await Device.connect(needs_lan.ws_lan(), "phone")
    try:
        presence = await laptop.next(lambda m: m.get("type") == "presence"
                                     and {d["device"] for d in m["devices"]} == {"laptop", "phone"})
        assert len(presence["devices"]) == 2
        await asyncio.gather(phone.drain(), laptop.drain())

        reply = await phone.say("what's my battery")
        said = await laptop.next(lambda m: m.get("type") == "sync" and m["role"] == "user")
        answered = await laptop.next(lambda m: m.get("type") == "sync" and m["role"] == "assistant")
        assert said == {"type": "sync", "role": "user", "text": "what's my battery", "device": "phone"}
        assert answered["text"] == reply and answered["device"] == "phone"
        # The laptop only shows it; it must not be told to speak it.
        await asyncio.sleep(0.5)
        assert not [m for m in laptop.pending() if m.get("type") in ("text", "audio")]

        # And the other way round.
        await laptop.say("how's my laptop")
        mirrored = await phone.next(lambda m: m.get("type") == "sync" and m["role"] == "user")
        assert mirrored["device"] == "laptop"
    finally:
        await laptop.close()
        await phone.close()

    # Phone leaves -> laptop's presence updates.
    laptop = await Device.connect(needs_lan.ws_local(), "laptop")
    try:
        p = await laptop.next(lambda m: m.get("type") == "presence")
        assert [d["device"] for d in p["devices"]] == ["laptop"]
    finally:
        await laptop.close()


@pytest.mark.phone
async def test_conversation_continues_after_reconnect(needs_lan):
    phone = await Device.connect(needs_lan.ws_lan(), "phone")
    await phone.drain()
    await phone.say("how's my laptop")
    await phone.close()

    laptop = await Device.connect(needs_lan.ws_local(), "laptop")
    try:
        history = await laptop.next(lambda m: m.get("type") == "history")
        texts = [(m["role"], m["device"], m["text"]) for m in history["messages"]]
        assert ("user", "phone", "how's my laptop") in texts
    finally:
        await laptop.close()


@pytest.mark.phone
async def test_power_confirmation_works_across_devices(needs_lan):
    """Ask on the laptop, say no on the phone: one shared conversation."""
    laptop = await Device.connect(needs_lan.ws_local(), "laptop")
    phone = await Device.connect(needs_lan.ws_lan(), "phone")
    await asyncio.gather(phone.drain(), laptop.drain())
    try:
        assert "Say yes to confirm" in await laptop.say("suspend my laptop")
        assert await phone.say("no") == "Cancelled."
    finally:
        await laptop.close()
        await phone.close()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

async def test_preferences_round_trip(live_server):
    base = live_server.local
    r = httpx.post(f"{base}/api/settings/preferences",
                   json={"user_name": "Debjyoti", "honorific": "boss", "weather_location": "Kolkata"})
    assert r.json() == {"success": True}
    assert httpx.get(f"{base}/api/settings/preferences").json() == \
        {"user_name": "Debjyoti", "honorific": "boss", "weather_location": "Kolkata"}

    laptop = await Device.connect(live_server.ws_local(), "laptop")
    await laptop.drain()
    try:
        assert "Say yes to confirm" in await laptop.say("restart the laptop")
        await laptop.say("no")
    finally:
        await laptop.close()
    httpx.post(f"{base}/api/settings/preferences", json={"user_name": "", "honorific": "sir", "weather_location": ""})


async def test_settings_reject_bad_input(live_server):
    base = live_server.local
    bad_key = httpx.post(f"{base}/api/settings/keys", json={"key_name": "PATH", "key_value": "/tmp"})
    assert bad_key.status_code == 400
    injection = httpx.post(f"{base}/api/settings/keys",
                           json={"key_name": "USER_NAME", "key_value": "x\nKIAROS_TOKEN=pwned"})
    assert injection.status_code == 400
    env = (live_server.data_dir / "env").read_text()
    assert "pwned" not in env


async def test_status_reports_integrations(live_server):
    s = httpx.get(f"{live_server.local}/api/settings/status").json()
    assert s["env_keys_set"]["anthropic"] is False and s["env_keys_set"]["gemini"] is False
    assert s["env_keys_set"]["llm"] is False and s["ai_provider"] == "not configured"
    assert s["calendar_configured"] is False and s["mail_configured"] is False
    assert s["voice"] == "device"
    assert s["notes_dir"].startswith(str(live_server.data_dir))


async def test_google_key_in_anthropic_box_is_stored_as_gemini(live_server):
    base = live_server.local
    r = httpx.post(f"{base}/api/settings/keys", json={"key_name": "ANTHROPIC_API_KEY", "key_value": "AIzaSy-fake-test-key"})
    assert r.json() == {"success": True, "saved_as": "GEMINI_API_KEY"}
    env = (live_server.data_dir / "env").read_text()
    assert "GEMINI_API_KEY=AIzaSy-fake-test-key" in env and "ANTHROPIC_API_KEY=AIza" not in env
    # Testing it from the Anthropic box asks Google, which rejects this fake key with a clear reason.
    t = httpx.post(f"{base}/api/settings/test-anthropic", json={"key_value": "AIzaSy-fake-test-key"}, timeout=30).json()
    assert t["valid"] is False and "Google" in t["error"]
    httpx.post(f"{base}/api/settings/keys", json={"key_name": "GEMINI_API_KEY", "key_value": ""})


async def test_whatsapp_message_end_to_end(live_server):
    """The user's own sentence: names the person and message, so it's sent straight away (stand-in WhatsApp page)."""
    laptop = await Device.connect(live_server.ws_local(), "laptop")
    await laptop.drain()
    try:
        ack = await laptop.say("hey open WhatsApp and send message to Somesh that how was he")
        assert ack == 'Sending "How was he?" to Somesh on WhatsApp.'
        done = await laptop.next(lambda m: m.get("type") == "text" and "Sent to" in m["text"], timeout=60)
        assert done["text"] == 'Sent to Somesh on WhatsApp: "How was he?"'
    finally:
        await laptop.close()
