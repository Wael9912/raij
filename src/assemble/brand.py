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
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

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


def _title_block(title: str, series: str | None) -> Image.Image:
    size = 118
    while size > TITLE_MIN_SIZE and len(wrap(title, size, 960)) > 2:   # shrink to fit two lines, never drop words
        size -= 6
    wrapped = wrap(title, size, 960)
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
                  fps: int = 30, center_y: int = 820) -> Path:
    """The hook title popping in at the start and fading out by `seconds`; returns an ffconcat list.
    The stream ends there and the overlay passes the video through (eof_action=pass)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    block = _title_block(title, series)
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
    entries = [(frame(f"in{i}", s, a), step) for i, (s, a) in enumerate(pop)]
    hold = seconds - step * (len(pop) + len(fade))
    if hold > 0:
        entries.append((frame("hold", 1.0, 1.0), hold))
    entries += [(frame(f"out{i}", 1.0, a), step) for i, a in enumerate(fade)]
    entries.append((blank, step))

    lst = out_dir / "hook.txt"
    body = "ffconcat version 1.0\n" + "".join(f"file '{p.name}'\nduration {d:.3f}\n" for p, d in entries)
    lst.write_text(body + f"file '{entries[-1][0].name}'\n", encoding="utf-8")
    return lst


def endcard(brand: dict[str, Any], out: Path, series: str | None = None) -> Path:
    """Closing card: wordmark, series badge, follow prompt."""
    img = Image.new("RGBA", (W, H), INK)
    parts = [text_image(brand.get("name") or brand["id"], 260, fill=YELLOW)]
    if series:
        parts.append(pill(series, 64))
    parts.append(text_image("تابعنا للمزيد", 88, fill=WHITE))
    gap = 70
    y = (H - sum(p.height for p in parts) - gap * (len(parts) - 1)) // 2 - 60
    for p in parts:
        img.alpha_composite(p, ((W - p.width) // 2, y))
        y += p.height + gap
    img.convert("RGB").save(out)
    return out
