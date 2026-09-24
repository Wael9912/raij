"""TikTok v1: no API (approval takes weeks). The MP4 + caption are copied into a dated folder and, when
Telegram is set up, sent to the owner's chat — upload from the phone: save the video, paste the caption.
On hosts without a disk you can reach (GitHub Actions) the Telegram copy is the one that counts, so a
failed send fails the post (and it's retried)."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from src.config import Config
from src.publish.common import Posted, PostText, PublishError


def missing(cfg: Config) -> str | None:
    from src.publish import tiktok_api
    if tiktok_api.connected(cfg):
        return "TikTok app connected — the Telegram copy isn't needed (platform `tiktok`)"
    return None


def _send(cfg: Config, video: Path, text: PostText, video_id: int) -> None:
    from src.review.runner import make_bot, preview_for
    from src.review.telegram import TelegramError
    try:
        bot, chat = make_bot(cfg)
    except TelegramError:
        return                                            # no Telegram: the folder copy is it
    try:
        msg = bot.send_video(chat, preview_for(cfg, video, 60), f"📲 TikTok — #{video_id}: save this video and "
                                                                 f"upload it; caption below ⬇️")
        bot.send_message(chat, text.caption, reply_to_message_id=msg["message_id"])
    except TelegramError as exc:
        raise PublishError(f"couldn't send the TikTok copy to Telegram: {exc}") from None


def export(cfg: Config, client, video: Path, text: PostText, video_id: int) -> Posted:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = cfg.root / "data" / "export" / "tiktok" / day
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{video_id}.mp4"
    shutil.copyfile(video, dest)
    dest.with_suffix(".txt").write_text(text.caption + "\n", encoding="utf-8")
    _send(cfg, video, text, video_id)
    return Posted(str(video_id), str(dest.relative_to(cfg.root)), status="exported")
