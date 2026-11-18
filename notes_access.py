"""
KIAROS Notes — plain Markdown files in a folder you own.

Default folder: ~/Documents/KIAROS Notes (override with NOTES_DIR). Works with
any editor, Obsidian vaults, or a synced folder (Nextcloud, Syncthing, Drive).
KIAROS can create new notes and read/search existing ones; it never edits or
deletes a note you wrote.
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger("kiaros.notes")


def notes_dir() -> Path:
    d = Path(os.getenv("NOTES_DIR", str(Path.home() / "Documents" / "KIAROS Notes"))).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _note_files() -> list[Path]:
    files = [p for p in notes_dir().rglob("*") if p.suffix.lower() in (".md", ".txt") and p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _title(p: Path) -> str:
    try:
        first = p.read_text(errors="replace").lstrip().splitlines()[0]
        if first.startswith("# "):
            return first[2:].strip()
    except (OSError, IndexError):
        pass
    return p.stem


async def get_recent_notes(count: int = 10) -> list[dict]:
    def _list():
        return [{
            "title": _title(p),
            "date": datetime.fromtimestamp(p.stat().st_mtime).strftime("%b %d, %Y"),
            "folder": str(p.parent.relative_to(notes_dir())) if p.parent != notes_dir() else "",
        } for p in _note_files()[:count]]
    return await asyncio.to_thread(_list)


async def read_note(title_match: str) -> dict | None:
    q = title_match.lower().strip()

    def _find():
        files = _note_files()
        for p in files:
            if q in _title(p).lower() or q in p.stem.lower():
                return {"title": _title(p), "body": p.read_text(errors="replace")}
        for p in files:  # fall back to body search
            body = p.read_text(errors="replace")
            if q in body.lower():
                return {"title": _title(p), "body": body}
        return None
    return await asyncio.to_thread(_find)


async def search_notes_local(query: str, count: int = 5) -> list[dict]:
    q = query.lower()

    def _search():
        hits = []
        for p in _note_files():
            body = p.read_text(errors="replace")
            if q in body.lower() or q in p.stem.lower():
                hits.append({"title": _title(p), "date": datetime.fromtimestamp(p.stat().st_mtime).strftime("%b %d, %Y")})
            if len(hits) >= count:
                break
        return hits
    return await asyncio.to_thread(_search)


async def create_local_note(title: str, body: str) -> bool:
    """Create a NEW note. Never overwrites: adds a numeric suffix on collision."""
    slug = re.sub(r"[^\w\s-]", "", title).strip()[:80] or "Note"
    def _write():
        path = notes_dir() / f"{slug}.md"
        n = 2
        while path.exists():
            path = notes_dir() / f"{slug} ({n}).md"
            n += 1
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        path.write_text(f"# {title}\n\n_{stamp} · created by KIAROS_\n\n{body.strip()}\n")
        return path
    try:
        path = await asyncio.to_thread(_write)
        log.info(f"Created note: {path}")
        return True
    except OSError as e:
        log.error(f"Note creation failed: {e}")
        return False
