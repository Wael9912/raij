"""Channel look: the brand font, logo, series badge, animated hook title and end card.

All text is shaped by HarfBuzz (Pillow's raqm layout, see src/textshape.py), so any Arabic font
works — Cairo Black is the channel font (OFL, bundled). Graphics are transparent PNGs that the
render overlays; the animated hook title is a short PNG sequence played through ffmpeg's concat
demuxer, like the subtitles.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from src import textshape

textshape.ensure()                                   # before PIL's font module loads
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps  # noqa: E402

from src.config import ROOT  # noqa: E402

log = logging.getLogger("raij.assemble")

FONT = ROOT / "assets" / "fonts" / "Cairo-Variable.ttf"
LOGO = ROOT / "assets" / "brand" / "logo.png"     # optional owner-supplied logo; else a wordmark is drawn
W, H = 1080, 1920
YELLOW = (255, 212, 0, 255)
INK = (18, 22, 34, 255)
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)
_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


class ShapingError(RuntimeError):
    """Arabic can't be shaped (raqm missing) — rendering would produce broken text."""


@lru_cache(maxsize=32)
def font(size: int, weight: int = 900) -> ImageFont.FreeTypeFont:
    if not textshape.available():
        raise ShapingError("Arabic text shaping needs raqm — run `brew install libraqm` (see SETUP.md)")
    f = ImageFont.truetype(str(FONT), size, layout_engine=ImageFont.Layout.RAQM)
    axes = f.get_variation_axes()
    f.set_variation_by_axes([weight if a["name"] in (b"Weight", "Weight") else a["default"] for a in axes])
    return f


def text_kw(text: str) -> dict[str, str]:
    """Pillow draw/measure kwargs: right-to-left for anything containing Arabic."""
    return {"direction": "rtl" if _ARABIC.search(text) else "ltr", "language": "ar"}


