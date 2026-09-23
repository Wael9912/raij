"""rank: score recent candidates, screen the best for retellability, select today's top N.

Selection is per UTC day and idempotent: re-running tops today's picks up to top_n rather than
adding another N. Political items are flagged and never selected.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src import db, llm
from src.analytics.report import winner_boost
from src.config import Config
from src.rank.retellability import classify
from src.rank.score import score_rows

log = logging.getLogger("raij.rank")


def _pool(conn: sqlite3.Connection, window_hours: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM candidates WHERE status IN ('new', 'ranked') "
        "AND last_seen_at >= datetime('now', ?)",
        (f"-{int(window_hours)} hours",),
    ).fetchall()
    return [dict(r) for r in rows]


def _selected_today(conn: sqlite3.Connection, day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM candidates WHERE status = 'selected' AND substr(selected_at, 1, 10) = ? "
        "ORDER BY score DESC",
        (day,),
    ).fetchall()
    return [dict(r) for r in rows]


def _entry(row: dict[str, Any], parts: dict[str, float] | None = None) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "title": row["title"],
        "url": row["canonical_url"],
        "source": row["source"],
        "region": row["region"],
        "category": row["category"],
        "topic": row.get("topic"),
        "score": row["score"],
        "reason": row["rank_reason"],
    }
    if parts:
        out["score_parts"] = parts
    return out


def pick(rows: list[dict[str, Any]], need: int, categories: set[str], max_per_category: int,
         already: list[dict[str, Any]] | None = None, boost: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Highest-scoring retellable rows: at most max_per_category per category and one per topic,
    counting what was `already` selected today. `boost` multiplies scores by category (recent winners)."""
    boost = boost or {}
    counts: dict[str, int] = {}
    topics: set[str] = set()
    for r in already or []:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
        if r.get("topic"):
            topics.add(r["topic"])
    chosen = []
    for r in sorted(rows, key=lambda r: (r["score"] or 0) * boost.get(r["category"], 1.0), reverse=True):
        if len(chosen) >= need:
            break
        if r["retellable"] != 1 or r["status"] != "ranked" or r["category"] not in categories:
            continue
        if counts.get(r["category"], 0) >= max_per_category or (r.get("topic") and r["topic"] in topics):
            continue
        counts[r["category"]] = counts.get(r["category"], 0) + 1
        if r.get("topic"):
            topics.add(r["topic"])
        chosen.append(r)
    return chosen


def rank(cfg: Config, conn: sqlite3.Connection, dry_run: bool = False,
         client: httpx.Client | None = None, out_dir: Path | None = None) -> int:
    top_n = cfg.get("ranking.top_n", 5)
    window = cfg.get("ranking.window_hours", 48)
    pool_size = cfg.get("ranking.classify_pool", 30)
    max_per_cat = cfg.get("ranking.max_per_category", 2)
    categories = set(cfg.get("ranking.categories", []))
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")

    rows = _pool(conn, window)
    scored = score_rows(rows, cfg.get("ranking.weights", {}), now=now)
    by_id = {r["id"]: r for r in rows}
    to_check = [by_id[s.id] for s in scored if by_id[s.id]["retellable"] is None][:pool_size]
    already = _selected_today(conn, day)
    need = max(top_n - len(already), 0)

    if dry_run:
        log.info("[dry run] %d candidates in the last %dh; %d already selected today, need %d",
                 len(rows), window, len(already), need)
        log.info("[dry run] would screen %d unchecked items with LLM (%s), batch size %d",
                 len(to_check), ", ".join(llm.available_providers(cfg)) or "none configured",
                 cfg.get("ranking.batch_size", 15))
        for s in scored[:10]:
            log.info("[dry run] %.3f %-7s %s", s.score, by_id[s.id]["source"], (by_id[s.id]["title"] or "")[:70])
        log.info("[dry run] no LLM calls made, nothing written")
        return 0

    run_id = db.start_run(conn, "rank")
    for s in scored:
        conn.execute("UPDATE candidates SET score = ?, status = 'ranked' WHERE id = ?", (s.score, s.id))
        by_id[s.id].update(score=s.score, status="ranked")
    conn.commit()

    notes: dict[str, Any] = {"pool": len(rows), "screened": 0}
    status = "ok"
    if need and to_check:
        try:
            verdicts = classify(cfg, to_check, batch_size=cfg.get("ranking.batch_size", 15), client=client)
        except llm.LLMError as exc:
            log.error("Retellability screen unavailable: %s", exc)
            notes["llm_error"] = str(exc)
            verdicts = []
            status = "failed"
        flagged_cats = set(cfg.get("ranking.flagged_categories", []))
        for v in verdicts:
            new_status = "flagged" if v.category in flagged_cats else ("ranked" if v.retellable else "rejected")
            conn.execute(
                "UPDATE candidates SET retellable = ?, category = ?, rank_reason = ?, topic = ?, status = ? "
                "WHERE id = ?",
                (int(v.retellable), v.category, v.reason, v.topic, new_status, v.id),
            )
            by_id[v.id].update(retellable=int(v.retellable), category=v.category,
                               rank_reason=v.reason, topic=v.topic, status=new_status)
        notes["screened"] = len(verdicts)
        if status == "ok" and len(verdicts) < len(to_check):
            status = "partial"
        conn.commit()

    boost = winner_boost(conn, cfg.get("ranking.winner_boost", 0.15))
    if boost:
        log.info("Recent winners boost categories: %s", ", ".join(sorted(boost)))
    chosen = pick(list(by_id.values()), need, categories, max_per_cat, already, boost=boost)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    for r in chosen:
        conn.execute("UPDATE candidates SET status = 'selected', selected_at = ? WHERE id = ?", (stamp, r["id"]))
        r.update(status="selected", selected_at=stamp)

    selected = already + chosen
    parts = {s.id: s.parts for s in scored}
    flagged = [r for r in by_id.values() if r["status"] == "flagged"]
    report = {
        "date": day,
        "selected": [_entry(r, parts.get(r["id"])) for r in selected],
        "flagged": [_entry(r) for r in sorted(flagged, key=lambda r: r["score"] or 0, reverse=True)],
    }
    if status == "ok" and len(selected) < top_n:
        status = "partial"
    notes.update(selected=len(selected), flagged=len(flagged))
    db.finish_run(conn, run_id, status, notes)

    out_dir = out_dir or cfg.root / "data" / "rank"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{day}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Rank report written to %s", out_dir / f"{day}.json")   # not printed: the Actions log is public (S5)

    level = logging.INFO if len(selected) >= top_n else logging.WARNING
    log.log(level, "Rank %s: %d/%d selected today, %d screened this run, %d flagged",
            status, len(selected), top_n, notes["screened"], len(flagged))
    return 1 if status == "failed" else 0
