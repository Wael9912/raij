"""What the reviewer sees in Telegram: caption, full script, the decision buttons, the queue and the digest.

Text rules (RTL-safe, the owner reads on a phone): Arabic titles on their own line; numbers first or one
fact per line; no English jargon in the caption — the technical bits go into the script message.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from src.review.telegram import MAX_CAPTION

ACTIONS = {"ap": "approved", "rj": "rejected", "ed": "edit", "nb": "new_broll", "rv": "revoice"}
# Actions that don't refer to one in-review card: retry a failed publish, confirm/cancel a bulk command.
# Bulk callbacks carry the highest in-review id the owner saw, so cards that arrive later are untouched.
EXTRA = {"rt": "retry", "ba": "approve_all", "bs": "skip_all", "bx": "cancel"}
ROLE_AR = {"hook": "🎣 الافتتاحية", "body": "📖", "payoff": "💡 الخلاصة", "cta": "📣 الدعوة"}
PLATFORM = {"youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "tiktok_export": "TikTok"}
COMMANDS = [
    ("queue", "What's waiting for review or publishing"),
    ("status", "Publishing state and counts"),
    ("approve_all", "Approve every card in review (asks first)"),
    ("skip", "Reject every card in review (asks first)"),
    ("pause", "Stop all publishing"),
    ("resume", "Publishing on again"),
    ("report", "Send the weekly report now"),
    ("help", "What the buttons and commands do"),
]
HELP = """🤖 Ra'ij review bot

Each card: ✅ Approve · ❌ Reject · ✏️ Edit script (reply to the prompt with what to change) · 🔁 New b-roll · 🎙 Re-voice.
Taps are handled on the next pass (a few minutes), so the toast may say "too old" — the decision still counts.

/queue — cards in review and approved videos not yet out, per platform
/status — paused or not, counts by state
/approve_all, /skip — act on every card in review, after a confirm button
/pause, /resume — the publishing kill switch
/report — the weekly numbers now
/help — this"""


def context(conn: sqlite3.Connection, video_id: int) -> dict[str, Any]:
    """Everything about a video the review flow needs, in one row."""
    row = conn.execute(
        "SELECT v.*, x.id AS script_id, x.brand_id, x.version, x.body_ar, x.beats, x.description_en, x.hashtags, "
        "x.similarity, x.edit_note, x.notes AS script_notes, s.id AS story_id, s.hook, s.key_facts, s.claims, "
        "s.why_trending, s.transcript, s.sources, c.id AS candidate_id, c.title, c.canonical_url, c.category, "
        "c.rank_reason, c.source "
        "FROM videos v JOIN scripts x ON x.id = v.script_id JOIN stories s ON s.id = x.story_id "
        "JOIN candidates c ON c.id = s.candidate_id WHERE v.id = ?",
        (video_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"video {video_id} not found")
    ctx = dict(row)
    if ctx.get("parent_id"):
        parent = conn.execute("SELECT decision, note FROM approvals WHERE video_id = ? "
                              "AND decision IN ('edit', 'new_broll', 'revoice') ORDER BY id DESC LIMIT 1",
                              (ctx["parent_id"],)).fetchone()
        ctx["parent_decision"] = parent["decision"] if parent else None
        ctx["parent_note"] = parent["note"] if parent else None
    return ctx


def keyboard(video_id: int) -> dict:
    b = lambda text, act: {"text": text, "callback_data": f"{act}:{video_id}"}   # noqa: E731
    return {"inline_keyboard": [
        [b("✅ Approve", "ap"), b("❌ Reject", "rj")],
        [b("✏️ Edit script", "ed"), b("🔁 New b-roll", "nb"), b("🎙 Re-voice", "rv")],
    ]}


def retry_keyboard(video_id: int) -> dict:
    return {"inline_keyboard": [[{"text": "🔁 Retry", "callback_data": f"rt:{video_id}"}]]}


def confirm_keyboard(act: str, upto: int) -> dict:
    label = "✅ Yes, approve all" if act == "ba" else "❌ Yes, reject all"
    return {"inline_keyboard": [[{"text": label, "callback_data": f"{act}:{upto}"},
                                 {"text": "↩️ Cancel", "callback_data": f"bx:{upto}"}]]}


def parse_callback(data: str) -> tuple[str, int] | None:
    act, _, vid = (data or "").partition(":")
    return (act, int(vid)) if (act in ACTIONS or act in EXTRA) and vid.isascii() and vid.isdigit() else None


# -- labels ----------------------------------------------------------------------

def title_of(ctx: dict[str, Any]) -> str:
    """The Arabic hook title burned into the video (also the YouTube title); the source headline as fallback."""
    for blob in (ctx.get("script_notes"), ctx.get("notes")):
        t = (json.loads(blob or "{}") or {}).get("hook_title")
        if t:
            return str(t)
    beats = json.loads(ctx.get("beats") or "[]")
    return (beats[0]["text"] if beats else None) or ctx.get("hook") or ctx.get("title") or f"#{ctx['id']}"


def series_of(ctx: dict[str, Any]) -> str | None:
    for blob in (ctx.get("notes"), ctx.get("script_notes")):
        s = (json.loads(blob or "{}") or {}).get("series")
        if s:
            return str(s)
    return None


def age_text(started: str | None, now: datetime | None = None) -> str:
    """'3 h' / '2 d 5 h' since a UTC 'YYYY-MM-DD HH:MM:SS' timestamp."""
    hours = age_hours(started, now)
    if hours is None:
        return "?"
    if hours < 1:
        return f"{int(hours * 60)} min"
    if hours < 48:
        return f"{int(hours)} h"
    return f"{int(hours // 24)} d {int(hours % 24)} h"


def age_hours(started: str | None, now: datetime | None = None) -> float | None:
    if not started:
        return None
    then = datetime.strptime(started[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return max(((now or datetime.now(timezone.utc)) - then).total_seconds() / 3600, 0.0)


# -- the card ----------------------------------------------------------------------

WHY_MAX = 180


def why_line(ctx: dict[str, Any]) -> str:
    """Where it trended and rank's one-line reason (why_trending as fallback); both are LLM sentences, so one
    is enough and it's cut to WHY_MAX — the caption has 1024 characters for everything."""
    reason = next((str(ctx.get(k) or "").strip() for k in ("rank_reason", "why_trending") if ctx.get(k)), "")
    if len(reason) > WHY_MAX:
        reason = reason[:WHY_MAX - 1].rsplit(" ", 1)[0] + "…"
    src = ctx.get("source")
    where = {"youtube": "YouTube", "reddit": "Reddit", "trends": "Google Trends", "rss": "news"}.get(src or "", src)
    head = f"🔎 Why: trending on {where}" if where else "🔎 Why"
    return head + (f" — {reason}" if reason else "")


