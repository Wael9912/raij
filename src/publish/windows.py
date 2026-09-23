"""Posting windows (Phase 12): a video's first upload happens only inside one of `publish.windows.times`
(local to `publish.windows.timezone`), one video per window, so three Shorts never go out 28 s apart again
and each lands when the Gulf audience is scrolling. A window is "taken" once any video's first post went
out after the window opened. No windows configured → publish as soon as approved.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.config import Config

DONE = ("published", "exported")


@dataclass
class Window:
    label: str                 # "13:00" as configured
    start: datetime            # UTC, aware
    end: datetime              # UTC, aware


def configured(cfg: Config) -> list[str]:
    return [str(t) for t in (cfg.get("publish.windows.times") or [])]


def enabled(cfg: Config) -> bool:
    return bool(configured(cfg))


def current(cfg: Config, now: datetime | None = None) -> Window | None:
    """The window open at `now` (UTC), if any: from its start for `open_minutes`."""
    times = configured(cfg)
    if not times:
        return None
    tz = ZoneInfo(str(cfg.get("publish.windows.timezone") or cfg.get("schedule.timezone") or "UTC"))
    span = timedelta(minutes=float(cfg.get("publish.windows.open_minutes", 120)))
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(tz)
    for day_offset in (0, -1):                      # a late-night window may have opened "yesterday"
        day = (local + timedelta(days=day_offset)).date()
        for t in times:
            try:
                hh, mm = (int(x) for x in t.split(":"))
            except ValueError:
                continue
            start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)
            if start <= now < start + span:
                return Window(t, start, start + span)
    return None


def next_start(cfg: Config, now: datetime | None = None) -> datetime | None:
    """When the next window opens (UTC), for log lines."""
    times = configured(cfg)
    if not times:
        return None
    tz = ZoneInfo(str(cfg.get("publish.windows.timezone") or cfg.get("schedule.timezone") or "UTC"))
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(tz)
    starts = []
    for day_offset in (0, 1):
        day = (local + timedelta(days=day_offset)).date()
        for t in times:
            try:
                hh, mm = (int(x) for x in t.split(":"))
            except ValueError:
                continue
            starts.append(datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).astimezone(timezone.utc))
    later = [s for s in starts if s > now]
    return min(later) if later else None


def taken(conn: sqlite3.Connection, window: Window) -> bool:
    """True once some video's *first* successful post happened inside this window."""
    since = window.start.strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT count(*) FROM (SELECT video_id, min(published_at) AS first FROM posts "
        "WHERE status IN ('published', 'exported') AND published_at IS NOT NULL GROUP BY video_id) "
        "WHERE first >= ?", (since,)).fetchone()
    return bool(row[0])


def started(conn: sqlite3.Connection, video_id: int) -> bool:
    """A video with any platform already out finishes the rest whenever they're due (not window-gated)."""
    row = conn.execute("SELECT count(*) FROM posts WHERE video_id = ? AND status IN ('published', 'exported')",
                       (video_id,)).fetchone()
    return bool(row[0])
