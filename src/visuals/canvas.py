"""Shared drawing for generated visuals: the channel palette, easing, Arabic text, number formatting, and the
frame writer that pipes raw RGB frames into ffmpeg."""
from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from src.assemble import brand                     # loads raqm before Pillow's font module
from PIL import Image, ImageDraw                   # noqa: E402

from src.config import Config                      # noqa: E402

FPS = 30
# خريطة المال: gold on deep navy.
OCEAN = (9, 20, 34)
LAND = (24, 41, 61)
BORDER = (46, 70, 98)
GOLD = (255, 196, 0)
GOLD_DIM = (150, 118, 20)
TEAL = (46, 196, 182)
WHITE = (255, 255, 255)
MUTED = (160, 176, 196)
GRID = (40, 58, 82)
PANEL = (14, 28, 46)
SERIES_COLORS = (GOLD, TEAL, (240, 110, 90), (150, 130, 255), (120, 200, 90))

FrameSink = Callable[[Iterable[Image.Image], Path, tuple[int, int]], Path]


def ease(t: float) -> float:
    """Smooth in-out on [0, 1]."""
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def ease_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


def phase(t: float, start: float, end: float) -> float:
    """Progress 0→1 of an animation running from `start` to `end` (seconds) at time t."""
    if end <= start:
        return 1.0 if t >= end else 0.0
    return min(max((t - start) / (end - start), 0.0), 1.0)


def text(draw_on: Image.Image, xy: tuple[float, float], s: str, size: int, fill=WHITE, anchor: str = "ra",
         weight: int = 900, stroke: int = 0, stroke_fill=(0, 0, 0), alpha: float = 1.0) -> tuple[int, int, int, int]:
    """Draw one line of (Arabic-shaped) text; anchor like Pillow's ("ra" = right/ascender). Returns its box."""
    f = brand.font(size, weight)
    kw = brand.text_kw(s)
    if alpha < 1.0:
        layer = Image.new("RGBA", draw_on.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.text(xy, s, font=f, fill=(*fill[:3], int(255 * alpha)), anchor=anchor, stroke_width=stroke,
               stroke_fill=(*stroke_fill[:3], int(255 * alpha)), **kw)
        draw_on.paste(layer, (0, 0), layer)
        return d.textbbox(xy, s, font=f, anchor=anchor, stroke_width=stroke, **kw)
    d = ImageDraw.Draw(draw_on)
    d.text(xy, s, font=f, fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=stroke_fill, **kw)
    return d.textbbox(xy, s, font=f, anchor=anchor, stroke_width=stroke, **kw)


class Labels:
    """Keeps labels from piling up: a label whose box overlaps one already placed is skipped."""

    def __init__(self, reserved: list[tuple[float, float, float, float]] | None = None):
        self.boxes: list[tuple[float, float, float, float]] = list(reserved or [])

    def free(self, box: tuple[float, float, float, float], pad: float = 6) -> bool:
        l, t, r, b = box
        for L, T, R, B in self.boxes:
            if l - pad < R and r + pad > L and t - pad < B and b + pad > T:
                return False
        self.boxes.append(box)
        return True


def text_box(xy: tuple[float, float], s: str, size: int, anchor: str = "ra", weight: int = 900,
             stroke: int = 0) -> tuple[int, int, int, int]:
    f = brand.font(size, weight)
    return ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(xy, s, font=f, anchor=anchor, stroke_width=stroke,
                                                             **brand.text_kw(s))


def text_width(s: str, size: int, weight: int = 900) -> int:
    f = brand.font(size, weight)
    l, _, r, _ = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), s, font=f, **brand.text_kw(s))
    return int(r - l)


def fit_size(s: str, size: int, max_px: int, floor: int = 18) -> int:
    while size > floor and text_width(s, size) > max_px:
        size -= 2
    return size


def number(value: float, unit: str | None = None) -> str:
    """Readable Arabic figure: 1234567 → '1.23 مليون', 2.5 → '2.5'; digits stay Western (as the Gulf writes
    them and as the voice reads them). A '%' unit sticks to the number."""
    a = abs(value)
    if a >= 1e12:
        s = f"{value / 1e12:.2f}".rstrip("0").rstrip(".") + " تريليون"
    elif a >= 1e9:
        s = f"{value / 1e9:.1f}".rstrip("0").rstrip(".") + " مليار"
    elif a >= 1e6:
        s = f"{value / 1e6:.1f}".rstrip("0").rstrip(".") + " مليون"
    elif a >= 1e4:
        s = f"{value:,.0f}"
    elif a >= 100:
        s = f"{value:,.0f}"
    elif a >= 10:
        s = f"{value:.1f}".rstrip("0").rstrip(".")
    else:
        s = f"{value:.2f}".rstrip("0").rstrip(".")
    if unit in ("%", "٪"):
        return f"{s}%"
    return f"{s} {unit}" if unit else s


def notes(img: Image.Image, lines: list[str | None], scale: float) -> float:
    """Small muted lines under the title (what the chart shows, its source); returns the y below them. They
    live up here because the burned-in subtitles own the lower third of every frame."""
    W, _ = img.size
    y = 256 * scale
    for line in lines:
        if not line:
            continue
        size = fit_size(line, int(28 * scale), int(W * 0.8))
        text(img, (W - 64 * scale, y), line, size, fill=MUTED, anchor="ra", weight=600)
        y += 40 * scale
    return y


def safe_bottom(size: tuple[int, int]) -> float:
    """Lowest y a chart may use. Shorts keep their subtitles over the visuals, so it stops above the band
    (src/assemble/subtitles.Style.for_frame); long videos mute subtitles while their own visuals play."""
    W, H = size
    return (1250 - 40) * H / 1920 if H > W else H - 110


def title_card(img: Image.Image, title: str | None, scale: float, alpha: float = 1.0) -> None:
    """The visual's own title, top right, on a gold rule."""
    if not title:
        return
    W, _ = img.size
    size = fit_size(title, int(52 * scale), int(W * 0.8))
    box = text(img, (W - 64 * scale, 150 * scale), title, size, fill=WHITE, anchor="ra", alpha=alpha,
               stroke=max(1, int(3 * scale)), stroke_fill=OCEAN)
    if alpha <= 0:
        return
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rectangle((box[0], box[3] + 10 * scale, W - 64 * scale, box[3] + 16 * scale),
                                    fill=(*GOLD, int(255 * alpha)))
    img.paste(layer, (0, 0), layer)


def ffmpeg_sink(cfg: Config) -> FrameSink:
    """Write frames (RGB, all `size`) to an H.264 mp4 at FPS."""
    def sink(frames: Iterable[Image.Image], out: Path, size: tuple[int, int]) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.stem + ".part.mp4")
        cmd = [cfg.secret("FFMPEG_BIN", "ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-r", str(FPS), "-i", "-",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(part)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for frame in frames:
                proc.stdin.write(frame.convert("RGB").tobytes())
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace")
            code = proc.wait(timeout=600)
        except BaseException:
            proc.kill()
            part.unlink(missing_ok=True)
            raise
        if code != 0 or not part.exists():
            part.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg (visual) failed: {err.strip()[-300:]}")
        part.rename(out)
        return out
    return sink


def frame_count(seconds: float) -> int:
    return max(1, int(math.ceil(seconds * FPS)))