def parent_line(ctx: dict[str, Any]) -> str:
    if not ctx.get("parent_id"):
        return ""
    what = {"edit": "script edited", "new_broll": "new b-roll", "revoice": "re-voiced"}.get(
        ctx.get("parent_decision") or "", "regenerated")
    line = f"↩️ Replaces #{ctx['parent_id']} ({what})"
    note = ctx.get("parent_note") or ctx.get("edit_note")
    return f"{line}\n✏️ {note}" if what == "script edited" and note else line


def caption(ctx: dict[str, Any]) -> str:
    notes = json.loads(ctx.get("notes") or "{}")
    tags = " ".join(json.loads(ctx.get("hashtags") or "[]"))
    domains = sorted({urlsplit(u).hostname.removeprefix("www.") for u in json.loads(ctx.get("sources") or "[]")
                      if urlsplit(u).hostname})
    series = series_of(ctx)
    head = f"🎬 #{ctx['id']} · {ctx.get('duration_s') or 0:.0f}s" + (f" · {series}" if series else "")
    parts = [head, parent_line(ctx), title_of(ctx), why_line(ctx), ctx.get("description_en") or "", tags,
             f"Trending item: {ctx['title']}" if ctx.get("title") else ""]
    if domains:
        parts.append("Sources: " + ", ".join(domains))
    for credit in notes.get("credits") or []:
        parts.append(credit)
    text = "\n\n".join(p for p in parts if p)
    return text if len(text) <= MAX_CAPTION else text[:MAX_CAPTION - 1] + "…"


def script_text(ctx: dict[str, Any]) -> str:
    """The full script, then the technical line (script version, similarity, voice) — out of the caption (U8)."""
    lines = [f"📝 السكربت — فيديو #{ctx['id']}"]
    for b in json.loads(ctx.get("beats") or "[]"):
        who = f" · 📷 {b['person']}" if b.get("person") else ""
        lines.append(f"\n{ROLE_AR.get(b['role'], b['role'])}{who}\n{b['text']}")
    notes = json.loads(ctx.get("notes") or "{}")
    sim = ctx.get("similarity")
    tech = [f"script v{ctx.get('version') or 1}",
            f"similarity {sim:.2f}" if sim is not None else "similarity n/a (non-Arabic source)"]
    if notes.get("voice"):
        tech.append(f"voice {notes['voice']}")
    lines.append("\n🔧 " + " · ".join(tech))
    return "\n".join(lines)


# -- queue / status / digest ---------------------------------------------------------

def _rows(conn: sqlite3.Connection, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        "SELECT v.id, v.status, v.notes, v.created_at, v.parent_id, x.notes AS script_notes, x.beats, x.brand_id, "
        "s.hook, c.title, (SELECT max(decided_at) FROM approvals a WHERE a.video_id = v.id "
        "                  AND a.decision = 'approved') AS approved_at "
        f"FROM videos v JOIN scripts x ON x.id = v.script_id JOIN stories s ON s.id = x.story_id "
        f"JOIN candidates c ON c.id = s.candidate_id WHERE v.status IN ({marks}) ORDER BY v.id",
        statuses).fetchall()
    return [dict(r) for r in rows]


