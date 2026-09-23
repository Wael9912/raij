"""rank: score recent candidates, screen the best for retellability, select today's top N.

Selection is per local (schedule.timezone) day and idempotent: re-running tops today's picks up to top_n rather than
adding another N. Political items are flagged and never selected.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
        "audience_fit": row.get("audience_fit"),
        "evergreen": row.get("evergreen"),
        "format": row.get("format"),
        "reason": row["rank_reason"],
    }
    if parts:
        out["score_parts"] = parts
    return out


def screen_pool(scored: list, by_id: dict[int, dict[str, Any]], size: int) -> list[dict[str, Any]]:
    """Unchecked candidates to send to the LLM: round-robin across sources in score order, so a source with
    no velocity signal (RSS sits at a flat ≈0.59) still gets screened next to the top Trends terms (D: the
    tech/wow-facts/life-hack feeds never reached the model when the pool was the plain top-30)."""
    queues: dict[str, list[dict[str, Any]]] = {}
    for s in scored:
        row = by_id[s.id]
        if row["retellable"] is None:
            queues.setdefault(row["source"], []).append(row)
    out: list[dict[str, Any]] = []
    while len(out) < size and any(queues.values()):
        for source in list(queues):
            if queues[source] and len(out) < size:
                out.append(queues[source].pop(0))
    return out


class Weights:
    """Selection multipliers (Phase 12): category, region, audience fit, evergreen — all from `ranking.*`."""

    def __init__(self, cfg: Config | None = None):
        get = cfg.get if cfg else (lambda key, default=None: default)
        self.category = {str(k): float(v) for k, v in (get("ranking.category_weights", {}) or {}).items()}
        self.region = {str(k).upper(): float(v) for k, v in (get("ranking.region_weights", {}) or {}).items()}
        self.region_default = float(get("ranking.region_default_weight", 1.0))
        self.fit = float(get("ranking.fit_weight", 0.0))
        self.evergreen = float(get("ranking.evergreen_bonus", 0.0))

    def region_weight(self, region: str | None) -> float:
        codes = [c.strip().upper() for c in (region or "").split(",") if c.strip()]
        if not codes or not self.region:
            return 1.0
        return max(self.region.get(c, self.region_default) for c in codes)

    def factor(self, row: dict[str, Any]) -> float:
        f = self.category.get(row.get("category") or "", 1.0) * self.region_weight(row.get("region"))
        fit = row.get("audience_fit")
        if fit is not None and self.fit:
            f *= 1 + self.fit * (int(fit) - 3)
        if row.get("evergreen") and self.evergreen:
            f *= 1 + self.evergreen
        return max(f, 0.0)


def pick(rows: list[dict[str, Any]], need: int, categories: set[str], max_per_category: int,
         already: list[dict[str, Any]] | None = None, boost: dict[str, float] | None = None,
         weights: Weights | None = None) -> list[dict[str, Any]]:
    """Highest-scoring retellable rows: at most max_per_category per category and one per topic,
    counting what was `already` selected today. `boost` multiplies scores by category (recent winners);
    `weights` applies the niche/region/fit multipliers. Items the screen marked not ad-safe never qualify."""
    boost = boost or {}
    weights = weights or Weights()
    counts: dict[str, int] = {}
    topics: set[str] = set()
    for r in already or []:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
        if r.get("topic"):
            topics.add(r["topic"])
    chosen = []
    for r in sorted(rows, key=lambda r: (r["score"] or 0) * boost.get(r["category"], 1.0) * weights.factor(r),
                    reverse=True):
        if len(chosen) >= need:
            break
        if r["retellable"] != 1 or r["status"] != "ranked" or r["category"] not in categories:
            continue
        if r.get("ad_safe") == 0:
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
    # Selection day = the schedule's local day (Cairo), the same clock `daily_due` uses — one zone, so a
    # manual rank around the UTC midnight can't select a second batch for the "next" day (A16).
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(cfg.get("schedule.timezone", "Africa/Cairo")))
    day = local.strftime("%Y-%m-%d")

    rows = _pool(conn, window)
    scored = score_rows(rows, cfg.get("ranking.weights", {}), now=now)
    by_id = {r["id"]: r for r in rows}
    to_check = screen_pool(scored, by_id, pool_size)
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
            if v.category in flagged_cats:
                new_status = "flagged"
            elif not v.retellable or not v.ad_safe:       # not advertiser-friendly → never selected (D)
                new_status = "rejected"
            else:
                new_status = "ranked"
            conn.execute(
                "UPDATE candidates SET retellable = ?, category = ?, rank_reason = ?, topic = ?, status = ?, "
                "audience_fit = ?, evergreen = ?, ad_safe = ?, format = ? WHERE id = ?",
                (int(v.retellable), v.category, v.reason, v.topic, new_status,
                 v.audience_fit, int(v.evergreen), int(v.ad_safe), v.format, v.id),
            )
            by_id[v.id].update(retellable=int(v.retellable), category=v.category, rank_reason=v.reason,
                               topic=v.topic, status=new_status, audience_fit=v.audience_fit,
                               evergreen=int(v.evergreen), ad_safe=int(v.ad_safe), format=v.format)
        notes["screened"] = len(verdicts)
        if status == "ok" and len(verdicts) < len(to_check):
            status = "partial"
        conn.commit()

    boost = winner_boost(conn, cfg.get("ranking.winner_boost", 0.15))
    if boost:
        log.info("Recent winners boost categories: %s", ", ".join(sorted(boost)))
    chosen = pick(list(by_id.values()), need, categories, max_per_cat, already, boost=boost, weights=Weights(cfg))
    stamp = local.strftime("%Y-%m-%d %H:%M:%S")            # local time, so substr(…,10) is the selection day
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
