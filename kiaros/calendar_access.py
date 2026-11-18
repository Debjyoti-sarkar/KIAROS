"""
KIAROS Calendar Access — read calendars from iCalendar (ICS) feeds. Read-only.

Works with any provider that publishes an ICS link, with no OAuth:
  - Google Calendar: Settings → your calendar → "Secret address in iCal format"
  - Outlook: Settings → Calendar → Shared calendars → Publish → ICS link
  - Nextcloud / Proton / Fastmail: "subscribe" / export link
  - Local .ics files

Set CALENDAR_ICS_URLS to a comma-separated list of URLs and/or file paths.
Results are cached; refresh_cache() re-downloads.
"""

import asyncio
import logging
import os
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger("kiaros.calendar")

_event_cache: list[dict] = []
_cache_time: float = 0
_CACHE_TTL = 300


def _sources() -> list[str]:
    raw = os.getenv("CALENDAR_ICS_URLS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def is_configured() -> bool:
    return bool(_sources())


async def _fetch(source: str) -> bytes | None:
    try:
        if source.startswith(("http://", "https://", "webcal://")):
            url = "https://" + source[len("webcal://"):] if source.startswith("webcal://") else source
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
                resp = await http.get(url)
                resp.raise_for_status()
                return resp.content
        path = Path(source).expanduser()
        return path.read_bytes() if path.exists() else None
    except Exception as e:
        log.warning(f"Calendar fetch failed ({source[:40]}...): {e}")
        return None


def _to_local(dt) -> tuple[datetime, bool]:
    """Return (naive local datetime, all_day)."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt, False
    if isinstance(dt, date):
        return datetime(dt.year, dt.month, dt.day), True
    raise TypeError(type(dt))


def _parse_events(data: bytes, start: datetime, end: datetime) -> list[dict]:
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(data)
    cal_name = str(cal.get("X-WR-CALNAME", "")) or "Calendar"
    events = []
    for comp in recurring_ical_events.of(cal).between(start, end):
        try:
            start_dt, all_day = _to_local(comp.get("DTSTART").dt)
        except Exception:
            continue
        title = str(comp.get("SUMMARY", "")).strip() or "(untitled)"
        location = str(comp.get("LOCATION", "")).strip()
        events.append({
            "title": title,
            "start": "All day" if all_day else start_dt.strftime("%I:%M %p").lstrip("0"),
            "start_dt": start_dt,
            "all_day": all_day,
            "calendar": cal_name,
            "location": location,
        })
    return events


async def get_events_between(start: datetime, end: datetime) -> list[dict]:
    sources = _sources()
    if not sources:
        return []
    blobs = await asyncio.gather(*(_fetch(s) for s in sources))
    events: list[dict] = []
    for blob in blobs:
        if not blob:
            continue
        try:
            events += await asyncio.to_thread(_parse_events, blob, start, end)
        except Exception as e:
            log.warning(f"Calendar parse failed: {e}")
    events.sort(key=lambda e: (not e["all_day"], e["start_dt"]))
    return events


async def refresh_cache():
    """Re-download all feeds and cache today's events."""
    global _event_cache, _cache_time
    now = datetime.now()
    start = datetime(now.year, now.month, now.day)
    t0 = _time.time()
    _event_cache = await get_events_between(start, start + timedelta(days=1))
    _cache_time = _time.time()
    log.info(f"Calendar cache refreshed: {len(_event_cache)} events today ({_time.time() - t0:.1f}s)")


async def get_todays_events() -> list[dict]:
    if _time.time() - _cache_time > _CACHE_TTL:
        await refresh_cache()
    return _event_cache


async def get_upcoming_events(hours: int = 4) -> list[dict]:
    now = datetime.now()
    events = await get_events_between(now - timedelta(minutes=1), now + timedelta(hours=hours))
    return [e for e in events if not e["all_day"] and e["start_dt"] >= now]


async def get_next_event() -> dict | None:
    events = await get_upcoming_events(hours=24)
    return events[0] if events else None


def format_events_for_context(events: list[dict]) -> str:
    if not is_configured():
        return "Calendar not connected (set CALENDAR_ICS_URLS)."
    if not events:
        return "No events scheduled today."
    lines = []
    for evt in events:
        entry = f"  All day — {evt['title']}" if evt.get("all_day") else f"  {evt['start']} — {evt['title']}"
        if evt.get("location"):
            entry += f" @ {evt['location']}"
        if evt.get("calendar"):
            entry += f" [{evt['calendar']}]"
        lines.append(entry)
    return "\n".join(lines)


def format_schedule_summary(events: list[dict]) -> str:
    if not is_configured():
        return "Your calendar isn't connected yet. Add an ICS link in settings."
    if not events:
        return "Your schedule is clear today."
    if len(events) == 1:
        evt = events[0]
        if evt.get("all_day"):
            return f"You have one all-day event: {evt['title']}."
        return f"You have one event: {evt['title']} at {evt['start']}."
    summaries = [f"{e['title']} all day" if e.get("all_day") else f"{e['title']} at {e['start']}" for e in events[:3]]
    result = f"You have {len(events)} events today. " + ". ".join(summaries)
    if len(events) > 3:
        result += f". And {len(events) - 3} more."
    return result