def in_review(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(conn, ("in_review",))


def platform_state(conn: sqlite3.Connection, cfg, video: dict[str, Any], missing: dict[str, str | None]) -> str:
    """'YouTube ✅ · Instagram ⏳ · TikTok 📲' for an approved video, one mark per wanted platform."""
    brand = next((b for b in cfg.brands if b["id"] == video["brand_id"]), {})
    want = brand.get("platforms") or list(PLATFORM)
    posts = {r["platform"]: dict(r) for r in conn.execute("SELECT * FROM posts WHERE video_id = ?", (video["id"],))}
    marks = []
    for p in want:
        post = posts.get(p) or {}
        if post.get("status") == "published":
            m = "✅"
        elif post.get("status") == "exported":
            m = "📲"
        elif missing.get(p):
            m = "🔑 no keys"
        elif post.get("status") == "failed":
            m = f"⚠️ {post.get('attempts', 0)}×"
        else:
            m = "⏳"
        marks.append(f"{PLATFORM.get(p, p)} {m}")
    return " · ".join(marks)


def queue_text(conn: sqlite3.Connection, cfg, missing: dict[str, str | None] | None = None,
               now: datetime | None = None) -> str:
    """/queue: cards waiting for a decision, then approved videos not yet out everywhere."""
    review = in_review(conn)
    approved = _rows(conn, ("approved",))
    if not review and not approved:
        return "📭 Queue is empty — nothing in review, nothing waiting to publish."
    lines: list[str] = []
    if review:
        lines.append(f"📥 In review: {len(review)}")
        for v in review:
            lines.append(f"#{v['id']} · {age_text(v['created_at'], now)} in review")
            lines.append(title_of(v))
    if approved:
        if lines:
            lines.append("")
        lines.append(f"📤 Approved, publishing: {len(approved)}")
        for v in approved:
            lines.append(f"#{v['id']} · approved {age_text(v['approved_at'], now)} ago")
            lines.append(title_of(v))
            lines.append(platform_state(conn, cfg, v, missing or {}))
    return "\n".join(lines)


def status_text(conn: sqlite3.Connection, paused: bool) -> str:
    counts = dict(conn.execute("SELECT status, count(*) FROM videos GROUP BY status").fetchall())
    lines = ["📊 Ra'ij status", f"Publishing: {'⏸ paused' if paused else '▶️ on'}"]
    order = ["in_review", "approved", "published", "rejected", "expired", "failed", "superseded"]
    names = {"in_review": "in review", "approved": "approved, waiting", "published": "published",
             "rejected": "rejected", "expired": "expired", "failed": "failed", "superseded": "replaced"}
    for k in order + sorted(set(counts) - set(order)):
        if counts.get(k):
            lines.append(f"{counts[k]} {names.get(k, k)}")
    if len(lines) == 2:
        lines.append("No videos yet.")
    lines.append("/queue for the list · /help for commands")
    return "\n".join(lines)


def digest_text(conn: sqlite3.Connection, video_ids: list[int], hours: float = 24) -> str:
    """One message before the day's cards: what's coming, what rank flagged, which stages had trouble."""
    lines = [f"📬 {len(video_ids)} new video{'s' if len(video_ids) != 1 else ''} to review"]
    for vid in video_ids:
        ctx = context(conn, vid)
        series = series_of(ctx)
        lines.append(f"#{vid}" + (f" · {series}" if series else ""))
        lines.append(title_of(ctx))
    window = (f"-{float(hours):.0f} hours",)
    flagged = conn.execute("SELECT title FROM candidates WHERE status = 'flagged' "
                           "AND last_seen_at >= datetime('now', ?) ORDER BY score DESC", window).fetchall()
    if flagged:
        lines.append("")
        lines.append(f"🚩 {len(flagged)} flagged (political, not scheduled)")
        lines += [f"• {(r['title'] or '?')[:80]}" for r in flagged[:5]]
    trouble = conn.execute("SELECT command, status, notes FROM runs WHERE status IN ('failed', 'partial') "
                           "AND started_at >= datetime('now', ?) AND command != 'review' ORDER BY id", window).fetchall()
    if trouble:
        lines.append("")
        lines.append(f"⚠️ {len(trouble)} stage run{'s' if len(trouble) != 1 else ''} with problems")
        seen: set[str] = set()
        for r in trouble:
            key = f"{r['command']} {r['status']}"
            if key not in seen:
                seen.add(key)
                lines.append(f"• {key}")
    return "\n".join(lines)


def reminder_text(cards: list[dict[str, Any]], level: int, max_age: int) -> str:
    head = (f"⌛ {len(cards)} card{'s' if len(cards) != 1 else ''} in review for over {level} h"
            + (" — the trend is going stale; approve, reject, or leave it" if level >= max_age
               else " — decide soon or the trend goes stale"))
    lines = [head]
    for v in cards:
        lines.append(f"#{v['id']} · {age_text(v['created_at'])}")
        lines.append(title_of(v))
    return "\n".join(lines)
