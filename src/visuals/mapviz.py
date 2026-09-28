"""Animated map: the camera eases from a wide view onto the focus countries, they light up (gold; `compare`
countries in teal), labels fade in, markers pulse and a sea route draws itself.

Two basemaps (plain and highlighted) are drawn once, big enough for the closest view, and every frame is a crop
of them resized to the output — so a 20-second map costs two polygon passes, not 600. Projection: plate carrée
with longitudes squeezed by cos(latitude of the focus), which reads right at regional scale.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterator

from PIL import Image, ImageDraw

from src.visuals import canvas, geo
from src.visuals.canvas import BORDER, GOLD, LAND, OCEAN, TEAL, WHITE

MAX_BASE_PX = 7200            # longest side of a basemap (two of them in memory: ~150 MB)
ZOOM_FROM = 3.2               # the opening view is this many times wider than the final one
MAX_END_SPAN = 75.0          # degrees of latitude at most in the final view (else the route runs off-screen)
MIN_SPAN = 9.0                # degrees of latitude at least in the final view (tiny countries stay in context)


class View:
    def __init__(self, cx: float, cy: float, w: float, h: float):
        self.cx, self.cy, self.w, self.h = cx, cy, w, h


def _fit(points: list[tuple[float, float]], k: float, aspect: float, pad: float = 0.28) -> View:
    xs = [x * k for x, _ in points]
    ys = [-y for _, y in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = max(x1 - x0, 1e-3) * (1 + 2 * pad), max(y1 - y0, 1e-3) * (1 + 2 * pad)
    h = max(h, MIN_SPAN)
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    return View((x0 + x1) / 2, (y0 + y1) / 2, w, h)


def plan(spec: dict[str, Any], size: tuple[int, int]) -> dict[str, Any]:
    """Resolve the spec against the geo data: what to highlight, where the camera ends, what to draw on top."""
    cs = geo.countries()
    focus = [cs[i] for i in spec.get("focus", []) if i in cs]
    compare = [cs[i] for i in spec.get("compare", []) if i in cs and i not in spec.get("focus", [])]
    pl = geo.places()
    markers = [dict(pl[m], id=m) for m in spec.get("markers", []) if m in pl]
    route = geo.route_points(spec["route"]) if spec.get("route") in geo.routes() else []
    pts: list[tuple[float, float]] = []
    for c in focus + compare:
        x0, y0, x1, y1 = c.main
        pts += [(x0, y0), (x1, y1)]
    pts += [(m["lon"], m["lat"]) for m in markers] + route
    if not pts:
        raise ValueError("map has nothing to show (no known countries, markers or route)")
    lat0 = sum(y for _, y in pts) / len(pts)
    k = math.cos(math.radians(max(min(lat0, 60.0), -60.0)))
    aspect = size[0] / size[1]
    end = _fit(pts, k, aspect)
    span_cap = MAX_END_SPAN if aspect < 1 else MAX_END_SPAN * 1.6    # landscape can show a long route whole
    if route and (end.h > span_cap or end.w > 250 * k):
        # A long route in a tall frame would zoom out to mostly ocean: frame the countries and markers, and let
        # the route run off-screen.
        near = [q for q in pts if q not in route] or route[:2] + route[-2:]
        end = _fit(near, k, aspect)
    # The opening view: wider by ZOOM_FROM, but never past the edges of the world.
    start_h = min(end.h * ZOOM_FROM, 150.0, 360 * k / aspect)
    start = View(end.cx, end.cy, start_h * aspect, start_h)
    if end.w >= start.w:
        start = View(end.cx, end.cy, end.w, end.h)
    half_w, half_h = start.w / 2, start.h / 2
    start.cx = min(max(start.cx, -180 * k + half_w), 180 * k - half_w) if half_w < 180 * k else 0.0
    start.cy = min(max(start.cy, -85 + half_h), 85 - half_h) if half_h < 85 else 0.0
    return {"focus": focus, "compare": compare, "markers": markers, "route": geo.densify(route), "k": k,
            "start": start, "end": end}


def _basemaps(p: dict[str, Any], size: tuple[int, int]) -> tuple[Image.Image, Image.Image, tuple[float, float, float]]:
    """(plain, highlighted, (x0, y0, ppu)): both cover the opening view, at ≥ output resolution for the final
    one."""
    start, end, k = p["start"], p["end"], p["k"]
    ppu = size[0] * 1.25 / end.w
    mw, mh = start.w * 1.04, start.h * 1.04                  # a margin, so float rounding never crops outside
    ppu = min(ppu, MAX_BASE_PX / max(mw, mh))
    bw, bh = int(math.ceil(mw * ppu)), int(math.ceil(mh * ppu))
    x0, y0 = start.cx - mw / 2, start.cy - mh / 2
    plain = Image.new("RGB", (bw, bh), OCEAN)
    d = ImageDraw.Draw(plain)
    # Graticule every 10°: a quiet "this is a map" texture.
    grid = tuple(min(c + 10, 255) for c in OCEAN)
    for lon in range(-180, 181, 10):
        X = (lon * k - x0) * ppu
        if 0 <= X <= bw:
            d.line([(X, 0), (X, bh)], fill=grid, width=1)
    for lat in range(-80, 81, 10):
        Y = (-lat - y0) * ppu
        if 0 <= Y <= bh:
            d.line([(0, Y), (bw, Y)], fill=grid, width=1)
    lon_lo, lon_hi = x0 / k - 5, (x0 + start.w) / k + 5
    lat_hi, lat_lo = -y0 + 5, -(y0 + start.h) - 5
    hl = {c.iso: GOLD for c in p["focus"]} | {c.iso: TEAL for c in p["compare"]}
    border_w = max(1, int(round(ppu * 0.06)))

    def draw(img: Image.Image, highlight: bool) -> None:
        dr = ImageDraw.Draw(img)
        for c in geo.countries().values():
            for ring in c.rings:
                xs = [x for x, _ in ring]
                ys = [y for _, y in ring]
                if max(xs) < lon_lo or min(xs) > lon_hi or max(ys) < lat_lo or min(ys) > lat_hi:
                    continue
                pix = [((x * k - x0) * ppu, (-y - y0) * ppu) for x, y in ring]
                fill = hl.get(c.iso, LAND) if highlight else LAND
                dr.polygon(pix, fill=fill)
                dr.line(pix + pix[:1], fill=BORDER if fill == LAND else OCEAN, width=border_w)

    draw(plain, False)
    lit = plain.copy() if not hl else Image.new("RGB", (bw, bh))
    if hl:
        lit.paste(plain)
        draw(lit, True)
    return plain, lit, (x0, y0, ppu)


def _clamp(box: tuple[float, float, float, float], size: tuple[int, int]) -> tuple[float, float, float, float]:
    """Shift a crop box back inside the basemap (Pillow refuses boxes that stick out)."""
    l, t, r, b = box
    dx = max(0.0, -l) - max(0.0, r - size[0])
    dy = max(0.0, -t) - max(0.0, b - size[1])
    l, r, t, b = l + dx, r + dx, t + dy, b + dy
    return max(l, 0.0), max(t, 0.0), min(r, float(size[0])), min(b, float(size[1]))


def _view_at(p: dict[str, Any], t: float, fly_end: float, seconds: float) -> View:
    s, e = p["start"], p["end"]
    q = canvas.ease(canvas.phase(t, 0.15, fly_end))
    w = s.w * (e.w / s.w) ** q                            # zoom in log space: steady feel
    h = s.h * (e.h / s.h) ** q
    push = 1 - 0.05 * canvas.phase(t, fly_end, seconds)  # slow push-in afterwards so it never freezes
    return View(s.cx + (e.cx - s.cx) * q, s.cy + (e.cy - s.cy) * q, w * push, h * push)


def frames(spec: dict[str, Any], seconds: float, size: tuple[int, int]) -> Iterator[Image.Image]:
    p = plan(spec, size)
    plain, lit, (x0, y0, ppu) = _basemaps(p, size)
    W, H = size
    scale = min(W, H) / 1080
    k = p["k"]
    n = canvas.frame_count(seconds)
    fly_end = min(3.2, max(1.2, seconds * 0.4))
    hl_at = fly_end - 0.2
    route_from, route_to = fly_end + 0.2, min(seconds - 0.3, fly_end + 0.2 + max(2.0, seconds * 0.45))
    labels = sorted([(c.name_ar, c.label, GOLD, (c.main[2] - c.main[0]) * k) for c in p["focus"]]
                    + [(c.name_ar, c.label, TEAL, (c.main[2] - c.main[0]) * k) for c in p["compare"]],
                    key=lambda x: -x[3])
    for i in range(n):
        t = i / canvas.FPS
        v = _view_at(p, t, fly_end, seconds)
        box = _clamp(((v.cx - v.w / 2 - x0) * ppu, (v.cy - v.h / 2 - y0) * ppu,
                      (v.cx + v.w / 2 - x0) * ppu, (v.cy + v.h / 2 - y0) * ppu), plain.size)
        a = canvas.ease(canvas.phase(t, hl_at, hl_at + 0.9))
        base = plain if a <= 0 else lit
        frame = base.resize(size, Image.Resampling.BILINEAR, box=box, reducing_gap=2.0)
        if 0 < a < 1:
            frame = Image.blend(plain.resize(size, Image.Resampling.BILINEAR, box=box, reducing_gap=2.0), frame, a)

        def to_px(lon: float, lat: float) -> tuple[float, float]:
            return ((lon * k - (v.cx - v.w / 2)) / v.w * W, (-lat - (v.cy - v.h / 2)) / v.h * H)

        # Route: drawn part-way, a bright head leading.
        rp = canvas.ease(canvas.phase(t, route_from, route_to))
        if p["route"] and rp > 0:
            pts = p["route"][: max(2, int(len(p["route"]) * rp))]
            pix = [to_px(x, y) for x, y in pts]
            d = ImageDraw.Draw(frame)
            d.line(pix, fill=(0, 0, 0), width=int(11 * scale), joint="curve")
            d.line(pix, fill=GOLD, width=int(6 * scale), joint="curve")
            hx, hy = pix[-1]
            r = 10 * scale
            d.ellipse((hx - r, hy - r, hx + r, hy + r), fill=WHITE, outline=GOLD, width=int(3 * scale))
        la = canvas.ease(canvas.phase(t, hl_at + 0.3, hl_at + 1.1))
        if la > 0:
            placed = canvas.Labels([(W * 0.45, 0, W, 230 * scale), (W * 0.5, H - 80 * scale, W, H)])
            d = ImageDraw.Draw(frame)
            # Markers first (they are the story), then countries biggest first; a label that would collide is
            # skipped, and a country too small on screen for its name goes unlabelled.
            for m in p["markers"]:
                X, Y = to_px(m["lon"], m["lat"])
                if not (0 < X < W and 0 < Y < H):
                    continue
                pulse = (t * 1.2) % 1.0
                R = (14 + 26 * pulse) * scale
                d.ellipse((X - R, Y - R, X + R, Y + R), outline=tuple(int(c * (1 - pulse)) for c in GOLD),
                          width=int(3 * scale))
                r = 11 * scale
                d.ellipse((X - r, Y - r, X + r, Y + r), fill=GOLD, outline=(0, 0, 0), width=int(3 * scale))
                size_px = int(30 * scale)
                for anchor, at in (("rm", (X - 22 * scale, Y)), ("lm", (X + 22 * scale, Y)),
                                   ("mb", (X, Y - 22 * scale)), ("mt", (X, Y + 22 * scale))):
                    if placed.free(canvas.text_box(at, m["ar"], size_px, anchor=anchor, stroke=int(4 * scale))):
                        canvas.text(frame, at, m["ar"], size_px, fill=GOLD, anchor=anchor, alpha=la,
                                    stroke=int(4 * scale), stroke_fill=(0, 0, 0))
                        break
            for name, (lx, ly), color, width in labels:
                X, Y = to_px(lx, ly)
                size_px = int(34 * scale)
                on_screen = width / v.w * W
                if not (0 < X < W and 0 < Y < H) or on_screen < canvas.text_width(name, size_px) * 0.55:
                    continue
                box = canvas.text_box((X, Y), name, size_px, anchor="mm", stroke=int(4 * scale))
                if placed.free(box):
                    canvas.text(frame, (X, Y), name, size_px, fill=WHITE, anchor="mm", alpha=la,
                                stroke=int(4 * scale), stroke_fill=(0, 0, 0))
        canvas.title_card(frame, spec.get("title"), scale, alpha=canvas.ease(canvas.phase(t, 0.2, 0.9)))
        canvas.notes(frame, [None if not spec.get("title") else "", "المصدر: خرائط Natural Earth"][1:], scale)
        yield frame


def render(spec: dict[str, Any], seconds: float, size: tuple[int, int], out: Path, sink: canvas.FrameSink) -> Path:
    return sink(frames(spec, seconds, size), out, size)


def still(spec: dict[str, Any], size: tuple[int, int], route: bool = True) -> Image.Image:
    """The final view of a map spec as one picture (highlight on, route fully drawn, no text) — for brand art
    and thumbnails."""
    p = plan(spec, size)
    _, lit, (x0, y0, ppu) = _basemaps(p, size)
    v = p["end"]
    box = _clamp(((v.cx - v.w / 2 - x0) * ppu, (v.cy - v.h / 2 - y0) * ppu,
                  (v.cx + v.w / 2 - x0) * ppu, (v.cy + v.h / 2 - y0) * ppu), lit.size)
    img = lit.resize(size, Image.Resampling.LANCZOS, box=box)
    if route and p["route"]:
        W, H = size
        scale = min(W, H) / 1080
        pix = [((x * p["k"] - (v.cx - v.w / 2)) / v.w * W, (-y - (v.cy - v.h / 2)) / v.h * H) for x, y in p["route"]]
        d = ImageDraw.Draw(img)
        d.line(pix, fill=(0, 0, 0), width=int(11 * scale), joint="curve")
        d.line(pix, fill=GOLD, width=int(6 * scale), joint="curve")
    return img
