"""What the numbers say: per-video totals, recent winners (fed back into rank + script), weekly report."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from src.analytics.collect import since


def video_totals(conn: sqlite3.Connection, days: int = 7) -> list[dict[str, Any]]:
    """Videos first published in the last `days`, with the latest metrics summed over platforms."""
    rows = conn.execute(
        "SELECT p.video_id, p.platform, m.views, m.likes, m.comments, m.shares, m.retention_pct, "
        "       x.notes AS script_notes, c.category, first.at AS first_at "
        "FROM posts p "
        "JOIN (SELECT video_id, min(published_at) AS at FROM posts WHERE status = 'published' GROUP BY video_id) first "
        "  ON first.video_id = p.video_id "
        "JOIN videos v ON v.id = p.video_id JOIN scripts x ON x.id = v.script_id "
        "JOIN stories s ON s.id = x.story_id JOIN candidates c ON c.id = s.candidate_id "
        "LEFT JOIN metrics m ON m.id = (SELECT max(id) FROM metrics WHERE post_id = p.id) "
        "WHERE p.status = 'published' AND first.at >= ? ORDER BY p.video_id",
        (since(days),),
    ).fetchall()
    videos: dict[int, dict[str, Any]] = {}
    for r in rows:
        notes = json.loads(r["script_notes"] or "{}")
        v = videos.setdefault(r["video_id"], {
            "video_id": r["video_id"], "title": notes.get("hook_title") or f"#{r['video_id']}",
            "series": notes.get("series"), "category": r["category"], "published_at": r["first_at"],
            "views": 0, "likes": 0, "comments": 0, "shares": 0, "platforms": {}, "retention_pct": None})
        for k in ("views", "likes", "comments", "shares"):
            v[k] += r[k] or 0
        v["platforms"][r["platform"]] = r["views"] or 0
        if r["platform"] == "youtube" and r["retention_pct"] is not None:
            v["retention_pct"] = r["retention_pct"]
    return sorted(videos.values(), key=lambda v: v["views"], reverse=True)


def winners(conn: sqlite3.Connection, days: int = 7, n: int = 3) -> list[dict[str, Any]]:
    """Best recent videos by views (only ones with any views)."""
    return [v for v in video_totals(conn, days) if v["views"] > 0][:n]


def winner_boost(conn: sqlite3.Connection, boost: float, days: int = 7) -> dict[str, float]:
    """Score multipliers for the categories of recent winners (used by rank.pick)."""
    return {w["category"]: 1 + boost for w in winners(conn, days) if w.get("category")} if boost else {}


def winners_prompt(conn: sqlite3.Connection, days: int = 7) -> str:
    """Few-shot lines for the script prompt; empty until there's data."""
    ws = winners(conn, days)
    if not ws:
        return ""
    lines = [f'  • "{w["title"]}" ({w["category"]}, {w["views"]:,} views)' for w in ws]
    return ("Recent on-screen hook titles that performed best on this channel — match their energy and "
            "specificity, never copy them:\n" + "\n".join(lines))


def weekly_text(conn: sqlite3.Connection, days: int = 7) -> str:
    vids = video_totals(conn, days)
    end = datetime.now(timezone.utc)
    head = f"📈 Ra'ij weekly report ({(end - timedelta(days=days)):%b %d} – {end:%b %d})"
    if not vids:
        return f"{head}\nNothing published in the last {days} days."
    tot = {k: sum(v[k] for v in vids) for k in ("views", "likes", "comments", "shares")}
    ret = [v["retention_pct"] for v in vids if v["retention_pct"] is not None]
    lines = [head, f"{len(vids)} video(s) · {tot['views']:,} views · {tot['likes']:,} likes · "
                   f"{tot['comments']:,} comments · {tot['shares']:,} shares"]
    if ret:
        lines.append(f"Avg watched on YouTube: {sum(ret) / len(ret):.0f}%")
    lines.append("")
    # An Arabic title and Latin numbers on one line get jumbled by RTL rendering: title alone, numbers next line.
    for i, v in enumerate(vids, 1):
        per = " · ".join(f"{n:,} {p}" for p, n in sorted(v["platforms"].items()))
        watched = f" · {v['retention_pct']:.0f}% watched" if v["retention_pct"] is not None else ""
        lines.append(f"{i}. {v['title']}")
        lines.append(f"   {v['views']:,} views ({per}){watched}")
    series: dict[str, int] = {}
    for v in vids:
        key = v["series"] or v["category"] or "?"
        series[key] = series.get(key, 0) + v["views"]
    lines += ["", "By series: " + " · ".join(f"{k} {n:,}" for k, n in sorted(series.items(), key=lambda kv: -kv[1]))]
    ws = [w["category"] for w in winners(conn, days) if w.get("category")]
    if ws:
        lines.append("Next picks lean toward: " + ", ".join(dict.fromkeys(ws)))
    return "\n".join(lines)
