"""
Shared fixtures for the KIAROS test suite.

Every test runs against throwaway data: a temporary data folder, notes folder
and settings file, so your real memory, contacts and .env are never touched.
`live_server` starts the real server as a subprocess on a free port.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parent.parent
TOKEN = "test-pairing-token"

# Isolate before any test module imports server.py / memory.py.
_TMP = Path(tempfile.mkdtemp(prefix="kiaros-test-"))
(_TMP / "notes").mkdir()
(_TMP / "env").write_text("")
os.environ.update({
    "KIAROS_DATA_DIR": str(_TMP / "data"),
    "KIAROS_ENV_FILE": str(_TMP / "env"),
    "NOTES_DIR": str(_TMP / "notes"),
    "KIAROS_TOKEN": TOKEN,
    "KIAROS_BROWSER_HEADLESS": "1",
    "OLLAMA_BACKUP": "0",  # never use a real local model in tests (test_ollama uses a fake one)
})
sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def lan_ip() -> str | None:
    """A non-loopback address of this machine: connecting to it looks like a phone on Wi-Fi."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


class LiveServer:
    def __init__(self, port: int, data_dir: Path):
        self.port = port
        self.data_dir = data_dir
        self.token = TOKEN
        self.local = f"http://localhost:{port}"
        ip = lan_ip()
        self.lan = f"http://{ip}:{port}" if ip else None

    def ws_local(self) -> str:
        return f"ws://localhost:{self.port}/ws/voice"

    def ws_lan(self, token: str | None = TOKEN) -> str:
        assert self.lan, "no LAN address"
        url = self.lan.replace("http://", "ws://") + "/ws/voice"
        return f"{url}?token={token}" if token else url


@pytest.fixture(scope="session")
def live_server():
    """The real KIAROS server (no API key, isolated data) on a free port."""
    port = _free_port()
    data = Path(tempfile.mkdtemp(prefix="kiaros-live-"))
    (data / "notes").mkdir()
    env_file = data / "env"
    env_file.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "GEMINI_", "GOOGLE_API", "LLM_PROVIDER", "FISH_", "MAIL_", "CALENDAR_"))}
    env.update({
        "KIAROS_DATA_DIR": str(data / "data"),
        "KIAROS_ENV_FILE": str(env_file),
        "NOTES_DIR": str(data / "notes"),
        "KIAROS_TOKEN": TOKEN,
        "KIAROS_PORT": str(port),
        # WhatsApp automation runs against a local stand-in page, headless — never your real WhatsApp.
        "KIAROS_WHATSAPP_URL": (ROOT / "tests" / "fixtures" / "whatsapp.html").as_uri(),
        "KIAROS_BROWSER_HEADLESS": "1",
        "OLLAMA_BACKUP": "0",
        "PYTHONUNBUFFERED": "1",
    })
    log = open(data / "server.log", "w")
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--no-https"],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    server = LiveServer(port, data)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"{server.local}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            raise RuntimeError("server exited:\n" + (data / "server.log").read_text())
        time.sleep(0.3)
    else:
        proc.kill()
        raise RuntimeError("server did not start:\n" + (data / "server.log").read_text())
    yield server
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture
def needs_lan(live_server):
    if not live_server.lan:
        pytest.skip("no non-loopback network address (phone tests need Wi-Fi/Ethernet)")
    return live_server
