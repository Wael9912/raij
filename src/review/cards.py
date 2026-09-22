"""What the reviewer sees in Telegram: caption, full script, and the decision buttons."""
from __future__ import annotations

import json
import sqlite3
from typing import Any
from urllib.parse import urlsplit

from src.review.telegram import MAX_CAPTION

ACTIONS = {"ap": "approved", "rj": "rejected", "ed": "edit", "nb": "new_broll", "rv": "revoice"}
ROLE_AR = {"hook": "🎣 الافتتاحية", "body": "📖", "payoff": "💡 الخلاصة", "cta": "📣 الدعوة"}


def context(conn: sqlite3.Connection, video_id: int) -> dict[str, Any]:
    """Everything about a video the review flow needs, in one row."""
    row = conn.execute(
        "SELECT v.*, x.id AS script_id, x.brand_id, x.version, x.body_ar, x.beats, x.description_en, x.hashtags, "
        "x.similarity, x.notes AS script_notes, s.id AS story_id, s.hook, s.key_facts, s.claims, s.why_trending, "
        "s.transcript, s.sources, c.id AS candidate_id, c.title, c.canonical_url "
        "FROM videos v JOIN scripts x ON x.id = v.script_id JOIN stories s ON s.id = x.story_id "
        "JOIN candidates c ON c.id = s.candidate_id WHERE v.id = ?",
        (video_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"video {video_id} not found")
    return dict(row)


def keyboard(video_id: int) -> dict:
    b = lambda text, act: {"text": text, "callback_data": f"{act}:{video_id}"}   # noqa: E731
    return {"inline_keyboard": [
        [b("✅ Approve", "ap"), b("❌ Reject", "rj")],
        [b("✏️ Edit script", "ed"), b("🔁 New b-roll", "nb"), b("🎙 Re-voice", "rv")],
    ]}


def parse_callback(data: str) -> tuple[str, int] | None:
    act, _, vid = (data or "").partition(":")
    return (act, int(vid)) if act in ACTIONS and vid.isdigit() else None


def caption(ctx: dict[str, Any]) -> str:
    notes = json.loads(ctx.get("notes") or "{}")
    tags = " ".join(json.loads(ctx.get("hashtags") or "[]"))
    domains = sorted({urlsplit(u).hostname.removeprefix("www.") for u in json.loads(ctx.get("sources") or "[]")
                      if urlsplit(u).hostname})
    sim = ctx.get("similarity")
    meta = f"#{ctx['id']} · {ctx.get('duration_s') or 0:.0f}s · script v{ctx['version']}"
    meta += f" · similarity {sim:.2f}" if sim is not None else " · similarity n/a (non-Arabic source)"
    parts = [f"🎬 {meta}", ctx.get("hook") or ctx.get("title") or "", ctx.get("description_en") or "", tags,
             f"Trending item: {ctx['title']}" if ctx.get("title") else ""]
    if domains:
        parts.append("Sources: " + ", ".join(domains))
    for credit in notes.get("credits") or []:
        parts.append(credit)
    text = "\n\n".join(p for p in parts if p)
    return text if len(text) <= MAX_CAPTION else text[:MAX_CAPTION - 1] + "…"


def script_text(ctx: dict[str, Any]) -> str:
    lines = [f"📝 السكربت — فيديو #{ctx['id']}"]
    for b in json.loads(ctx.get("beats") or "[]"):
        who = f" · 📷 {b['person']}" if b.get("person") else ""
        lines.append(f"\n{ROLE_AR.get(b['role'], b['role'])}{who}\n{b['text']}")
    return "\n".join(lines)
