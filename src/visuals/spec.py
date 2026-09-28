"""Validate the `visual` a script model attaches to a beat. A bad visual is dropped (the beat falls back to stock
footage), never a reason to reject the script.

  {"type": "map", "title": "…", "focus": ["IRN"], "compare": ["SAU"], "markers": ["hormuz"], "route": "gulf_asia"}
  {"type": "chart", "kind": "bar"|"line", "title": "…", "indicator": "NY.GDP.PCAP.CD", "countries": ["SAU", "ARE"],
   "years": [2010, 2024]}                                              ← World Bank (preferred)
  {"type": "chart", "kind": "bar"|"line", "title": "…", "unit": "…", "source": "…",
   "points": [{"label": "2020", "value": 1770}, …]}                    ← figures the story card states
  {"type": "stat", "title": "…", "value": 2400, "unit": "دولار", "label": "…", "source": "…"}

Card figures (chart points, stat values) must match a number on the story card within honest rounding — the same
rule the script's own figures pass (src/script/facts.py) — or the visual is dropped: a chart may not invent data.
"""
from __future__ import annotations

import re
import time
from typing import Any

from src.script import facts

_ARABIC_LETTER = re.compile(r"[ء-ي]")
_ISO3 = re.compile(r"^[A-Z]{3}$")


def _title(value: Any, max_words: int = 8) -> str | None:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    if not s or not _ARABIC_LETTER.search(s) or len(s.split()) > max_words:
        return None
    return s


def _short(value: Any, limit: int = 40) -> str | None:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return s[:limit] if s else None


def _num(value: Any) -> float | None:
    try:
        v = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) < 1e15 else None


def _isos(values: Any, limit: int) -> list[str]:
    from src.visuals import geo
    known = geo.countries()
    out = [str(v).upper() for v in values or [] if _ISO3.match(str(v).upper()) and str(v).upper() in known]
    return list(dict.fromkeys(out))[:limit]


def clean(raw: Any) -> dict[str, Any] | None:
    """Shape check only (known countries/places/indicators, sane fields). None = no usable visual."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").lower()
    title = _title(raw.get("title"))
    if kind == "map":
        from src.visuals import geo
        spec = {"type": "map", "title": title, "focus": _isos(raw.get("focus"), 6),
                "compare": [c for c in _isos(raw.get("compare"), 8) if c not in _isos(raw.get("focus"), 6)],
                "markers": [m for m in dict.fromkeys(str(x) for x in raw.get("markers") or []) if m in geo.places()][:4],
                "route": str(raw.get("route")) if str(raw.get("route") or "") in geo.routes() else None}
        return spec if (spec["focus"] or spec["compare"] or spec["markers"] or spec["route"]) else None
    if kind == "chart":
        from src.visuals import worldbank
        chart_kind = str(raw.get("kind") or "bar").lower()
        if chart_kind not in ("bar", "line"):
            return None
        if raw.get("indicator"):
            ind = str(raw["indicator"]).strip()
            countries = _isos(raw.get("countries"), 8 if chart_kind == "bar" else 4)
            if ind not in worldbank.CATALOG or not countries:
                return None
            now = time.gmtime().tm_year
            years = [int(y) for y in raw.get("years") or [] if _num(y) is not None][:2]
            if len(years) != 2 or not 1960 <= years[0] < years[1] <= now:
                years = [now - 20 if chart_kind == "line" else now - 6, now]
            if chart_kind == "bar" and len(countries) < 2:
                return None
            return {"type": "chart", "kind": chart_kind, "title": title, "indicator": ind, "countries": countries,
                    "years": years}
        points = []
        for p in raw.get("points") or []:
            if isinstance(p, dict) and _short(p.get("label"), 30) and _num(p.get("value")) is not None:
                points.append({"label": _short(p["label"], 30), "value": _num(p["value"])})
        if not 2 <= len(points) <= 8:
            return None
        return {"type": "chart", "kind": chart_kind, "title": title, "points": points,
                "unit": _short(raw.get("unit"), 20), "source": _short(raw.get("source"))}
    if kind == "stat":
        value = _num(raw.get("value"))
        label = _title(raw.get("label"), max_words=10)
        if value is None or not label:
            return None
        return {"type": "stat", "title": title, "value": value, "unit": _short(raw.get("unit"), 20), "label": label,
                "source": _short(raw.get("source"))}
    return None


def _scaled(value: float, unit: str | None) -> float:
    """8 with unit "مليون دولار" is 8,000,000 — compared at its real size, else a bare 8 matches any 8 on the card."""
    if unit:
        found = facts.numbers(f"{value:g} {unit}")
        if found:
            return found[0]
    return value


def card_figures(spec: dict[str, Any]) -> list[float]:
    """The figures in a visual that must come from the story card (World Bank series come from the API)."""
    if spec["type"] == "stat":
        return [_scaled(spec["value"], spec.get("unit"))]
    if spec["type"] == "chart" and "points" in spec:
        return [_scaled(p["value"], spec.get("unit")) for p in spec["points"]]
    return []


def check(spec: dict[str, Any] | None, story: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """(spec, None) when every card figure is on the card; (None, reason) otherwise."""
    if not spec:
        return None, None
    known = facts.numbers(facts.card_text(story))
    bad = [f"{n:g}" for n in card_figures(spec) if not any(facts.supported(n, m) for m in known)]
    if bad:
        return None, f"{spec['type']} figures not on the card: {', '.join(bad)}"
    return spec, None
