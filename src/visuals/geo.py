"""Map data: Natural Earth 1:50m countries (public domain; `tools/fetch_geo.py` → assets/geo/) and the curated
places / sea lanes in places.json."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.config import ROOT

GEO_DIR = ROOT / "assets" / "geo"
COUNTRIES = GEO_DIR / "ne_50m_admin_0_countries.geojson"
PLACES = Path(__file__).with_name("places.json")

Ring = list[tuple[float, float]]


class GeoMissing(RuntimeError):
    """assets/geo has no country file — run `uv run python tools/fetch_geo.py`."""


@dataclass(frozen=True)
class Country:
    iso: str
    name_ar: str
    rings: tuple[tuple[tuple[float, float], ...], ...]      # outer rings only, lon/lat
    main: tuple[float, float, float, float]                 # bbox of the largest polygon (no overseas bits)
    label: tuple[float, float]


def _area(ring: Ring) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]))) / 2


def _bbox(ring: Ring) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


@lru_cache(maxsize=1)
def countries() -> dict[str, Country]:
    if not COUNTRIES.exists():
        raise GeoMissing(f"{COUNTRIES.relative_to(ROOT)} is missing — run `uv run python tools/fetch_geo.py`")
    data = json.loads(COUNTRIES.read_text(encoding="utf-8"))
    out: dict[str, Country] = {}
    for f in data["features"]:
        p = f["properties"]
        iso = p.get("ISO_A3") if p.get("ISO_A3") not in (None, "-99") else p.get("ADM0_A3")
        g = f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        rings = [tuple((float(x), float(y)) for x, y in poly[0]) for poly in polys]
        biggest = max(rings, key=lambda r: _area(list(r)))
        out[str(iso)] = Country(str(iso), str(p.get("NAME_AR") or p.get("NAME")), tuple(rings),
                                _bbox(list(biggest)),
                                (float(p.get("LABEL_X") or 0), float(p.get("LABEL_Y") or 0)))
    return out


@lru_cache(maxsize=1)
def _places_doc() -> dict:
    return json.loads(PLACES.read_text(encoding="utf-8"))


def places() -> dict[str, dict]:
    return _places_doc()["places"]


def routes() -> dict[str, dict]:
    return _places_doc()["routes"]


def route_points(route_id: str) -> Ring:
    """The route's waypoints, lanes chained (a '-lane' runs backwards), joints not doubled."""
    doc = _places_doc()
    pts: Ring = []
    for lane in doc["routes"][route_id]["lanes"]:
        seq = doc["lanes"][lane.lstrip("-")]
        seq = list(reversed(seq)) if lane.startswith("-") else list(seq)
        for x, y in seq:
            if not pts or (abs(pts[-1][0] - x) > 1e-6 or abs(pts[-1][1] - y) > 1e-6):
                pts.append((float(x), float(y)))
    return pts


def densify(points: Ring, step: float = 0.5) -> Ring:
    """Extra points every `step` degrees so a route can be drawn part-way smoothly."""
    out: Ring = []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        out += [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n)]
    if points:
        out.append(points[-1])
    return out
