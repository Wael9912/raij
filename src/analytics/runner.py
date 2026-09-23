"""report: pull metrics for recent posts (one row per post per UTC day), and on the report weekday send
the weekly report to Telegram once per ISO week. A platform failing never blocks the others.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

import httpx

from src import db
from src.analytics import collect, report as rep
from src.config import Config
from src.discover.common import FetchError, make_client

log = logging.getLogger("raij.analytics")


def _posts(conn: sqlite3.Connection, days: int) -> dict[str, list[dict]]:
    rows = conn.execute("SELECT * FROM posts WHERE status = 'published' AND external_id IS NOT NULL "
                        "AND published_at >= ? ORDER BY id", (collect.since(days),)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["platform"], []).append(dict(r))
    return out


def _save(conn: sqlite3.Connection, post_id: int, m: collect.Metric) -> None:
    """Today's row is replaced, so re-runs on one day don't pile up rows."""
    conn.execute("DELETE FROM metrics WHERE post_id = ? AND date(collected_at) = date('now')", (post_id,))
    conn.execute("INSERT INTO metrics (post_id, views, likes, comments, shares, avg_watch_s, retention_pct) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (post_id, m.views, m.likes, m.comments, m.shares, m.avg_watch_s, m.retention_pct))


def send_weekly(cfg: Config, conn: sqlite3.Connection, bot=None) -> str:
    text = rep.weekly_text(conn, cfg.get("analytics.report_days", 7))
    if bot is None:
        from src.review.runner import make_bot
        bot, chat = make_bot(cfg)
    else:
        chat = cfg.secret("TELEGRAM_CHAT_ID")
    bot.send_message(chat, text, disable_web_page_preview=True)
    _backup(cfg, bot, chat)
    return text


def _backup(cfg: Config, bot, chat: str) -> None:
    """Weekly encrypted DB copy to Telegram — the off-site backup when state lives in a CI cache."""
    import os
    import tempfile
    from pathlib import Path
    from src import state
    if not os.environ.get("RAIJ_STATE_KEY"):
        return
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = state.pack(cfg, Path(tmp) / f"raij-db-{datetime.now(timezone.utc):%Y%m%d}.enc", with_media=False)
            with path.open("rb") as f:
                bot.call("sendDocument", files={"document": (path.name, f, "application/octet-stream")}, chat_id=chat,
                         caption="🔐 Weekly DB backup (encrypted with RAIJ_STATE_KEY). Keep it; restore with "
                                 "`state unpack`.")
    except Exception as exc:
        log.warning("Weekly backup not sent: %s", exc)


def report(cfg: Config, conn: sqlite3.Connection, dry_run: bool = False, client: httpx.Client | None = None,
           weekly: bool | None = None, bot=None, now: datetime | None = None) -> int:
    """`weekly`: True = send the report now, False = never, None = on the report weekday, once a week."""
    now = now or datetime.now(timezone.utc)
    posts = _posts(conn, cfg.get("analytics.track_days", 30))
    week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    due = weekly if weekly is not None else (
        now.weekday() == int(cfg.get("analytics.report_weekday", 0)) and db.get_flag(conn, "weekly_report_week") != week)
    if dry_run:
        log.info("[dry run] would collect metrics for %s", ", ".join(f"{len(v)} {k}" for k, v in posts.items()) or "no posts")
        log.info("[dry run] weekly report %s", "due — would send" if due else "not due")
        return 0

    run_id = db.start_run(conn, "report")
    got, errors = {}, {}
    own = client is None
    client = client or make_client()
    try:
        for platform, fn in collect.COLLECTORS.items():
            if not posts.get(platform):
                continue
            try:
                metrics = fn(cfg, client, posts[platform])
            except (FetchError, OSError, ValueError, KeyError, RuntimeError) as exc:
                log.warning("%s metrics failed: %s", platform, exc)
                errors[platform] = str(exc)[:300]
                continue
            for pid, m in metrics.items():
                _save(conn, pid, m)
            conn.commit()
            got[platform] = len(metrics)
    finally:
        if own:
            client.close()

    sent = False
    if due:
        try:
            send_weekly(cfg, conn, bot)
            db.set_flag(conn, "weekly_report_week", week)
            sent = True
        except Exception as exc:                        # the report retries on the next run this weekday
            log.warning("Weekly report not sent: %s", exc)
            errors["weekly_report"] = str(exc)[:300]
    status = "ok" if not errors else ("partial" if got or sent else "failed")
    db.finish_run(conn, run_id, status, {"collected": got, "errors": errors, "weekly_sent": sent})
    log.info("Report %s: metrics %s%s", status, got or "none", "; weekly report sent" if sent else "")
    return 1 if status == "failed" else 0
