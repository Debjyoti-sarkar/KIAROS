"""
KIAROS auth — a shared pairing token for every client that isn't the laptop itself.

The server can open terminals, run Claude Code and control the laptop, so any
connection from another device (your phone, or anyone else on the Wi-Fi) must
present KIAROS_TOKEN. Requests from 127.0.0.1 / ::1 are trusted.
"""

import hmac
import os
import secrets
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def ensure_token() -> str:
    """Return KIAROS_TOKEN, generating and persisting one on first run."""
    token = os.getenv("KIAROS_TOKEN", "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(24)
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    lines = [l for l in lines if not l.strip().startswith("KIAROS_TOKEN=")]
    lines.append(f"KIAROS_TOKEN={token}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass
    os.environ["KIAROS_TOKEN"] = token
    return token


def is_loopback(host: str | None) -> bool:
    return (host or "") in _LOOPBACK


def token_ok(presented: str | None) -> bool:
    expected = os.getenv("KIAROS_TOKEN", "")
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())


def extract_token(headers, query_params) -> str | None:
    """Token from `Authorization: Bearer x`, `X-Kiaros-Token`, or `?token=`."""
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-kiaros-token") or query_params.get("token")


def authorized(client_host: str | None, headers, query_params) -> bool:
    if is_loopback(client_host) and not headers.get("x-forwarded-for"):
        return True
    return token_ok(extract_token(headers, query_params))
