"""The Telegram pick flow (Phase 13): the owner chooses what to produce, in which format, for which platforms.

Three entry points share one flow:
  /trending  → the `trending` job screens today's candidates and sends a numbered list with toggle buttons
               (step "items"), then "➡️ Next" → step "options" (format + platforms) → "🚀 Make".
  /topic …   → straight to "options" (the topic is researched online by the extract stage).
  /script …  → "options" with the format fixed by the word count (the text is voiced as written).

The flow lives in `control.pick_flow` (one at a time; a new one replaces it) and every tap edits the same
message, so the chat doesn't fill up. Callback data: pk:<n> toggle item n · pf:<i> toggle format · pp:<i>
toggle platform · pn:0 next · pb:0 back · pg:0 go · px:0 cancel (all handled by review/bot.py).
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src import db, formats
from src.config import Config
from src.review.cards import PLATFORM

FLAG = "pick_flow"
TTL = 12 * 3600                      # an untouched flow expires; its message loses the buttons on the next tap
PLATFORM_ORDER = ("youtube", "instagram", "facebook", "tiktok_export")
CATEGORY_AR = {"tech": "تقنية", "money": "مال", "wow-facts": "هل تعلم", "life-hack": "حيلة", "tools": "أداة",
               "news-lite": "أخبار", "sports": "رياضة", "culture": "ثقافة"}
SOURCE = {"trends": "Trends", "rss": "news", "wiki": "Wikipedia", "youtube": "YouTube", "reddit": "Reddit"}


def load(conn: sqlite3.Connection) -> dict[str, Any] | None:
    try:
        flow = json.loads(db.get_flag(conn, FLAG) or "null")
    except ValueError:
        return None
    if not isinstance(flow, dict) or time.time() - float(flow.get("at") or 0) > TTL:
        return None
    return flow


def save(conn: sqlite3.Connection, flow: dict[str, Any] | None) -> None:
    if flow is not None:
        flow["at"] = time.time()
    db.set_flag(conn, FLAG, json.dumps(flow, ensure_ascii=False))


def new(cfg: Config, kind: str, items: list[dict[str, Any]] | None = None, text: str | None = None,
        fmts: list[str] | None = None, words: int | None = None) -> dict[str, Any]:
    brand = (cfg.brands or [{}])[0]
    return {"kind": kind, "step": "items" if kind == "trend" else "options",
            "items": [_item(r) for r in items or []], "chosen": [],
            "formats": fmts or ["short"], "platforms": list(brand.get("platforms") or list(PLATFORM_ORDER)),
            "text": text, "words": words, "msg": None, "at": time.time()}


def _item(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": int(r["id"]), "title": (r.get("title") or "?")[:90], "category": r.get("category"),
            "fit": r.get("audience_fit"), "source": r.get("source"), "why": (r.get("rank_reason") or "")[:140],
            "evergreen": bool(r.get("evergreen"))}


# --- rendering ----------------------------------------------------------------------------

def text(flow: dict[str, Any], missing: dict[str, str | None] | None = None) -> str:
    if flow["step"] == "items":
        return _items_text(flow)
    return _options_text(flow, missing or {})


def _items_text(flow: dict[str, Any]) -> str:
    chosen = set(flow["chosen"])
    lines = [f"🔥 Trending now — tap the numbers to pick ({len(chosen)} picked), then ➡️ Next"]
    for n, it in enumerate(flow["items"], 1):
        mark = "✅" if it["id"] in chosen else "⬜"
        cat = CATEGORY_AR.get(it.get("category") or "", it.get("category") or "")
        meta = " · ".join(x for x in (cat, f"fit {it['fit']}/5" if it.get("fit") else "",
                                      SOURCE.get(it.get("source") or "", it.get("source") or ""),
                                      "evergreen" if it.get("evergreen") else "") if x)
        lines.append(f"{mark} {n}. {it['title']}")
        lines.append(f"      {meta}" if meta else "")
        if it.get("why"):
            lines.append(f"      {it['why']}")
    lines.append("")
    lines.append("Or send /topic <what to make a video about> · /script <your script text>")
    return "\n".join(l for l in lines if l is not None)


def _options_text(flow: dict[str, Any], missing: dict[str, str | None]) -> str:
    lines = []
    if flow["kind"] == "trend":
        picked = [it for it in flow["items"] if it["id"] in set(flow["chosen"])]
        lines.append(f"🎯 {len(picked)} topic{'s' if len(picked) != 1 else ''} picked:")
        lines += [f"• {it['title']}" for it in picked]
    elif flow["kind"] == "topic":
        lines.append("✍️ Topic:")
        lines.append(flow.get("text") or "")
    else:
        est = int((flow.get("words") or 0) / (1.85 if "long" in flow["formats"] else 2.05))
        lines.append(f"📝 Your script: {flow.get('words') or '?'} words ≈ {est // 60}:{est % 60:02d} spoken → "
                     f"{formats.LABEL.get(flow['formats'][0], flow['formats'][0])}")
    lines.append("")
    if flow["kind"] != "script":
        lines.append("Format: " + " + ".join(formats.LABEL[k] for k in flow["formats"]))
        lines.append("📱 Short = vertical ≤60 s (Shorts/Reels/TikTok) · 🎬 Long = landscape 2–5 min with chapters (YouTube)")
    lines.append("Post to: " + ", ".join(PLATFORM.get(p, p) for p in flow["platforms"]) if flow["platforms"]
                 else "Post to: nowhere (review only)")
    keyless = [PLATFORM.get(p, p) for p in flow["platforms"] if missing.get(p)]
    if keyless:
        lines.append(f"🔑 No keys yet for {', '.join(keyless)} — those posts wait until the keys exist.")
    if "long" in flow["formats"]:
        lines.append("Long videos go to YouTube (and the TikTok export); Reels APIs cap at 90 s.")
    lines.append("")
    lines.append("Tap to change, then 🚀 Make.")
    return "\n".join(lines)


def keyboard(flow: dict[str, Any]) -> dict:
    if flow["step"] == "items":
        chosen = set(flow["chosen"])
        rows, row = [], []
        for n, it in enumerate(flow["items"], 1):
            row.append({"text": f"{n} {'✅' if it['id'] in chosen else '⬜'}", "callback_data": f"pk:{n}"})
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": f"➡️ Next ({len(chosen)})", "callback_data": "pn:0"},
                     {"text": "✖ Cancel", "callback_data": "px:0"}])
        return {"inline_keyboard": rows}
    rows = []
    if flow["kind"] != "script":
        rows.append([{"text": f"{formats.LABEL[k]} {'✅' if k in flow['formats'] else '⬜'}", "callback_data": f"pf:{i}"}
                     for i, k in enumerate(formats.KINDS)])
    plats = [{"text": f"{PLATFORM.get(p, p)} {'✅' if p in flow['platforms'] else '⬜'}", "callback_data": f"pp:{i}"}
             for i, p in enumerate(PLATFORM_ORDER)]
    rows += [plats[:2], plats[2:]]
    count = len(flow["chosen"]) if flow["kind"] == "trend" else 1
    last = [{"text": f"🚀 Make {count * len(flow['formats'])} video{'s' if count * len(flow['formats']) != 1 else ''}",
             "callback_data": "pg:0"}]
    if flow["kind"] == "trend":
        last.append({"text": "⬅️ Back", "callback_data": "pb:0"})
    last.append({"text": "✖ Cancel", "callback_data": "px:0"})
    rows.append(last)
    return {"inline_keyboard": rows}


# --- transitions --------------------------------------------------------------------------

def toggle(flow: dict[str, Any], act: str, n: int) -> str | None:
    """Apply a tap; returns a short toast text, or None when the tap changed nothing."""
    if act == "pk":
        if not 1 <= n <= len(flow["items"]):
            return None
        cid = flow["items"][n - 1]["id"]
        if cid in flow["chosen"]:
            flow["chosen"].remove(cid)
            return "Removed"
        flow["chosen"].append(cid)
        return "Picked"
    if act == "pf":
        if not 0 <= n < len(formats.KINDS) or flow["kind"] == "script":
            return None
        k = formats.KINDS[n]
        if k in flow["formats"]:
            if len(flow["formats"]) == 1:
                return "Keep at least one format"
            flow["formats"].remove(k)
        else:
            flow["formats"] = [x for x in formats.KINDS if x in flow["formats"] + [k]]
        return "Format updated"
    if act == "pp":
        if not 0 <= n < len(PLATFORM_ORDER):
            return None
        p = PLATFORM_ORDER[n]
        if p in flow["platforms"]:
            flow["platforms"].remove(p)
        else:
            flow["platforms"] = [x for x in PLATFORM_ORDER if x in flow["platforms"] + [p]]
        return "Platforms updated"
    if act == "pn":
        if flow["kind"] == "trend" and not flow["chosen"]:
            return "Pick at least one topic first"
        flow["step"] = "options"
        return None
    if act == "pb":
        flow["step"] = "items" if flow["kind"] == "trend" else "options"
        return None
    return None


def commit(cfg: Config, conn: sqlite3.Connection, flow: dict[str, Any]) -> dict[str, Any]:
    """Write the ask into the DB: trending picks become `selected` with `wanted`; a topic/script becomes a manual
    candidate. Returns a summary {ids, formats, platforms, kind}."""
    from src.discover import manual
    fmts, plats = list(flow["formats"]), list(flow["platforms"])
    ids: list[int] = []
    if flow["kind"] == "trend":
        stamp = datetime.now(ZoneInfo(cfg.get("schedule.timezone", "Africa/Cairo"))).strftime("%Y-%m-%d %H:%M:%S")
        wanted = formats.encode(fmts, plats, kind="trend")
        with conn:
            for cid in flow["chosen"]:
                row = conn.execute("SELECT status FROM candidates WHERE id = ?", (cid,)).fetchone()
                if row is None:
                    continue
                if row["status"] in ("ranked", "new", "expired", "extract_failed", "script_rejected"):
                    conn.execute("UPDATE candidates SET status = 'selected', selected_at = ?, wanted = ?, attempts = 0 "
                                 "WHERE id = ?", (stamp, wanted, cid))
                else:                                        # already picked by the daily run: just widen the ask
                    conn.execute("UPDATE candidates SET wanted = ? WHERE id = ?", (wanted, cid))
                ids.append(int(cid))
    elif flow["kind"] == "topic":
        ids.append(manual.add_topic(cfg, conn, flow["text"] or "", fmts, plats))
    else:
        cid, kind = manual.add_script(cfg, conn, flow["text"] or "", plats, kind=fmts[0] if fmts else None)
        fmts = [kind]
        ids.append(cid)
    return {"ids": ids, "formats": fmts, "platforms": plats, "kind": flow["kind"]}


def done_text(summary: dict[str, Any]) -> str:
    n = len(summary["ids"]) * len(summary["formats"])
    where = ", ".join(PLATFORM.get(p, p) for p in summary["platforms"]) or "nowhere (review only)"
    what = " + ".join(formats.LABEL[k] for k in summary["formats"])
    return (f"🚀 Making {n} video{'s' if n != 1 else ''} ({what}) → {where}.\n"
            f"Cards arrive here for approval when rendered — a few minutes per Short, longer for a Long. "
            f"/jobs shows progress.")
