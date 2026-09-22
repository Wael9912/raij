"""Backfill on-screen hook titles for scripts written before titles existed (Phase 6.5).

One batched LLM call covers every passed script still headed for review or publishing that has no
`hook_title` in its notes. An LLM outage changes nothing; the videos just render without a title.
"""
from __future__ import annotations

import json
import logging
import sqlite3

import httpx

from src import llm
from src.config import Config
from src.script.write import clean_title

log = logging.getLogger("raij.script")


def missing(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT x.id, x.brand_id, x.body_ar, x.notes FROM scripts x WHERE x.status = 'passed' "
        "AND EXISTS (SELECT 1 FROM videos v WHERE v.script_id = x.id "
        "            AND v.status IN ('pending', 'voiced', 'rendered', 'in_review', 'approved')) ORDER BY x.id"
    ).fetchall()
    return [dict(r) for r in rows if not json.loads(r["notes"] or "{}").get("hook_title")]


def backfill(cfg: Config, conn: sqlite3.Connection, client: httpx.Client | None = None) -> int:
    """Returns how many scripts got a title."""
    todo = missing(conn)
    done = 0
    for b in cfg.brands:
        mine = [s for s in todo if s["brand_id"] == b["id"]]
        if not mine:
            continue
        prompt = llm.load_prompt(
            "hook_titles", brand_name=b.get("name") or b["id"],
            series=" / ".join(f'"{v}"' for v in (b.get("series") or {}).values()) or "(none — omit it)",
            scripts="\n\n".join(f"[id {s['id']}]\n{s['body_ar']}" for s in mine))
        try:
            reply = llm.complete_json(cfg, prompt, client=client)
        except llm.LLMError as exc:
            log.warning("Hook titles not backfilled (%s); videos render without one", exc)
            return done
        by_id = {s["id"]: s for s in mine}
        for t in (reply.get("titles") if isinstance(reply, dict) else None) or []:
            s = by_id.get(t.get("id")) if isinstance(t, dict) else None
            title = clean_title(t.get("hook_title")) if s else None
            if not title:
                continue
            notes = {**json.loads(s["notes"] or "{}"), "hook_title": title}
            if str(t.get("series") or "").strip():
                notes["series"] = str(t["series"]).strip()
            conn.execute("UPDATE scripts SET notes = ? WHERE id = ?", (json.dumps(notes, ensure_ascii=False), s["id"]))
            done += 1
        conn.commit()
    if todo:
        log.info("Hook titles backfilled for %d/%d script(s)", done, len(todo))
    return done
