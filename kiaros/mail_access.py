"""
KIAROS Mail Access — READ-ONLY email over IMAP.

Mailboxes are opened with EXAMINE (read-only) and bodies fetched with
BODY.PEEK, so KIAROS never marks anything as read, sends, moves or deletes.

Configure in .env:
  MAIL_IMAP_HOST     (default imap.gmail.com)
  MAIL_ADDRESS       your address
  MAIL_APP_PASSWORD  an app password (Gmail: myaccount.google.com/apppasswords)
"""

import asyncio
import email
import email.header
import email.utils
import imaplib
import logging
import os
import re
from email.message import Message

log = logging.getLogger("kiaros.mail")


def _cfg() -> tuple[str, str, str]:
    return (
        os.getenv("MAIL_IMAP_HOST", "imap.gmail.com").strip(),
        os.getenv("MAIL_ADDRESS", "").strip(),
        os.getenv("MAIL_APP_PASSWORD", "").replace(" ", "").strip(),
    )


def is_configured() -> bool:
    _, user, pw = _cfg()
    return bool(user and pw)


def _connect() -> imaplib.IMAP4_SSL:
    host, user, pw = _cfg()
    if not (user and pw):
        raise RuntimeError("Mail not configured")
    conn = imaplib.IMAP4_SSL(host, 993, timeout=20)
    conn.login(user, pw)
    conn.select("INBOX", readonly=True)
    return conn


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for text, enc in email.header.decode_header(value):
        if isinstance(text, bytes):
            parts.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(text)
    return "".join(parts).strip()


def _body_text(msg: Message, limit: int = 2000) -> str:
    def _payload(part):
        data = part.get_payload(decode=True) or b""
        return data.decode(part.get_content_charset() or "utf-8", errors="replace")

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                return _payload(part)[:limit]
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return re.sub(r"<[^>]+>", " ", _payload(part))[:limit]
        return ""
    text = _payload(msg)
    if msg.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    return text[:limit]


def _fetch(conn: imaplib.IMAP4_SSL, ids: list[bytes], with_body: bool = False) -> list[dict]:
    out = []
    what = "(FLAGS BODY.PEEK[])" if with_body else "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
    for msg_id in ids:
        typ, data = conn.fetch(msg_id, what)
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            continue
        flags = data[0][0].decode(errors="replace")
        msg = email.message_from_bytes(data[0][1])
        item = {
            "sender": _decode(msg.get("From")),
            "subject": _decode(msg.get("Subject")) or "(no subject)",
            "date": _decode(msg.get("Date")),
            "read": "\\Seen" in flags,
        }
        if with_body:
            item["content"] = _body_text(msg)
        out.append(item)
    return out


def _search(criteria: list[str], count: int, with_body: bool = False) -> list[dict]:
    conn = _connect()
    try:
        typ, data = conn.search(None, *criteria)
        if typ != "OK":
            return []
        ids = data[0].split()[-count:][::-1]  # newest first
        return _fetch(conn, ids, with_body)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _imap_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Public async API (same shape the server expects)
# ---------------------------------------------------------------------------

async def get_unread_count() -> dict:
    """Returns {"total": int, "accounts": {"Inbox": n}}."""
    def _count():
        conn = _connect()
        try:
            typ, data = conn.search(None, "UNSEEN")
            return len(data[0].split()) if typ == "OK" else 0
        finally:
            conn.logout()

    total = await asyncio.to_thread(_count)
    return {"total": total, "accounts": {}}


async def get_unread_messages(count: int = 10) -> list[dict]:
    return await asyncio.to_thread(_search, ["UNSEEN"], count)


async def get_recent_messages(count: int = 10) -> list[dict]:
    return await asyncio.to_thread(_search, ["ALL"], count)


async def search_mail(query: str, count: int = 10) -> list[dict]:
    q = _imap_quote(query)
    return await asyncio.to_thread(_search, ["OR", "FROM", q, "SUBJECT", q], count)


async def read_message(subject_match: str) -> dict | None:
    msgs = await asyncio.to_thread(_search, ["SUBJECT", _imap_quote(subject_match)], 1, True)
    return msgs[0] if msgs else None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_unread_summary(unread: dict) -> str:
    total = unread["total"]
    if total == 0:
        return "Inbox is clear. No unread messages."
    return f"You have {total} unread {'message' if total == 1 else 'messages'}."


def format_messages_for_context(messages: list[dict], label: str = "Recent emails") -> str:
    if not messages:
        return f"{label}: None."
    lines = [f"{label}:"]
    for m in messages[:10]:
        marker = "" if m.get("read") else " [UNREAD]"
        line = f"  - {m['sender']}: {m['subject']}{marker}"
        if m.get("date"):
            line += f" ({m['date'][:16]})"
        lines.append(line)
    return "\n".join(lines)


def format_messages_for_voice(messages: list[dict]) -> str:
    if not messages:
        return "No messages to report."
    if len(messages) == 1:
        m = messages[0]
        return f"One message from {_short_sender(m['sender'])}: {m['subject']}."
    summaries = [f"{_short_sender(m['sender'])} regarding {m['subject']}" for m in messages[:3]]
    result = f"You have {len(messages)} messages. " + ". ".join(summaries)
    if len(messages) > 3:
        result += f". And {len(messages) - 3} more."
    return result


def _short_sender(sender: str) -> str:
    name, addr = email.utils.parseaddr(sender)
    return name or (addr.split("@")[0] if addr else sender)
