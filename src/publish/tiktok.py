"""TikTok v1: no API (approval takes weeks) — copy the MP4 + caption into a dated folder for manual upload."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from src.config import Config
from src.publish.common import Posted, PostText


def missing(cfg: Config) -> str | None:
    return None


def export(cfg: Config, client, video: Path, text: PostText, video_id: int) -> Posted:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = cfg.root / "data" / "export" / "tiktok" / day
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{video_id}.mp4"
    shutil.copyfile(video, dest)
    dest.with_suffix(".txt").write_text(text.caption + "\n", encoding="utf-8")
    return Posted(str(video_id), str(dest.relative_to(cfg.root)), status="exported")
