"""Run every enabled discovery source; one source failing never stops the others."""
from __future__ import annotations

import json
import logging
import sqlite3

import httpx

from src.config import Config
from src.discover import reddit, rss, trends, youtube
from src.discover.common import SourceResult, SourceSkipped, dedupe, make_client, upsert_candidates
from src.discover.quota import QuotaBudget

log = logging.getLogger("raij.discover")

SOURCES = ("youtube", "reddit", "trends", "rss")
DAILY_TARGET = 50


def _budget(cfg: Config, conn: sqlite3.Connection) -> QuotaBudget:
    return QuotaBudget(conn, "youtube", cfg.get("discovery.youtube.daily_quota_budget", 5000))


def _fetch(name: str, cfg: Config, conn: sqlite3.Connection, client: httpx.Client) -> SourceResult:
    if name == "youtube":
        return youtube.fetch(cfg, client, _budget(cfg, conn))
    return {"reddit": reddit, "trends": trends, "rss": rss}[name].fetch(cfg, client)


def _plan(name: str, cfg: Config, conn: sqlite3.Connection) -> str:
    if name == "youtube":
        if not cfg.secret("YOUTUBE_API_KEY"):
            return "skip (YOUTUBE_API_KEY not set)"
        b = _budget(cfg, conn)
        return f"~{youtube.estimate_units(cfg)} quota units (remaining today: {b.remaining()}/{b.daily_limit})"
    if name == "reddit":
        if not (cfg.secret("REDDIT_CLIENT_ID") and cfg.secret("REDDIT_CLIENT_SECRET")):
            return "skip (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set)"
        return f"top/day of {len(cfg.get('discovery.reddit.subreddits', []))} subreddits"
    if name == "trends":
        return f"trending RSS for {', '.join(cfg.get('discovery.trends.geos', []))}"
    feeds = cfg.get("discovery.rss.feeds", []) or []
    return f"{len(feeds)} feed(s)" if feeds else "skip (no feeds configured)"


def enabled_sources(cfg: Config, only: list[str] | None = None) -> list[str]:
    names = only or [s for s in SOURCES if cfg.get(f"discovery.{s}.enabled", True)]
    return [s for s in names if s in SOURCES]


def discover(
    cfg: Config,
    conn: sqlite3.Connection,
    only: list[str] | None = None,
    dry_run: bool = False,
    client: httpx.Client | None = None,
) -> int:
    sources = enabled_sources(cfg, only)
    if dry_run:
        for name in sources:
            log.info("[dry run] %-8s %s", name, _plan(name, cfg, conn))
        log.info("[dry run] no network calls made, nothing written")
        return 0

    run_id = conn.execute("INSERT INTO runs (command) VALUES ('discover')").lastrowid
    conn.commit()
    own_client = client is None
    client = client or make_client()
    report: dict[str, dict] = {}
    try:
        for name in sources:
            try:
                res = _fetch(name, cfg, conn, client)
            except SourceSkipped as exc:
                log.warning("Skipping %s: %s", name, exc)
                report[name] = {"skipped": str(exc)}
                continue
            except Exception as exc:
                log.exception("Source %s failed", name)
                report[name] = {"failed": f"{type(exc).__name__}: {exc}"}
                continue
            unique = dedupe(res.candidates)
            new, updated = upsert_candidates(conn, unique)
            report[name] = {"fetched": len(res.candidates), "new": new, "updated": updated}
            if res.errors:
                report[name]["errors"] = res.errors
            log.info("%s: %d fetched, %d new, %d updated", name, len(res.candidates), new, updated)
    finally:
        if own_client:
            client.close()

    ran = [r for r in report.values() if "fetched" in r]
    if not ran:
        status = "failed"
    elif len(ran) < len(report) or any("errors" in r for r in ran):
        status = "partial"
    else:
        status = "ok"
    conn.execute(
        "UPDATE runs SET finished_at = datetime('now'), status = ?, notes = ? WHERE id = ?",
        (status, json.dumps(report, ensure_ascii=False), run_id),
    )
    conn.commit()

    today = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE discovered_at >= datetime('now', '-1 day')"
    ).fetchone()[0]
    level = logging.INFO if today >= DAILY_TARGET else logging.WARNING
    log.log(level, "Discovery %s: %d new candidates in the last 24h (target %d)", status, today, DAILY_TARGET)
    return 0 if status != "failed" else 1
