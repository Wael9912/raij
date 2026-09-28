"""Animated data charts and stat cards, drawn right-to-left where Arabic readers expect it.

bar  — horizontal bars, labels on the right, bars growing leftwards, values counting up; the first item (the
       story's subject) in gold, the rest in blue.
line — years run left to right (as Arabic news graphics do), value axis on the right; lines draw themselves and
       end on a labelled dot.
stat — one figure counting up, big, with its label.
Every frame prints its source ("المصدر: …").
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterator

from PIL import Image, ImageDraw

from src.visuals import canvas
from src.visuals.canvas import GOLD, GRID, MUTED, OCEAN, PANEL, SERIES_COLORS, WHITE

OTHER = (70, 118, 168)
BUILD = 2.4                     # seconds for the chart to build


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.floor(lo / step) * step
    ticks = []
    v = first
    while v <= hi + step * 0.5:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _background(size: tuple[int, int]) -> Image.Image:
    """Navy with a soft top-to-bottom gradient."""
    W, H = size
    grad = Image.linear_gradient("L").resize((W, H))
    dark = Image.new("RGB", size, OCEAN)
    light = Image.new("RGB", size, PANEL)
    return Image.composite(dark, light, grad)


def _describe(data: dict[str, Any]) -> str | None:
    """'نصيب الفرد من الناتج المحلي (دولار، 2024)'."""
    label = data.get("label") or ""
    unit, note = data.get("unit"), data.get("note")
    tail = "، ".join(x for x in ((unit if unit != "%" else "%") if unit else None, note) if x)
    return f"{label} ({tail})" if label and tail else (label or None)


def _notes(img: Image.Image, data: dict[str, Any], scale: float) -> float:
    src = data.get("source")
    return canvas.notes(img, [_describe(data), f"المصدر: {src}" if src else None], scale)


def bar_frames(data: dict[str, Any], title: str | None, seconds: float, size: tuple[int, int]) -> Iterator[Image.Image]:
    W, H = size
    scale = min(W, H) / 1080
    pts = data["points"][:8]
    if data.get("sort", True):
        pts = [pts[0]] + sorted(pts[1:], key=lambda p: -p["value"]) if pts else pts
    unit = data.get("unit")
    bg = _background(size)
    top, bottom, side = 340 * scale, canvas.safe_bottom(size), 72 * scale
    label_size = int(34 * scale)
    label_w = max(canvas.text_width(p["label"], label_size) for p in pts) + 28 * scale
    value_w = max(canvas.text_width(canvas.number(p["value"], unit), int(32 * scale)) for p in pts) + 36 * scale
    right = W - side - label_w                     # bars start here…
    left = side + value_w                          # …and may reach here
    vmin, vmax = min(0.0, min(p["value"] for p in pts)), max(0.0, max(p["value"] for p in pts))
    span = (vmax - vmin) or 1.0

    def x_of(v: float) -> float:                   # bigger → further left (RTL)
        return right - (v - vmin) / span * (right - left)

    zero = x_of(0.0)
    row = (bottom - top) / max(len(pts), 1)
    bar_h = min(row * 0.62, 90 * scale)
    for i in range(canvas.frame_count(seconds)):
        t = i / canvas.FPS
        img = bg.copy()
        canvas.title_card(img, title, scale, alpha=canvas.ease(canvas.phase(t, 0.0, 0.6)))
        _notes(img, data, scale)
        d = ImageDraw.Draw(img)
        d.line([(zero, top - 10 * scale), (zero, bottom)], fill=GRID, width=max(1, int(2 * scale)))
        for k, p in enumerate(pts):
            g = canvas.ease_out(canvas.phase(t, 0.3 + k * 0.12, 0.3 + k * 0.12 + BUILD * 0.6))
            cy = top + row * (k + 0.5)
            canvas.text(img, (W - side, cy), p["label"], label_size, fill=WHITE if k else GOLD, anchor="rm",
                        alpha=min(1.0, g * 3) if g > 0 else 0.0)
            if g <= 0:
                continue
            v = p["value"] * g
            x = x_of(v)
            color = GOLD if k == 0 else OTHER
            d.rounded_rectangle((min(x, zero), cy - bar_h / 2, max(x, zero), cy + bar_h / 2),
                                radius=int(8 * scale), fill=color)
            label = canvas.number(v if g < 1 else p["value"], unit)
            canvas.text(img, (min(x, zero) - 14 * scale, cy), label, int(32 * scale), fill=WHITE, anchor="rm")
        yield img


def line_frames(data: dict[str, Any], title: str | None, seconds: float, size: tuple[int, int]) -> Iterator[Image.Image]:
    W, H = size
    scale = min(W, H) / 1080
    lines = data["lines"][:4]
    unit = data.get("unit")
    xs = sorted({x for ln in lines for x, _ in ln["points"]})
    vals = [v for ln in lines for _, v in ln["points"]]
    ticks = nice_ticks(min(0.0, min(vals)) if min(vals) >= 0 and min(vals) < max(vals) * 0.3 else min(vals),
                       max(vals))
    lo, hi = ticks[0], ticks[-1]
    bg = _background(size)
    tick_w = max(canvas.text_width(canvas.number(tv, None if unit != "%" else "%"), int(28 * scale)) for tv in ticks)
    top, bottom = 390 * scale, canvas.safe_bottom(size) - 44 * scale
    left, right = 90 * scale, W - 90 * scale - tick_w
    x_lo, x_hi = xs[0], xs[-1]

    def px(x: float, v: float) -> tuple[float, float]:
        fx = (x - x_lo) / ((x_hi - x_lo) or 1)
        fy = (v - lo) / ((hi - lo) or 1)
        return left + fx * (right - left), bottom - fy * (bottom - top)

    step = max(1, math.ceil(len(xs) / (6 if W > H else 4)))
    for i in range(canvas.frame_count(seconds)):
        t = i / canvas.FPS
        img = bg.copy()
        canvas.title_card(img, title, scale, alpha=canvas.ease(canvas.phase(t, 0.0, 0.6)))
        below = _notes(img, data, scale)
        d = ImageDraw.Draw(img)
        for tv in ticks:
            _, y = px(x_lo, tv)
            d.line([(left, y), (right, y)], fill=GRID, width=1)
            canvas.text(img, (W - 80 * scale, y), canvas.number(tv, "%" if unit == "%" else None), int(28 * scale),
                        fill=MUTED, anchor="rm", weight=600)
        for x in xs[::step] + ([xs[-1]] if (len(xs) - 1) % step else []):
            X, _ = px(x, lo)
            xl = data.get("x_labels")
            canvas.text(img, (X, bottom + 18 * scale), str(xl[int(x)]) if xl else str(x), int(28 * scale),
                        fill=MUTED, anchor="ma", weight=600)
        # Legend, top right under the subtitle.
        lx = right - 16 * scale                    # legend clear of the value axis
        if len(lines) > 1:
            for k, ln in enumerate(lines):
                color = SERIES_COLORS[k % len(SERIES_COLORS)]
                box = canvas.text(img, (lx, below + 4 * scale), ln["label"], int(28 * scale), fill=color, anchor="ra")
                lx = box[0] - 40 * scale
        g = canvas.ease(canvas.phase(t, 0.3, 0.3 + BUILD))
        placed = canvas.Labels()
        x_now = x_lo + (x_hi - x_lo) * g
        for k, ln in enumerate(lines):
            color = SERIES_COLORS[k % len(SERIES_COLORS)]
            pts = [(x, v) for x, v in ln["points"] if x <= x_now]
            nxt = next(((x, v) for x, v in ln["points"] if x > x_now), None)
            if pts and nxt:                       # the head moves smoothly between data points
                (x0, v0), (x1, v1) = pts[-1], nxt
                f = (x_now - x0) / ((x1 - x0) or 1)
                pts = pts + [(x_now, v0 + (v1 - v0) * f)]
            if len(pts) < 2:
                continue
            pix = [px(x, v) for x, v in pts]
            d.line(pix, fill=color, width=int(7 * scale), joint="curve")
            hx, hy = pix[-1]
            r = 10 * scale
            d.ellipse((hx - r, hy - r, hx + r, hy + r), fill=color, outline=WHITE, width=int(3 * scale))
            if g >= 1:
                label = canvas.number(pts[-1][1], unit)
                for anchor, at in (("rb", (hx - 18 * scale, hy - 14 * scale)), ("rt", (hx - 18 * scale, hy + 14 * scale)),
                                   ("rm", (hx - 22 * scale, hy))):
                    if placed.free(canvas.text_box(at, label, int(30 * scale), anchor=anchor)):
                        canvas.text(img, at, label, int(30 * scale), fill=color, anchor=anchor,
                                    stroke=int(3 * scale), stroke_fill=OCEAN)
                        break
        yield img


def stat_frames(data: dict[str, Any], title: str | None, seconds: float, size: tuple[int, int]) -> Iterator[Image.Image]:
    W, H = size
    scale = min(W, H) / 1080
    bg = _background(size)
    value, unit = float(data["value"]), data.get("unit")
    final = canvas.number(value, unit)
    big = canvas.fit_size(final, int(190 * scale), int(W * 0.84))
    for i in range(canvas.frame_count(seconds)):
        t = i / canvas.FPS
        img = bg.copy()
        canvas.title_card(img, title, scale, alpha=canvas.ease(canvas.phase(t, 0.0, 0.6)))
        g = canvas.ease_out(canvas.phase(t, 0.2, 0.2 + BUILD * 0.8))
        shown = final if g >= 1 else canvas.number(value * g, unit)
        cy = min(H * 0.47, canvas.safe_bottom(size) - big * 1.4)
        canvas.text(img, (W / 2, cy), shown, big, fill=GOLD, anchor="mm")
        if data.get("label"):
            size_px = canvas.fit_size(data["label"], int(54 * scale), int(W * 0.84))
            canvas.text(img, (W / 2, cy + big * 0.75), data["label"], size_px, fill=WHITE, anchor="mt",
                        alpha=canvas.ease(canvas.phase(t, 0.6, 1.2)))
        src = data.get("source")
        canvas.notes(img, [f"المصدر: {src}" if src else None], scale)
        yield img


def render(kind: str, data: dict[str, Any], title: str | None, seconds: float, size: tuple[int, int], out: Path,
           sink: canvas.FrameSink) -> Path:
    make = {"bar": bar_frames, "line": line_frames, "stat": stat_frames}[kind]
    return sink(make(data, title, seconds, size), out, size)
