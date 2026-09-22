"""extract: turn each selected candidate into a story card (hook, key facts, claims, why-trending).

Source text is stored in stories.transcript for Phase 4's similarity gate. No media persists:
YouTube audio (whisper fallback) lives only in a temp dir. Candidates move selected → extracted,
or → extract_failed when the source has no usable story. An LLM outage leaves them 'selected'
so the next run retries.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src import llm
from src.config import Config
from src.discover.common import make_client
from src.extract.sources import ExtractError, RunCmd, SourceText, run_cmd, source_text

log = logging.getLogger("raij.extract")

KIND = {
    "article": "full article", "news": "news articles from several outlets", "summary": "feed summary only",
    "headlines": "headlines only", "selftext": "Reddit post", "autosubs": "video subtitles",
    "whisper": "video speech transcript",
}


class CardError(ValueError):
    """The LLM answered, but not with a usable card."""


def _pending(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT c.* FROM candidates c WHERE c.status = 'selected' "
        "AND NOT EXISTS (SELECT 1 FROM stories s WHERE s.candidate_id = c.id) "
        "ORDER BY c.selected_at, c.score DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def _found_via(row: dict[str, Any]) -> str:
    raw = json.loads(row.get("raw_json") or "{}")
    bits = [row["source"]]
    if row["source"] == "trends":
        bits.append(f"Google Trends {row['region']}, {raw.get('approx_traffic') or '?'} searches")
    elif raw.get("feed"):
        bits.append(raw["feed"])
    elif raw.get("subreddit"):
        bits.append(f"r/{raw['subreddit']}")
    if row.get("views") and row["source"] != "trends":
        bits.append(f"{row['views']:,} views")
    return ", ".join(bits)


def validate_card(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise CardError("card is not a JSON object")
    if data.get("usable") is False:
        raise ExtractError(f"LLM: not a usable story — {data.get('reason') or 'no reason given'}")
    hook = str(data.get("hook") or "").strip()
    facts = [str(f).strip() for f in data.get("key_facts") or [] if str(f).strip()]
    if not hook or len(facts) < 2:
        raise CardError("card is missing a hook or key facts")
    claims = [c for c in data.get("claims") or [] if isinstance(c, dict) and c.get("claim")]
    return {"hook": hook, "key_facts": facts[:5], "claims": claims,
            "why_trending": str(data.get("why_trending") or "").strip()}


def distill(cfg: Config, row: dict[str, Any], src: SourceText, client: httpx.Client | None = None) -> dict[str, Any]:
    prompt = llm.load_prompt(
        "story_distill",
        title=row["title"] or "",
        found_via=_found_via(row),
        topic=row.get("topic") or "(none)",
        reason=row.get("rank_reason") or "(none)",
        source_kind=KIND.get(src.src, src.src),
        text=src.text[: cfg.get("extract.max_prompt_chars", 12000)],
    )
    return validate_card(llm.complete_json(cfg, prompt, client=client))


def extract(cfg: Config, conn: sqlite3.Connection, dry_run: bool = False, client: httpx.Client | None = None,
            run: RunCmd = run_cmd, out_dir: Path | None = None) -> int:
    pending = _pending(conn)
    if dry_run:
        log.info("[dry run] %d selected candidate(s) awaiting extraction; LLM: %s",
                 len(pending), ", ".join(llm.available_providers(cfg)) or "none configured")
        for r in pending:
            log.info("[dry run] #%d %-7s %s", r["id"], r["source"], (r["title"] or "")[:70])
        log.info("[dry run] nothing fetched, no LLM calls, nothing written")
        return 0

    run_id = conn.execute("INSERT INTO runs (command) VALUES ('extract')").lastrowid
    conn.commit()
    min_chars = cfg.get("extract.min_source_chars", 120)
    cards, failed, retry = [], [], []
    own_client = client is None
    client = client or make_client()
    try:
        for row in pending:
            try:
                src = source_text(cfg, client, row, run=run)
                if len(src.text) < min_chars:
                    raise ExtractError(f"only {len(src.text)} chars of source text ({src.src})")
                card = distill(cfg, row, src, client=client)
            except ExtractError as exc:
                log.warning("#%d %s: %s", row["id"], row["source"], exc)
                conn.execute("UPDATE candidates SET status = 'extract_failed' WHERE id = ?", (row["id"],))
                conn.commit()
                failed.append({"id": row["id"], "error": str(exc)})
                continue
            except (llm.LLMError, CardError) as exc:
                log.error("#%d: story card failed, will retry next run: %s", row["id"], exc)
                retry.append({"id": row["id"], "error": str(exc)})
                continue
            except Exception as exc:                        # one bad item never kills the stage
                log.exception("#%d: unexpected extract error", row["id"])
                retry.append({"id": row["id"], "error": f"{type(exc).__name__}: {exc}"})
                continue
            max_t = cfg.get("extract.max_transcript_chars", 20000)
            story_id = conn.execute(
                "INSERT INTO stories (candidate_id, transcript, transcript_src, sources, hook, key_facts, "
                "claims, why_trending) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], src.text[:max_t], src.src, json.dumps(src.urls, ensure_ascii=False), card["hook"],
                 json.dumps(card["key_facts"], ensure_ascii=False), json.dumps(card["claims"], ensure_ascii=False),
                 card["why_trending"]),
            ).lastrowid
            conn.execute("UPDATE candidates SET status = 'extracted' WHERE id = ?", (row["id"],))
            conn.commit()
            cards.append({"story_id": story_id, "candidate_id": row["id"], "title": row["title"],
                          "transcript_src": src.src, "source_chars": len(src.text), "sources": src.urls, **card})
            log.info("#%d → story %d (%s, %d chars): %s", row["id"], story_id, src.src, len(src.text), card["hook"])
    finally:
        if own_client:
            client.close()

    if not pending or len(cards) == len(pending):
        status = "ok"
    elif cards:
        status = "partial"
    else:
        status = "failed"
    notes = {"pending": len(pending), "extracted": len(cards), "failed": failed, "retry": retry}
    conn.execute("UPDATE runs SET finished_at = datetime('now'), status = ?, notes = ? WHERE id = ?",
                 (status, json.dumps(notes, ensure_ascii=False), run_id))
    conn.commit()

    if cards:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_dir = out_dir or cfg.root / "data" / "extract"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{day}.json"
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        path.write_text(json.dumps(existing + cards, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(cards, ensure_ascii=False, indent=2))

    level = logging.INFO if status == "ok" else logging.WARNING
    log.log(level, "Extract %s: %d/%d story cards, %d unusable, %d to retry",
            status, len(cards), len(pending), len(failed), len(retry))
    return 1 if status == "failed" else 0