def text_image(text: str, size: int, fill=WHITE, stroke: int = 0, stroke_fill=BLACK) -> Image.Image:
    """One line of text on a transparent image cropped to its ink (plus stroke)."""
    f = font(size)
    kw = text_kw(text)
    l, t, r, b = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), text, font=f, stroke_width=stroke, **kw)
    img = Image.new("RGBA", (int(r - l) + 2, int(b - t) + 2), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((-l + 1, -t + 1), text, font=f, fill=fill, stroke_width=stroke,
                             stroke_fill=stroke_fill, **kw)
    return img


def pill(text: str, size: int, fg=INK, bg=YELLOW, pad_x: float = 0.55, pad_y: float = 0.28) -> Image.Image:
    """Text on a rounded capsule, e.g. the series badge."""
    t = text_image(text, size, fill=fg)
    px, py = int(size * pad_x), int(size * pad_y)
    img = Image.new("RGBA", (t.width + 2 * px, t.height + 2 * py), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=img.height // 2, fill=bg)
    img.alpha_composite(t, (px, py))
    return img


def wrap(text: str, size: int, max_px: int) -> list[str]:
    """Greedy word wrap at max_px (every word kept), then rebalanced so two lines are about even."""
    f = font(size)
    words = text.split()
    width = lambda ws: f.getlength(" ".join(ws), **text_kw(" ".join(ws)))  # noqa: E731
    lines: list[list[str]] = [[]]
    for w in words:
        if lines[-1] and width(lines[-1] + [w]) > max_px:
            lines.append([])
        lines[-1].append(w)
    if len(lines) == 2:
        k = min(range(1, len(words)), key=lambda k: max(width(words[:k]), width(words[k:])))
        if max(width(words[:k]), width(words[k:])) <= max_px:
            lines = [words[:k], words[k:]]
    return [" ".join(line) for line in lines]


def logo(brand: dict[str, Any], height: int = 84) -> Image.Image:
    """The channel logo: assets/brand/logo.png if the owner supplied one, else a wordmark."""
    if LOGO.exists():
        img = Image.open(LOGO).convert("RGBA")
        return img.resize((max(1, round(img.width * height / img.height)), height), Image.LANCZOS)
    name = brand.get("name") or brand["id"]
    t = text_image(name, int(height * 0.62), fill=YELLOW)
    pad = int(height * 0.32)
    img = Image.new("RGBA", (t.width + 2 * pad, height), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, img.width - 1, height - 1), radius=height // 4, fill=(18, 22, 34, 200))
    img.alpha_composite(t, (pad, (height - t.height) // 2))
    return img


def series_name(brand: dict[str, Any], category: str | None, override: str | None = None) -> str | None:
    """The series badge for a video: the script's pick if it's one of the brand's series, else by category."""
    series = brand.get("series") or {}
    if override and override in series.values():
        return override
    return series.get(category or "") or series.get("default")


def logo_layer(brand: dict[str, Any], out: Path, opacity: float = 0.75) -> Path:
    """The logo as a translucent PNG; the render places it top-left (render.LOGO_XY)."""
    mark = logo(brand)
    mark.putalpha(mark.getchannel("A").point(lambda a: int(a * opacity)))
    mark.save(out, optimize=True)
    return out


TITLE_MIN_SIZE = 56


def _title_block(title: str, series: str | None, max_px: int = 960) -> Image.Image:
    size = 118
    while size > TITLE_MIN_SIZE and len(wrap(title, size, max_px)) > 2:   # shrink to fit two lines, never drop words
        size -= 6
    wrapped = wrap(title, size, max_px)
    if len(wrapped) > 2:
        # Still too long at the smallest readable size: a third line would sit on the subtitles (A14).
        # Keep two lines and mark the cut rather than overlap.
        log.warning("Hook title %r needs %d lines even at %dpx — truncated", title, len(wrapped), size)
        wrapped = wrapped[:2]
        wrapped[1] = wrapped[1] + "…"
    lines = [text_image(line, size, stroke=max(6, size // 12)) for line in wrapped]
    badge = pill(series, 52) if series else None
    gap = 22
    parts = ([badge] if badge else []) + lines
    w = max(p.width for p in parts)
    h = sum(p.height for p in parts) + gap * (len(parts) - 1) + (14 if badge else 0)
    block = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    y = 0
    for i, p in enumerate(parts):
        block.alpha_composite(p, ((w - p.width) // 2, y))
        y += p.height + gap + (14 if badge and i == 0 else 0)
    return block


def hook_sequence(title: str, series: str | None, out_dir: Path, seconds: float,
                  fps: int = 30, center_y: int | None = None, frame: tuple[int, int] = (W, H),
                  delay: float = 0.0) -> Path:
    """The hook title popping in at `delay` (after the cover card, Phase 20) and fading out by
    `delay + seconds`; returns an ffconcat list. The stream ends there and the overlay passes the video
    through (eof_action=pass)."""
    W, H = frame                                                       # noqa: N806
    center_y = center_y if center_y is not None else (820 if H > W else int(H * 0.42))
    out_dir.mkdir(parents=True, exist_ok=True)
    block = _title_block(title, series, max_px=960 if H > W else int(W * 0.8))
    blank = out_dir / "hook_blank.png"
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank)

    def frame(name: str, scale: float, alpha: float) -> Path:
        b = block.resize((max(1, round(block.width * scale)), max(1, round(block.height * scale))), Image.LANCZOS)
        if alpha < 1:
            b.putalpha(b.getchannel("A").point(lambda a: int(a * alpha)))
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        img.alpha_composite(b, ((W - b.width) // 2, center_y - b.height // 2))
        path = out_dir / f"hook_{name}.png"
        img.save(path, optimize=True)
        return path

    step = 1 / fps
    pop = [(0.55, 0.2), (0.75, 0.5), (0.92, 0.8), (1.06, 1.0), (1.03, 1.0), (1.0, 1.0)]   # scale, alpha
    fade = [0.75, 0.5, 0.25]
    entries = [(blank, delay)] if delay > 0 else []
    entries += [(frame(f"in{i}", s, a), step) for i, (s, a) in enumerate(pop)]
    hold = seconds - step * (len(pop) + len(fade))
    if hold > 0:
        entries.append((frame("hold", 1.0, 1.0), hold))
    entries += [(frame(f"out{i}", 1.0, a), step) for i, a in enumerate(fade)]
    entries.append((blank, step))

    lst = out_dir / "hook.txt"
    body = "ffconcat version 1.0\n" + "".join(f"file '{p.name}'\nduration {d:.3f}\n" for p, d in entries)
    lst.write_text(body + f"file '{entries[-1][0].name}'\n", encoding="utf-8")
    return lst


def cover(brand: dict[str, Any], out: Path, title: str | None, series: str | None = None,
          photo: Path | None = None, frame: tuple[int, int] = (W, H), with_logo: bool = True) -> Path:
    """The cover card (Phase 20): the story's real hero picture filling the frame, darkened towards the bottom,
    the hook title big and the series badge above it, the logo top-right. Opens every video for
    `video.cover_seconds` (so the first frame — what TikTok/Instagram/Telegram show before playing — *is* the
    thumbnail) and, at 1280×720, is the YouTube thumbnail of long videos. Without a photo: the brand ink."""
    W, H = frame                                                       # noqa: N806
    if photo and Path(photo).exists():
        img = ImageOps.exif_transpose(Image.open(photo)).convert("RGB")
        base = ImageOps.fit(img, (W, H), Image.LANCZOS)
        if img.width / img.height > 1.4 and H > W:
            # A wide news photo cropped to 9:16 loses its subject: show it whole over a blurred fill instead.
            bg = ImageEnhance.Brightness(base.filter(ImageFilter.GaussianBlur(40))).enhance(0.5)
            fg = ImageOps.contain(img, (W, int(H * 0.6)))
            bg.paste(fg, ((W - fg.width) // 2, int(H * 0.22)))
            base = bg
    else:
        base = Image.new("RGB", (W, H), INK[:3])
    shade = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shade).rectangle((0, int(H * 0.45), W, H), fill=170)
    shade = shade.filter(ImageFilter.GaussianBlur(90 if H > W else 60))
    base = Image.composite(ImageEnhance.Brightness(base).enhance(0.35), base, shade)
    canvas = base.convert("RGBA")
    max_px = 980 if H > W else int(W * 0.8)
    if title:
        size = 132 if H > W else 96
        while size > TITLE_MIN_SIZE and len(wrap(title, size, max_px)) > 2:
            size -= 6
        lines = wrap(title, size, max_px)[:2]
        blocks = [text_image(line, size, stroke=max(6, size // 12)) for line in lines]
        gap = 18
        total = sum(b.height for b in blocks) + gap * (len(blocks) - 1)
        y = int(H * (0.70 if H > W else 0.66)) - total // 2
        if series:
            badge = pill(series, 56 if H > W else 44)
            canvas.alpha_composite(badge, ((W - badge.width) // 2, y - badge.height - 26))
        for b in blocks:
            canvas.alpha_composite(b, ((W - b.width) // 2, y))
            y += b.height + gap
    elif series:
        badge = pill(series, 64)
        canvas.alpha_composite(badge, ((W - badge.width) // 2, int(H * 0.7)))
    if with_logo:
        mark = logo(brand, height=84 if H > W else 64)
        canvas.alpha_composite(mark, (W - mark.width - 48, 150 if H > W else 40))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out, "JPEG", quality=90)
    return out


def endcard(brand: dict[str, Any], out: Path, series: str | None = None, cta: str | None = None,
            frame: tuple[int, int] = (W, H)) -> Path:
    """Closing card: wordmark, series badge, closing line (`cta`, rotated per series in Phase 12)."""
    W, H = frame                                                       # noqa: N806
    scale = 1.0 if H > W else 0.72                                     # landscape has less height to fill
    img = Image.new("RGBA", (W, H), INK)
    parts = [text_image(brand.get("name") or brand["id"], int(260 * scale), fill=YELLOW)]
    if series:
        parts.append(pill(series, int(64 * scale)))
    line = cta or "تابعنا للمزيد"
    size = 88 if len(line) <= 18 else 68                       # longer lines still fit the width
    parts.append(text_image(line, int(size * scale), fill=WHITE))
    gap = int(70 * scale)
    y = (H - sum(p.height for p in parts) - gap * (len(parts) - 1)) // 2 - int(60 * scale)
    for p in parts:
        img.alpha_composite(p, ((W - p.width) // 2, y))
        y += p.height + gap
    img.convert("RGB").save(out)
    return out


def chapter_sequence(chapters: list[dict[str, Any]], out_dir: Path, total: float, frame: tuple[int, int] = (W, H),
                     seconds: float = 3.0, fps: int = 30) -> Path | None:
    """Long videos (Phase 15): each chapter title slides in as a yellow pill near the top for `seconds` at its
    start time. One ffconcat list of full-frame PNGs (blank between cards), like the hook title."""
    W, H = frame                                                       # noqa: N806
    cards = [c for c in chapters if c.get("title") and float(c.get("at") or 0) > 0.5]
    if not cards:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    blank = out_dir / "chapter_blank.png"
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank)
    entries: list[tuple[Path, float]] = []
    t = 0.0
    step = 1 / fps
    y = 110 if W > H else 300
    for i, c in enumerate(sorted(cards, key=lambda c: float(c["at"]))):
        at = float(c["at"])
        if at <= t:
            continue
        entries.append((blank, at - t))
        block = pill(str(c["title"]), 52 if W > H else 46)
        frames = [(0.4, 1.0), (0.7, 1.0), (0.9, 1.0), (1.0, 1.0)]        # slide in, hold, fade out
        for k, (sx, a) in enumerate(frames):
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            img.alpha_composite(block, ((W - block.width) // 2 - int((1 - sx) * 120), y))
            path = out_dir / f"chapter_{i}_{k}.png"
            img.save(path, optimize=True)
            entries.append((path, step))
        hold = max(seconds - step * 7, 0.5)
        entries.append((path, hold))
        for k, a in enumerate((0.66, 0.33)):
            faded = block.copy()
            faded.putalpha(faded.getchannel("A").point(lambda v, a=a: int(v * a)))
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            img.alpha_composite(faded, ((W - block.width) // 2, y))
            path = out_dir / f"chapter_{i}_out{k}.png"
            img.save(path, optimize=True)
            entries.append((path, step))
        t = at + step * 6 + hold
    if total > t:
        entries.append((blank, total - t))
    lst = out_dir / "chapters.txt"
    body = "ffconcat version 1.0\n" + "".join(f"file '{p.name}'\nduration {d:.3f}\n" for p, d in entries)
    lst.write_text(body + f"file '{entries[-1][0].name}'\n", encoding="utf-8")
    return lst
