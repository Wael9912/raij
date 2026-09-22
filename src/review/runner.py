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
    path = preview_for(cfg, cfg.root / ctx["video_path"], ctx.get("duration_s") or 60, run=run)
    msg = bot.send_video(chat, path, cards.caption(ctx), reply_markup=cards.keyboard(video_id))
    bot.send_message(chat, cards.script_text(ctx), reply_to_message_id=msg["message_id"])
    conn.execute("UPDATE videos SET status = 'in_review', review_msg_id = ? WHERE id = ?",
                 (msg["message_id"], video_id))
    conn.commit()
    return msg["message_id"]


def _pending(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM videos WHERE status = 'rendered' AND review_msg_id IS NULL ORDER BY id")]


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

    run_id = conn.execute("INSERT INTO runs (command) VALUES ('review')").lastrowid
    conn.commit()
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
    conn.execute("UPDATE runs SET finished_at = datetime('now'), status = ?, notes = ? WHERE id = ?",
                 (status, json.dumps({"pending": total, "sent": len(sent), "retry": retry}, ensure_ascii=False),
                  run_id))
    conn.commit()
    log.log(logging.INFO if status == "ok" else logging.WARNING, "Review %s: %d/%d sent", status, len(sent), total)
    return 1 if status == "failed" else 0
