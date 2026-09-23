"""script: write an original Arabic script per extracted story and brand, gated for similarity.

Each (story, brand) gets its versions written in one transaction: a draft that failed the gate is
kept as 'superseded' next to its rewrite; the last version is 'passed' or 'rejected'. An LLM
outage writes nothing, so the next run retries. Candidates move extracted → scripted when at least
one brand's script passed, else → script_rejected.
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
from src.config import Config
from src.analytics.report import winners_prompt
from src.script import titles
from src.script.write import write_script

log = logging.getLogger("raij.script")


def _pending(conn: sqlite3.Connection, brand_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.*, c.title FROM stories s JOIN candidates c ON c.id = s.candidate_id "
        "WHERE c.status = 'extracted' "
        "AND NOT EXISTS (SELECT 1 FROM scripts x WHERE x.story_id = s.id AND x.brand_id = ?) "
        "ORDER BY s.id",
        (brand_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _save(conn: sqlite3.Connection, story_id: int, brand_id: str, versions: list[dict[str, Any]]) -> int:
    with conn:
        for v in versions:
            last_id = conn.execute(
                "INSERT INTO scripts (story_id, brand_id, version, body_ar, beats, description_en, hashtags, "
                "similarity, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (story_id, brand_id, v["version"], v["body_ar"], json.dumps(v["beats"], ensure_ascii=False),
                 v["description_en"], json.dumps(v["hashtags"], ensure_ascii=False), v["similarity"], v["status"],
                 json.dumps(v["notes"], ensure_ascii=False)),
            ).lastrowid
    return last_id


def script(cfg: Config, conn: sqlite3.Connection, dry_run: bool = False, client: httpx.Client | None = None,
           out_dir: Path | None = None) -> int:
    brands = cfg.brands
    work = [(b, s) for b in brands for s in _pending(conn, b["id"])]
    if dry_run:
        log.info("[dry run] %d story×brand script(s) to write; LLM: %s", len(work),
                 ", ".join(llm.available_providers(cfg)) or "none configured")
        for b, s in work:
            log.info("[dry run] story %d → %s: %s", s["id"], b["id"], (s["hook"] or "")[:70])
        log.info("[dry run] no LLM calls, nothing written")
        return 0

    run_id = db.start_run(conn, "script")
    report, retry = [], []
    passed_stories: set[int] = set()
    done_stories: set[int] = set()
    winners = winners_prompt(conn) if work else ""       # last week's best hook titles as examples
    for brand, story in work:
        try:
            outcome = write_script(cfg, story, brand, client=client, winners=winners)
        except llm.LLMError as exc:
            log.error("Story %d (%s): no LLM answered, will retry next run: %s", story["id"], brand["id"], exc)
            retry.append({"story_id": story["id"], "brand": brand["id"], "error": str(exc)})
            continue
        except Exception as exc:                            # one bad item never kills the stage
            log.exception("Story %d (%s): unexpected script error", story["id"], brand["id"])
            retry.append({"story_id": story["id"], "brand": brand["id"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        script_id = _save(conn, story["id"], brand["id"], outcome.versions)
        final = outcome.final
        done_stories.add(story["id"])
        if final["status"] == "passed":
            passed_stories.add(story["id"])
        report.append({"script_id": script_id, "story_id": story["id"], "brand": brand["id"],
                       "title": story["title"], "status": final["status"], "versions": len(outcome.versions),
                       "similarity": final["similarity"], **final["notes"], "script": final["body_ar"],
                       "beats": final["beats"], "description_en": final["description_en"],
                       "hashtags": final["hashtags"]})
        level = logging.INFO if final["status"] == "passed" else logging.WARNING
        log.log(level, "Story %d (%s) → script %d %s: %s words, similarity %s%s", story["id"], brand["id"],
                script_id, final["status"], final["notes"].get("words", "?"), final["similarity"],
                f" — {final['notes']['reason']}" if final["notes"].get("reason") else "")

    with conn:
        for story_id in done_stories:
            status = "scripted" if story_id in passed_stories else "script_rejected"
            conn.execute("UPDATE candidates SET status = ? WHERE id = (SELECT candidate_id FROM stories WHERE id = ?)",
                         (status, story_id))

    titles.backfill(cfg, conn, client=client)            # scripts from before hook titles existed

    passed = sum(1 for r in report if r["status"] == "passed")
    if not work or passed == len(work):
        status = "ok"
    elif passed:
        status = "partial"
    else:
        status = "failed"
    notes = {"pending": len(work), "passed": passed, "rejected": len(report) - passed, "retry": retry}
    db.finish_run(conn, run_id, status, notes)

    if report:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_dir = out_dir or cfg.root / "data" / "script"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{day}.json"
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        path.write_text(json.dumps(existing + report, ensure_ascii=False, indent=2), encoding="utf-8")

    level = logging.INFO if status == "ok" else logging.WARNING
    log.log(level, "Script %s: %d/%d passed, %d rejected, %d to retry", status, passed, len(work),
            notes["rejected"], len(retry))
    return 1 if status == "failed" else 0
