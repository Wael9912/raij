"""Thumbnail for long videos (Phase 15): the sharpest, best-lit frame of the render with the hook title on it.

No image model: a frame is pulled from each of the video's first stock clips (the *clean* clips, not the finished
render — that carries subtitles, the logo and mid-transition blends), scored on brightness, contrast and edge
density (a flat dark frame scores low), and the winner gets the title (≤ 6 words, big), the series pill and the
logo. 1280×720 JPEG under 2 MB — what `thumbnails.set` accepts.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageStat

from src.assemble import brand
from src.config import Config

log = logging.getLogger("raij.assemble")

RunCmd = Callable[[list[str]], subprocess.CompletedProcess]
SIZE = (1280, 720)
POSITIONS = (0.15, 0.35, 0.55, 0.75)
MAX_SOURCES = 6


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def score(img: Image.Image) -> float:
    """Higher is better: mid brightness, high contrast, plenty of edges (detail), never near black."""
    g = img.convert("L").resize((320, 180))
    stat = ImageStat.Stat(g)
    mean, stddev = stat.mean[0], stat.stddev[0]
    edges = ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).mean[0]
    brightness = 1 - abs(mean - 128) / 128                 # 1 at mid grey, 0 at black/white
    return brightness * 0.4 + min(stddev / 64, 1) * 0.35 + min(edges / 20, 1) * 0.25


def frames(cfg: Config, video: Path, body_seconds: float, out_dir: Path, run: RunCmd = run_cmd) -> list[Path]:
    """Frames at POSITIONS of a video file (fallback when no clean clip is available)."""
    ffmpeg = cfg.secret("FFMPEG_BIN", "ffmpeg")
    out_dir.mkdir(parents=True, exist_ok=True)
    got = []
    for i, pos in enumerate(POSITIONS):
        at = max(body_seconds * pos, 0.5)
        path = out_dir / f"thumb_{i}.png"
        proc = run([ffmpeg, "-hide_banner", "-nostats", "-y", "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1",
                    str(path)])
        if proc.returncode == 0 and path.exists():
            got.append(path)
    return got


def clip_frames(cfg: Config, clips: list[Path], out_dir: Path, run: RunCmd = run_cmd) -> list[Path]:
    """One frame from the middle of each stock clip (photos as they are)."""
    ffmpeg = cfg.secret("FFMPEG_BIN", "ffmpeg")
    out_dir.mkdir(parents=True, exist_ok=True)
    got = []
    for i, clip in enumerate(clips[:MAX_SOURCES]):
        if clip.suffix.lower() in (".jpg", ".jpeg", ".png"):
            got.append(clip)
            continue
        path = out_dir / f"thumb_c{i}.png"
        proc = run([ffmpeg, "-hide_banner", "-nostats", "-y", "-ss", "2", "-i", str(clip), "-frames:v", "1", str(path)])
        if proc.returncode == 0 and path.exists():
            got.append(path)
    return got


def compose(frame: Image.Image, title: str | None, series: str | None, look: dict, out: Path) -> Path:
    img = frame.convert("RGB")
    img = img.resize(SIZE) if img.size != SIZE else img
    # A darker bottom half keeps the white title readable on any footage.
    shade = Image.new("L", SIZE, 0)
    ImageDraw.Draw(shade).rectangle((0, SIZE[1] // 2, SIZE[0], SIZE[1]), fill=140)
    shade = shade.filter(ImageFilter.GaussianBlur(60))
    img = Image.composite(ImageEnhance.Brightness(img).enhance(0.45), img, shade)
    canvas = img.convert("RGBA")
    y = SIZE[1] - 60
    if title:
        size = 96
        while size > 48 and len(brand.wrap(title, size, SIZE[0] - 120)) > 2:
            size -= 8
        lines = brand.wrap(title, size, SIZE[0] - 120)[:2]
        for line in reversed(lines):
            t = brand.text_image(line, size, stroke=max(4, size // 14))
            y -= t.height
            canvas.alpha_composite(t, ((SIZE[0] - t.width) // 2, y))
            y -= 8
    if series:
        p = brand.pill(series, 40)
        canvas.alpha_composite(p, ((SIZE[0] - p.width) // 2, y - p.height - 10))
    mark = brand.logo(look, height=64)
    canvas.alpha_composite(mark, (SIZE[0] - mark.width - 36, 30))
    out.parent.mkdir(parents=True, exist_ok=True)
    rgb = canvas.convert("RGB")
    for quality in (90, 82, 72, 60):
        rgb.save(out, "JPEG", quality=quality, optimize=True)
        if out.stat().st_size <= 2 * 1024 * 1024:
            break
    return out


def make(cfg: Config, video: Path, body_seconds: float, title: str | None, series: str | None, look: dict,
         out: Path, work: Path, run: RunCmd = run_cmd, clips: list[Path] | None = None) -> Path | None:
    """Best frame + title → `out` JPEG; None (logged) if no frame could be read. Never raises. Frames come from
    the clean stock `clips` when given, else from the finished video."""
    try:
        shots = clip_frames(cfg, clips, work, run=run) if clips else []
        if not shots:
            shots = frames(cfg, video, body_seconds, work, run=run)
        if not shots:
            log.warning("Thumbnail: no frame could be extracted from %s", video.name)
            return None
        best = max(shots, key=lambda p: score(Image.open(p)))
        return compose(Image.open(best), title, series, look, out)
    except Exception as exc:                                # cosmetic: the video itself is done
        log.warning("Thumbnail not made: %s", exc)
        return None
