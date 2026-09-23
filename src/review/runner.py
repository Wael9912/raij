"""review: send each rendered video to the reviewer's Telegram chat with decision buttons.

The buttons are handled by the long-running `bot` command (src/review/bot.py). A video that was
sent is 'in_review' with its message id stored, so re-running `review` never sends it twice.
Videos over Telegram's 50 MB bot limit are sent as a smaller preview copy; the full-quality file
is what gets published.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable

from src import db
from src.assemble import render
from src.config import Config
from src.review import cards
from src.review.telegram import MAX_VIDEO_BYTES, Bot, TelegramError

log = logging.getLogger("raij.review")

RunCmd = Callable[[list[str]], subprocess.CompletedProcess]


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def make_bot(cfg: Config, client=None) -> tuple[Bot, str]:
    chat = cfg.secret("TELEGRAM_CHAT_ID")
    if not chat:
        raise TelegramError("TELEGRAM_CHAT_ID is not set")
    return Bot(cfg.secret("TELEGRAM_BOT_TOKEN") or "", client), str(chat)


def preview_for(cfg: Config, path: Path, seconds: float, run: RunCmd = run_cmd) -> Path:
    """The file to upload: the video itself, or a re-encoded copy that fits the bot upload limit."""
    if path.stat().st_size <= MAX_VIDEO_BYTES:
        return path
    preview = path.with_name(path.stem + ".preview.mp4")
    if preview.exists() and preview.stat().st_mtime >= path.stat().st_mtime:
        return preview
    budget_bits = 44 * 1024 * 1024 * 8                        # leave headroom under 50 MB
    video_kbps = max(int(budget_bits / max(seconds, 1) / 1000) - 128, 800)
    proc = run([cfg.secret("FFMPEG_BIN", "ffmpeg"), "-hide_banner", "-nostats", "-y", "-i", str(path),
                "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k",
                "-bufsize", f"{video_kbps * 2}k", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                str(preview)])
    if proc.returncode != 0 or not preview.exists():
        raise TelegramError(f"preview encode failed: {(proc.stderr or '').strip()[-200:]}")
    return preview


def send_card(cfg: Config, conn: sqlite3.Connection, bot: Bot, chat: str, video_id: int,
              run: RunCmd = run_cmd) -> int:
    """Send one video for review; returns the Telegram message id holding the buttons."""
    ctx = cards.context(conn, video_id)
    # Same guardrail as publish (S7): only our own generated media ever leaves the machine.
    path = preview_for(cfg, render.guard(cfg, Path(ctx["video_path"])), ctx.get("duration_s") or 60, run=run)
    msg = bot.send_video(chat, path, cards.caption(ctx), reply_markup=cards.keyboard(video_id))
    bot.send_message(chat, cards.script_text(ctx), reply_to_message_id=msg["message_id"])
    conn.execute("UPDATE videos SET status = 'in_review', review_msg_id = ? WHERE id = ?",
                 (msg["message_id"], video_id))
    conn.commit()
    return msg["message_id"]


def _pending(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM videos WHERE status = 'rendered' AND review_msg_id IS NULL ORDER BY id")]


def send_digest(cfg: Config, conn: sqlite3.Connection, bot: Bot, chat: str, video_ids: list[int]) -> None:
    """One message before the day's burst of cards: titles, flagged items, stage trouble. Cosmetic — a failure
    here must not stop the cards."""
    if not video_ids:
        return
    try:
        bot.send_message(chat, cards.digest_text(conn, video_ids), disable_web_page_preview=True)
    except TelegramError as exc:
        log.warning("Digest not sent: %s", exc)


REMIND_AT = (48, 72)          # hours in review; the second is publish.max_age_hours by default


def remind(cfg: Config, conn: sqlite3.Connection, bot: Bot, chat: str, now=None) -> list[int]:
    """Warn about cards waiting too long (U5) — never decide for the owner (their call: warn only). Each card gets
    one reminder per level, recorded in videos.notes.reminded_h; the caption gets a ⌛ line, buttons stay."""
    max_age = int(cfg.get("publish.max_age_hours", 72))
    levels = sorted({REMIND_AT[0], max_age} if max_age > REMIND_AT[0] else {max_age})
    due: dict[int, list[dict]] = {}
    for v in cards.in_review(conn):
        hours = cards.age_hours(v["created_at"], now) or 0
        done = int((json.loads(v["notes"] or "{}") or {}).get("reminded_h") or 0)
        level = max((lv for lv in levels if hours >= lv), default=0)
        if level and level > done:
            due.setdefault(level, []).append(v)
    warned: list[int] = []
    for level, group in sorted(due.items()):
        try:
            bot.send_message(chat, cards.reminder_text(group, level, max_age), disable_web_page_preview=True)
        except TelegramError as exc:
            log.warning("Reminder not sent: %s", exc)
            continue
        for v in group:
            notes = json.loads(v["notes"] or "{}") or {}
            notes["reminded_h"] = level
            conn.execute("UPDATE videos SET notes = ? WHERE id = ?", (json.dumps(notes, ensure_ascii=False), v["id"]))
            msg_id = conn.execute("SELECT review_msg_id FROM videos WHERE id = ?", (v["id"],)).fetchone()[0]
            try:
                ctx = cards.context(conn, v["id"])
                bot.edit_caption(chat, msg_id, f"⌛ In review for {cards.age_text(v['created_at'], now)}\n\n"
                                 + cards.caption(ctx), reply_markup=cards.keyboard(v["id"]))
            except (TelegramError, KeyError) as exc:
                log.info("Caption not updated for #%d: %s", v["id"], exc)
            warned.append(v["id"])
        conn.commit()
    return warned


def review(cfg: Config, conn: sqlite3.Connection, dry_run: bool = False, bot: Bot | None = None,
           run: RunCmd = run_cmd) -> int:
    pending = _pending(conn)
    if dry_run:
        ready = bool(cfg.secret("TELEGRAM_BOT_TOKEN") and cfg.secret("TELEGRAM_CHAT_ID"))
        log.info("[dry run] %d rendered video(s) to send for review; Telegram %s", len(pending),
                 "configured" if ready else "NOT configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
        for vid in pending:
            ctx = cards.context(conn, vid)
            size = (cfg.root / ctx["video_path"]).stat().st_size / 1e6 if ctx.get("video_path") else 0
            log.info("[dry run] video %d (%.1f MB%s): %s", vid, size, ", preview copy" if size * 1e6 > MAX_VIDEO_BYTES
                     else "", (ctx.get("title") or "")[:60])
        log.info("[dry run] nothing sent")
        return 0

    run_id = db.start_run(conn, "review")
    sent, retry = [], []
    try:
        if bot is None:
            bot, chat = make_bot(cfg)
        else:
            chat = str(cfg.secret("TELEGRAM_CHAT_ID") or "")
    except TelegramError as exc:
        log.error("Telegram not configured: %s (see SETUP.md §4)", exc)
        retry = [{"video_id": v, "error": str(exc)} for v in pending]
        pending = []
    if pending:
        send_digest(cfg, conn, bot, chat, pending)
    for vid in pending:
        try:
            send_card(cfg, conn, bot, chat, vid, run=run)
        except (TelegramError, OSError) as exc:
            log.error("Video %d: send failed, will retry next run: %s", vid, exc)
            retry.append({"video_id": vid, "error": str(exc)})
            continue
        sent.append(vid)
        log.info("Video %d sent for review", vid)
    total = len(sent) + len(retry)
    status = "ok" if len(sent) == total else ("partial" if sent else "failed")
    db.finish_run(conn, run_id, status, {"pending": total, "sent": len(sent), "retry": retry})
    log.log(logging.INFO if status == "ok" else logging.WARNING, "Review %s: %d/%d sent", status, len(sent), total)
    return 1 if status == "failed" else 0
