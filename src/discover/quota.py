"""Per-API daily quota budgeting, persisted in SQLite so separate runs share one budget."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

# YouTube Data API quota resets at midnight Pacific time.
_RESET_TZ = {"youtube": "America/Los_Angeles"}


class QuotaExceeded(RuntimeError):
    pass


class QuotaBudget:
    def __init__(self, conn: sqlite3.Connection, api: str, daily_limit: int, now: datetime | None = None):
        self.conn = conn
        self.api = api
        self.daily_limit = daily_limit
        tz = ZoneInfo(_RESET_TZ.get(api, "UTC"))
        self.day = (now.astimezone(tz) if now else datetime.now(tz)).date().isoformat()

    def used(self) -> int:
        row = self.conn.execute(
            "SELECT units FROM api_quota WHERE day = ? AND api = ?", (self.day, self.api)
        ).fetchone()
        return row["units"] if row else 0

    def remaining(self) -> int:
        return max(0, self.daily_limit - self.used())

    def spend(self, units: int) -> None:
        """Reserve units before a call. Raises instead of overspending; failed calls still count."""
        if self.used() + units > self.daily_limit:
            raise QuotaExceeded(f"{self.api}: {units} units would exceed {self.daily_limit}/day "
                                f"({self.used()} used on {self.day})")
        self.conn.execute(
            "INSERT INTO api_quota (day, api, units) VALUES (?, ?, ?) "
            "ON CONFLICT(day, api) DO UPDATE SET units = units + excluded.units",
            (self.day, self.api, units),
        )
        self.conn.commit()
