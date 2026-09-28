"""Draw the خريطة المال brand kit into assets/brand/ (Phase 21): logo.png (video watermark), avatar.png (800²),
banner_youtube.png (2560×1440, text inside YouTube's 1546×423 safe area) and cover_facebook.png.

    uv run python tools/make_brand.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import textshape  # noqa: E402

textshape.ensure()
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter  # noqa: E402

from src.config import ROOT, load_config  # noqa: E402
from src.visuals import canvas, mapviz  # noqa: E402
from src.visuals.canvas import GOLD, OCEAN, WHITE  # noqa: E402

OUT = ROOT / "assets" / "brand"
GULF = {"focus": ["SAU", "ARE", "QAT", "KWT", "BHR", "OMN"], "compare": ["EGY", "IRQ", "IRN"],
        "route": "gulf_europe_suez"}


def glyph(size: int) -> Image.Image:
    """A folded map (three panels) with a gold route and a pin: the channel's mark."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 100
    panels = [[(10, 22), (37, 12), (37, 80), (10, 90)], [(37, 12), (63, 22), (63, 90), (37, 80)],
              [(63, 22), (90, 12), (90, 80), (63, 90)]]
    shades = [(255, 196, 0), (214, 160, 0), (255, 212, 60)]
    for poly, fill in zip(panels, shades):
        d.polygon([(x * s, y * s) for x, y in poly], fill=fill)
    route = [(18, 70), (30, 58), (44, 62), (56, 46), (70, 50), (80, 34)]
    d.line([(x * s, y * s) for x, y in route], fill=OCEAN, width=max(2, int(5 * s)), joint="curve")
    cx, cy, r = 80 * s, 34 * s, 7 * s
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WHITE, outline=OCEAN, width=max(2, int(3 * s)))
    return img


def wordmark(height: int) -> Image.Image:
    name = load_config().brands[0]["name"]
    size = int(height * 0.8)
    w = canvas.text_width(name, size) + 20
    img = Image.new("RGBA", (w, height), (0, 0, 0, 0))
    canvas.text(img, (w - 10, height / 2), name, size, fill=WHITE, anchor="rm")
    return img


def logo(height: int = 200) -> Image.Image:
    g = glyph(height)
    wm = wordmark(int(height * 0.62))
    pad = int(height * 0.14)
    img = Image.new("RGBA", (g.width + wm.width + 3 * pad, height + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=int(height * 0.3),
                                          fill=(9, 20, 34, 215))
    img.alpha_composite(wm, (pad, pad + (height - wm.height) // 2))       # Arabic reads right to left: mark right
    img.alpha_composite(g, (wm.width + 2 * pad, pad))
    return img


def backdrop(size: tuple[int, int], dim: float = 0.45) -> Image.Image:
    base = mapviz.still(GULF, size)
    base = ImageEnhance.Brightness(base).enhance(dim).filter(ImageFilter.GaussianBlur(1.2))
    return base.convert("RGBA")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    logo().save(OUT / "logo.png", optimize=True)

    av = backdrop((800, 800), dim=0.35)
    g = glyph(520)
    av.alpha_composite(g, (140, 120))
    av.convert("RGB").save(OUT / "avatar.png", optimize=True)

    for name, size, safe_h in (("banner_youtube.png", (2560, 1440), 423), ("cover_facebook.png", (1640, 624), 360)):
        img = backdrop(size)
        W, H = size
        scale = safe_h / 423
        g = glyph(int(300 * scale))
        wm = wordmark(int(170 * scale))
        total = g.width + wm.width + int(40 * scale)
        x = (W - total) // 2
        y = H // 2 - int(150 * scale)
        img.alpha_composite(wm, (x, y + (g.height - wm.height) // 2))
        img.alpha_composite(g, (x + wm.width + int(40 * scale), y))
        tag = "الاقتصاد على الخريطة: النفط، الذهب، طرق التجارة، ولماذا تغتني الدول"
        canvas.text(img, (W / 2, y + g.height + int(28 * scale)), tag,
                    canvas.fit_size(tag, int(54 * scale), int(1500 * scale)), fill=WHITE, anchor="mt")
        canvas.text(img, (W / 2, y + g.height + int(100 * scale)), "حلقة جديدة كل اثنين وخميس",
                    int(44 * scale), fill=GOLD, anchor="mt")
        img.convert("RGB").save(OUT / name, optimize=True)
    print("brand kit →", OUT)


if __name__ == "__main__":
    main()
