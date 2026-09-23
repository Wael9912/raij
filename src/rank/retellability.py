"""Batched LLM screen: does this item's value survive being retold without its visuals?

One call classifies a whole batch into {retellable, category, reason}. Items the model skips
or answers malformed stay unchecked and are retried on the next run.
"""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from src import llm
from src.config import Config

log = logging.getLogger("raij.rank")

_TAGS = re.compile(r"<[^>]+>")


FORMATS = ("story", "list", "howto", "explainer", "fact")


@dataclass
class Verdict:
    id: int
    retellable: bool
    category: str
    reason: str
    topic: str | None = None
    audience_fit: int = 3           # 1–5 (Phase 12); a missing/malformed value is neutral
    evergreen: bool = False
    ad_safe: bool = True            # a missing value never rejects an item
    format: str | None = None


def _fit(value: Any) -> int:
    try:
        return min(5, max(1, int(value)))
    except (TypeError, ValueError):
        return 3


def _clean(text: str | None, limit: int) -> str:
    text = html.unescape(_TAGS.sub(" ", text or ""))
    return " ".join(text.split())[:limit]


def describe(row: dict[str, Any]) -> dict[str, Any]:
    """Compact, source-aware view of a candidate for the prompt."""
    raw = json.loads(row.get("raw_json") or "{}")
    item: dict[str, Any] = {"id": row["id"], "source": row["source"], "title": row.get("title")}
    if row["source"] == "youtube":
        item["duration_s"] = row.get("duration_s")
        item["description"] = _clean(raw.get("description"), 300)
        item["tags"] = (raw.get("tags") or [])[:8]
    elif row["source"] == "reddit":
        item["subreddit"] = raw.get("subreddit")
        item["is_video"] = raw.get("is_video")
        item["text"] = _clean(raw.get("selftext"), 300)
        item["linked_url"] = raw.get("linked_url")
    elif row["source"] == "trends":
        item["note"] = "Google search trend; the title is only the search term"
        item["news_headlines"] = [n.get("title") for n in (raw.get("news") or []) if n.get("title")][:3]
    elif row["source"] == "wiki":
        item["note"] = "one of yesterday's most-viewed Arabic Wikipedia articles; the title is the article name"
        item["views"] = row.get("views")
    else:
        item["feed"] = raw.get("feed")
        item["summary"] = _clean(raw.get("summary"), 300)
    return {k: v for k, v in item.items() if v not in (None, "", [])}


def parse_verdicts(payload: Any, ids: set[int], allowed: set[str]) -> list[Verdict]:
    entries = payload.get("items") if isinstance(payload, dict) else payload
    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        try:
            cid = int(e["id"])
        except (KeyError, TypeError, ValueError):
            continue
        category = str(e.get("category") or "").strip().lower()
        if cid not in ids or category not in allowed or not isinstance(e.get("retellable"), bool):
            log.debug("Dropping malformed verdict: %r", e)
            continue
        topic = re.sub(r"[^a-z0-9]+", "-", str(e.get("topic") or "").lower()).strip("-")[:60] or None
        fmt = str(e.get("format") or "").strip().lower()
        out.append(Verdict(cid, e["retellable"], category, str(e.get("reason") or "").strip()[:300], topic,
                           audience_fit=_fit(e.get("audience_fit")),
                           evergreen=e.get("evergreen") is True,
                           ad_safe=e.get("ad_safe") is not False,
                           format=fmt if fmt in FORMATS else None))
        ids.discard(cid)   # first answer per id wins
    return out


def classify(cfg: Config, rows: list[dict[str, Any]], *, batch_size: int = 15,
             client: httpx.Client | None = None) -> list[Verdict]:
    """Classify rows in batches. Raises llm.LLMError only if *no* batch could be classified."""
    categories = (list(cfg.get("ranking.categories", [])) + list(cfg.get("ranking.other_categories", []))
                  + list(cfg.get("ranking.flagged_categories", [])))
    allowed = set(categories)
    verdicts: list[Verdict] = []
    last_error: llm.LLMError | None = None
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        prompt = llm.load_prompt(
            "classify",
            categories=", ".join(categories),
            audience=str(cfg.get("ranking.audience") or "Arabic-speaking viewers"),
            items=json.dumps([describe(r) for r in batch], ensure_ascii=False, indent=1),
        )
        try:
            payload = llm.complete_json(cfg, prompt, client=client)
        except llm.LLMError as exc:
            log.error("Retellability batch %d failed: %s", i // batch_size + 1, exc)
            last_error = exc
            continue
        got = parse_verdicts(payload, {r["id"] for r in batch}, allowed)
        if len(got) < len(batch):
            log.warning("Batch %d: %d/%d items classified; the rest retry next run",
                        i // batch_size + 1, len(got), len(batch))
        verdicts.extend(got)
    if not verdicts and last_error is not None:
        raise last_error
    return verdicts
