"""publish: post approved videos to each of the brand's platforms.

Hard rules: a video is published only if its status is 'approved' AND its latest review decision is an
`approvals` row with decision 'approved' for that exact video id; the /pause kill switch stops everything.
One `posts` row per (video, platform): queued → published | exported | failed. A failure counts an
attempt and is retried on later runs up to publish.max_attempts, then the owner gets a Telegram alert.
Retries back off (publish.retry_after_hours, default 1 h / 6 h / 24 h from the last attempt) so three
ticks in a row can't burn every attempt during one 30-minute outage (A8).
Published/exported posts are never redone. A platform without keys is skipped (no row), so adding keys
later picks up approved videos — unless they're older than publish.max_age_hours (trends go stale).
Posting windows (Phase 12, `publish.windows`): a video's *first* upload waits for an open window and each window
takes one video (oldest approval first); a video already partly out finishes its platforms whenever due.
When every platform is done, the video becomes 'published'. An approved video older than max_age_hours
is closed (`finalize`): 'published' if anything went out (skipped platforms noted), else 'expired' — so
missing keys or dead retries can't keep it, and its media, in the state bundle forever (A3).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from src import db
from src.assemble.render import guard
from src.config import Config
from src.discover.common import FetchError, make_client
from src.lock import Busy, single
from src.publish import meta, tiktok, windows, youtube
from src.publish.common import Posted, PostText, PublishError, PublishSkipped, post_text

log = logging.getLogger("raij.publish")

Publisher = Callable[[Config, httpx.Client, Path, PostText, int], Posted]
PLATFORMS: dict[str, tuple[Callable[[Config], str | None], Publisher]] = {
    "youtube": (youtube.missing, youtube.publish),
    "instagram": (meta.missing_instagram, meta.publish_instagram),
    "facebook": (meta.missing_facebook, meta.publish_facebook),
    "tiktok_export": (tiktok.missing, tiktok.export),
}
DONE = ("published", "exported")
PLATFORM_NAME = {"youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "tiktok_export": "TikTok"}


def eligible(conn: sqlite3.Connection, max_age_hours: float | None = None) -> list[dict[str, Any]]:
    """Approved videos whose latest approve/reject decision is an approval of that same video id."""
    age = f"AND a.decided_at >= datetime('now', '-{float(max_age_hours)} hours')" if max_age_hours else ""
    rows = conn.execute(
        "SELECT v.*, a.id AS approval_id, x.brand_id, x.beats, x.description_en, x.hashtags, "
        "x.notes AS script_notes, s.sources, c.category "
        "FROM videos v "
        "JOIN approvals a ON a.id = (SELECT max(id) FROM approvals WHERE video_id = v.id "
        "                            AND decision IN ('approved', 'rejected')) "
        "JOIN scripts x ON x.id = v.script_id JOIN stories s ON s.id = x.story_id "
        "JOIN candidates c ON c.id = s.candidate_id "
        f"WHERE v.status = 'approved' AND a.decision = 'approved' AND a.video_id = v.id {age} ORDER BY v.id"
    ).fetchall()
    return [dict(r) for r in rows]


def _brand(cfg: Config, brand_id: str) -> dict[str, Any]:
    return next((b for b in cfg.brands if b["id"] == brand_id), {"id": brand_id})


def _post(conn: sqlite3.Connection, video: dict[str, Any], platform: str) -> dict[str, Any]:
    conn.execute("INSERT OR IGNORE INTO posts (video_id, approval_id, platform) VALUES (?, ?, ?)",
                 (video["id"], video["approval_id"], platform))
    return dict(conn.execute("SELECT * FROM posts WHERE video_id = ? AND platform = ?",
                             (video["id"], platform)).fetchone())


def retry_due(post: dict[str, Any], schedule: list[float], now: datetime | None = None) -> bool:
    """A failed post may be retried once its backoff since the last attempt has passed: schedule[k-1]
    hours after attempt k (the last value repeats). A post never attempted is always due."""
    if not post.get("attempts") or not post.get("last_attempt_at"):
        return True
    wait = schedule[min(int(post["attempts"]), len(schedule)) - 1] if schedule else 0
    last = datetime.strptime(post["last_attempt_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) >= last + timedelta(hours=float(wait))


def _notify(cfg: Config, lines: list[str], bot=None, retries: list[tuple[int, str]] | None = None) -> None:
    """One summary message, then one message with a 🔁 Retry button per video that ran out of attempts (U9)."""
    if not lines and not retries:
        return
    try:
        if bot is None:
            from src.review.runner import make_bot
            bot, chat = make_bot(cfg)
        else:
            chat = cfg.secret("TELEGRAM_CHAT_ID")
        if lines:
            bot.send_message(chat, "\n".join(lines), disable_web_page_preview=True)
        from src.review import cards
        for vid, text in retries or []:
            bot.send_message(chat, text, disable_web_page_preview=True, reply_markup=cards.retry_keyboard(vid))
    except Exception as exc:                            # an alert failing must not fail the publish
        log.warning("Telegram notice not sent: %s", exc)


def _label(v: dict[str, Any]) -> str:
    """'#20 «Arabic title»' — the id alone meant nothing to the owner (C)."""
    from src.review import cards
    return f"#{v['id']} «{cards.title_of(v)}»"


def _link(name: str, res: Posted) -> str:
    if res.status == "exported":                        # TikTok: the copy went to Telegram; a repo path is noise
        return "copy sent above — upload from your phone"
    return res.url


def publish(cfg: Config, conn: sqlite3.Connection, dry_run: bool = False, client: httpx.Client | None = None,
            platforms: dict | None = None, bot=None) -> int:
    """One publisher at a time across processes (scheduled job + run-daily), so nothing uploads twice."""
    if dry_run:
        return _publish(cfg, conn, True, client, platforms, bot)
    try:
        with single(cfg.root, "publish"):
            return _publish(cfg, conn, False, client, platforms, bot)
    except Busy as exc:
        log.info("Publish skipped: %s", exc)
        return 0


def _publish(cfg: Config, conn: sqlite3.Connection, dry_run: bool, client: httpx.Client | None,
             platforms: dict | None, bot) -> int:
    platforms = platforms or PLATFORMS
    max_attempts = int(cfg.get("publish.max_attempts", 3))
    backoff = [float(h) for h in cfg.get("publish.retry_after_hours", [1, 6, 24])]
    videos = eligible(conn, cfg.get("publish.max_age_hours", 72))
    missing = {name: check(cfg) for name, (check, _) in platforms.items()}

    if db.publishing_paused(conn):
        log.warning("Publishing is paused (kill switch) — %d approved video(s) wait; `resume` to continue", len(videos))
        if videos and not dry_run and db.get_flag(conn, "paused_notice_sent") != "1":   # once per pause (A9)
            _notify(cfg, [f"⏸ Publishing is paused — {len(videos)} approved video(s) waiting."], bot)
            db.set_flag(conn, "paused_notice_sent", "1")
        return 0
    gated = windows.enabled(cfg)
    window = windows.current(cfg) if gated else None
    slot_taken = bool(window and windows.taken(conn, window))
    if dry_run:
        log.info("[dry run] %d approved video(s) to publish", len(videos))
        if gated:
            log.info("[dry run] posting window: %s", _window_text(cfg, window, slot_taken))
        for name, why in missing.items():
            log.info("[dry run] %s: %s", name, f"skipped — {why}" if why else "ready")
        for v in videos:
            want = [p for p in _brand(cfg, v["brand_id"]).get("platforms", platforms) if p in platforms]
            log.info("[dry run] video %d (approval %d) → %s", v["id"], v["approval_id"], ", ".join(want))
        log.info("[dry run] nothing uploaded or written")
        return 0

    # The runs row is created at the first real attempt, so a pass with nothing to do writes nothing
    # (keeps idle GitHub Actions ticks from re-saving the state).
    run_id = None
    done, failed, skipped, notices, retries = [], [], set(), [], []
    waiting = 0
    own_client = client is None
    client = client or make_client()
    try:
        for v in videos:
            if gated and not windows.started(conn, v["id"]):
                if window is None or slot_taken:
                    waiting += 1
                    continue
            path = guard(cfg, Path(v["video_path"]))           # only our own rendered output leaves the machine
            text = post_text(v, cfg)
            want = [p for p in _brand(cfg, v["brand_id"]).get("platforms", list(platforms)) if p in platforms]
            first = not windows.started(conn, v["id"])
            for name in want:
                if missing[name]:
                    skipped.add(name)
                    continue
                post = _post(conn, v, name)
                if post["status"] in DONE or post["attempts"] >= max_attempts:
                    continue
                if not retry_due(post, backoff):
                    log.info("Video %d → %s: attempt %d failed at %s, backing off", v["id"], name,
                             post["attempts"], post["last_attempt_at"])
                    continue
                if run_id is None:
                    run_id = db.start_run(conn, "publish")
                conn.execute("UPDATE posts SET attempts = attempts + 1, last_attempt_at = datetime('now') "
                             "WHERE id = ?", (post["id"],))
                conn.commit()
                try:
                    res = platforms[name][1](cfg, client, path, text, v["id"])
                except (PublishError, PublishSkipped, FetchError, OSError, ValueError, KeyError) as exc:
                    msg = f"{type(exc).__name__}: {exc}"[:500]
                    conn.execute("UPDATE posts SET status = 'failed', error = ? WHERE id = ?", (msg, post["id"]))
                    conn.commit()
                    last = post["attempts"] + 1 >= max_attempts
                    log.warning("Video %d → %s failed (attempt %d/%d): %s", v["id"], name, post["attempts"] + 1,
                                max_attempts, msg)
                    failed.append({"video_id": v["id"], "platform": name, "error": msg, "final": last})
                    if last:
                        retries.append((v["id"], f"⚠️ {_label(v)}\n{PLATFORM_NAME.get(name, name)} failed "
                                                 f"{max_attempts}× — giving up.\n{msg[:200]}"))
                    continue
                conn.execute("UPDATE posts SET status = ?, external_id = ?, url = ?, error = NULL, "
                             "published_at = datetime('now') WHERE id = ?",
                             (res.status, res.external_id, res.url, post["id"]))
                conn.commit()
                done.append({"video_id": v["id"], "platform": name, "url": res.url})
                if gated and first:
                    slot_taken = True                        # this window's one video went out
                notices.append(f"{'📲' if res.status == 'exported' else '✅'} {_label(v)}\n"
                               f"{PLATFORM_NAME.get(name, name)}: {_link(name, res)}")
                log.info("Video %d → %s %s: %s", v["id"], name, res.status, res.url)
            finished = conn.execute("SELECT count(*) FROM posts WHERE video_id = ? AND status IN ('published', 'exported')",
                                    (v["id"],)).fetchone()[0]
            if want and finished == len(want):
                conn.execute("UPDATE videos SET status = 'published' WHERE id = ?", (v["id"],))
                conn.commit()
    finally:
        if own_client:
            client.close()

    for name in sorted(skipped):
        log.info("%s skipped: %s", name, missing[name])
    if waiting:
        log.info("%d approved video(s) wait for a posting window — %s", waiting, _window_text(cfg, window, slot_taken))
    closed = finalize(cfg, conn, cfg.get("publish.max_age_hours", 72), platforms=platforms)
    notices += [f"🏁 #{vid} closed as {status} (older than {cfg.get('publish.max_age_hours', 72)} h)"
                for vid, status in closed]
    if run_id is None:
        log.info("Publish: nothing new to post (%d approved video(s) checked)", len(videos))
        _notify(cfg, notices, bot)
        return 0
    _notify(cfg, notices, bot, retries)
    status = "failed" if failed and not done else ("partial" if failed else "ok")
    notes = {"videos": len(videos), "done": done, "failed": failed,
             "skipped": {n: missing[n] for n in sorted(skipped)}}
    db.finish_run(conn, run_id, status, notes)
    level = logging.INFO if status == "ok" else logging.WARNING
    log.log(level, "Publish %s: %d post(s) done, %d failed, %d video(s) eligible", status, len(done), len(failed),
            len(videos))
    return 1 if status == "failed" else 0


def _window_text(cfg: Config, window: windows.Window | None, taken: bool) -> str:
    tz = str(cfg.get("publish.windows.timezone") or cfg.get("schedule.timezone") or "UTC")
    if window is None:
        nxt = windows.next_start(cfg)
        return f"none open now; next opens {nxt.astimezone(ZoneInfo(tz)):%H:%M} {tz}" if nxt else "none configured"
    return f"{window.label} {tz} open" + (" (already used)" if taken else " (free)")


def finalize(cfg: Config, conn: sqlite3.Connection, max_age_hours: float | None, dry_run: bool = False,
             platforms: dict | None = None) -> list[tuple[int, str]]:
    """Close approved videos that won't go anywhere else. With `max_age_hours`, every approved video older than
    that (which `eligible()` no longer offers to publish). With None (the `finalize` command), only videos whose
    wanted platforms are each done, unconfigured or out of attempts — never one still being uploaded.
    Result 'published' if any platform went out, else 'expired'; what was skipped is kept in videos.notes.publish."""
    platforms = platforms or PLATFORMS
    max_attempts = int(cfg.get("publish.max_attempts", 3))
    missing = {name: check(cfg) for name, (check, _) in platforms.items()}
    fresh = {v["id"] for v in eligible(conn, max_age_hours)} if max_age_hours else set()
    closed: list[tuple[int, str]] = []
    for v in eligible(conn):
        if v["id"] in fresh:
            continue
        want = [p for p in _brand(cfg, v["brand_id"]).get("platforms", list(platforms)) if p in platforms]
        posts = {r["platform"]: dict(r) for r in conn.execute("SELECT * FROM posts WHERE video_id = ?", (v["id"],))}
        done = [p for p in want if posts.get(p, {}).get("status") in DONE]
        pending = [p for p in want if p not in done and not missing[p]
                   and posts.get(p, {}).get("attempts", 0) < max_attempts]
        if max_age_hours is None and pending:
            continue
        status = "published" if done else "expired"
        skipped = {p: (missing[p] or (posts.get(p) or {}).get("error") or "not attempted")
                   for p in want if p not in done}
        closed.append((v["id"], status))
        if dry_run:
            continue
        notes = json.loads(v["notes"] or "{}")
        notes["publish"] = {"done": done, "skipped": skipped, "closed_at": _now()}
        conn.execute("UPDATE videos SET status = ?, notes = ? WHERE id = ?",
                     (status, json.dumps(notes, ensure_ascii=False), v["id"]))
        conn.commit()
        log.info("Video %d closed as %s (done: %s; skipped: %s)", v["id"], status, ", ".join(done) or "-",
                 ", ".join(skipped) or "-")
    return closed


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
