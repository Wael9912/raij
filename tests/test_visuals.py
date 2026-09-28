"""Phase 21: the channel's own maps, charts and stat cards."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.config import load_config
from src.script import write
from src.visuals import canvas, chart, geo, mapviz, render as vrender, spec, worldbank

STORY = {"id": 1, "hook": "الذهب عند 2400 دولار", "key_facts": json.dumps(["Gold hit 2,400 dollars an ounce",
                                                                          "Central banks bought 1,037 tonnes"]),
         "claims": "[]", "why_trending": "record"}


class Frames:
    """A sink that keeps frame count + the last frame instead of encoding."""

    def __init__(self):
        self.count, self.last, self.size = 0, None, None

    def __call__(self, frames, out: Path, size):
        for f in frames:
            self.count += 1
            self.last = f
        self.size = size
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return out


# --- spec -----------------------------------------------------------------------------

def test_clean_keeps_known_countries_places_routes_and_drops_the_rest():
    s = spec.clean({"type": "map", "title": "مضيق هرمز", "focus": ["irn", "XXX", "IRN"], "compare": ["SAU"],
                    "markers": ["hormuz", "atlantis"], "route": "gulf_asia"})
    assert s == {"type": "map", "title": "مضيق هرمز", "focus": ["IRN"], "compare": ["SAU"], "markers": ["hormuz"],
                 "route": "gulf_asia"}
    assert spec.clean({"type": "map", "focus": ["XXX"], "route": "nowhere"}) is None
    assert spec.clean({"type": "map", "title": "English only", "focus": ["EGY"]})["title"] is None
    assert spec.clean("map") is None and spec.clean({"type": "video"}) is None


def test_clean_chart_needs_catalog_indicator_or_card_points():
    wb = spec.clean({"type": "chart", "kind": "bar", "indicator": "NY.GDP.PCAP.CD", "countries": ["SAU", "ARE"],
                     "years": [2015, 2024]})
    assert wb["indicator"] == "NY.GDP.PCAP.CD" and wb["years"] == [2015, 2024]
    assert spec.clean({"type": "chart", "kind": "bar", "indicator": "MADE.UP", "countries": ["SAU", "ARE"]}) is None
    assert spec.clean({"type": "chart", "kind": "bar", "indicator": "NY.GDP.PCAP.CD", "countries": ["SAU"]}) is None
    bad_years = spec.clean({"type": "chart", "kind": "line", "indicator": "SP.POP.TOTL", "countries": ["EGY"],
                            "years": [2030, 1900]})
    assert bad_years["years"][1] - bad_years["years"][0] == 20
    pts = spec.clean({"type": "chart", "kind": "line", "points": [{"label": "2023", "value": "2,000"},
                                                                  {"label": "2024", "value": 2400}]})
    assert [p["value"] for p in pts["points"]] == [2000.0, 2400.0]
    assert spec.clean({"type": "chart", "points": [{"label": "x", "value": 1}]}) is None


def test_check_drops_a_visual_whose_figures_are_not_on_the_card():
    ok = spec.clean({"type": "stat", "value": 2400, "unit": "دولار", "label": "سعر أونصة الذهب"})
    assert spec.check(ok, STORY)[0] == ok
    invented = spec.clean({"type": "stat", "value": 2600, "unit": "دولار", "label": "سعر أونصة الذهب"})
    kept, why = spec.check(invented, STORY)
    assert kept is None and "2600" in why
    chart_pts = spec.clean({"type": "chart", "kind": "bar", "points": [{"label": "البنوك", "value": 1037},
                                                                       {"label": "الأفراد", "value": 900}]})
    assert spec.check(chart_pts, STORY)[0] is None
    wb = spec.clean({"type": "chart", "kind": "bar", "indicator": "NY.GDP.PCAP.CD", "countries": ["SAU", "ARE"]})
    assert spec.check(wb, STORY)[0] == wb                     # API data isn't card data


def test_validate_keeps_a_valid_visual_and_ignores_a_broken_one():
    data = {"beats": [
        {"role": "hook", "text": "لماذا يرتفع الذهب", "broll_keywords": ["gold bars"],
         "visual": {"type": "map", "focus": ["CHN"], "markers": ["shanghai"]}},
        {"role": "body", "text": " ".join(["كلمة"] * 20), "broll_keywords": ["vault"], "visual": {"type": "nope"}},
        {"role": "payoff", "text": "هذا هو السبب", "broll_keywords": ["coins"]},
        {"role": "cta", "text": "تابعنا", "broll_keywords": ["map"]}]}
    d = write.validate(data, 1, 1000)
    assert d.beats[0]["visual"]["focus"] == ["CHN"] and "visual" not in d.beats[1]


def test_prompt_offers_the_visual_menu():
    cfg = load_config()
    prompt = write.build_prompt(cfg, {**STORY, "claims": "[]"}, cfg.brands[0], kind="long")
    assert "NY.GDP.PCAP.CD" in prompt and "gulf_asia" in prompt and "hormuz" in prompt
    assert "NEVER financial or investment advice" in prompt


# --- data -----------------------------------------------------------------------------

def _wb_client(rows, calls):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json=[{"page": 1}, rows])
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_worldbank_bar_uses_the_latest_common_year_and_caches(tmp_path):
    cfg = load_config()
    cfg.root = tmp_path
    rows = [{"countryiso3code": "SAU", "date": "2024", "value": 35000.0},
            {"countryiso3code": "SAU", "date": "2023", "value": 34000.0},
            {"countryiso3code": "ARE", "date": "2023", "value": 50000.0},
            {"countryiso3code": "ARE", "date": "2024", "value": None}]
    calls: list[str] = []
    s = {"kind": "bar", "indicator": "NY.GDP.PCAP.CD", "countries": ["SAU", "ARE"], "years": [2020, 2024]}
    data = worldbank.series(cfg, _wb_client(rows, calls), s)
    assert [(p["value"], p["year"]) for p in data["points"]] == [(34000.0, 2023), (50000.0, 2023)]
    assert data["source"] == "البنك الدولي" and data["note"] == "2023"
    worldbank.series(cfg, _wb_client(rows, calls), s)
    assert len(calls) == 1                                   # second call from the cache


def test_worldbank_empty_is_unavailable(tmp_path):
    cfg = load_config()
    cfg.root = tmp_path
    with pytest.raises(worldbank.DataUnavailable):
        worldbank.series(cfg, _wb_client([], []), {"kind": "line", "indicator": "SP.POP.TOTL",
                                                   "countries": ["EGY"], "years": [2000, 2024]})


# --- drawing --------------------------------------------------------------------------

def test_route_chains_lanes_backwards_without_double_joints():
    pts = geo.route_points("asia_europe_suez")
    assert pts[0] == (122.5, 30.5) and pts[-1] == (4.0, 51.98)
    assert all(a != b for a, b in zip(pts, pts[1:]))


def test_map_frames_fill_the_beat_in_both_orientations(tmp_path):
    for size in ((320, 180), (180, 320)):
        sink = Frames()
        mapviz.render({"type": "map", "title": "مضيق هرمز", "focus": ["IRN"], "markers": ["hormuz"],
                       "route": "gulf_asia"}, 2.0, size, tmp_path / "m.mp4", sink)
        assert sink.count == 60 and sink.last.size == size
        # the highlight is on by the end: some pixel is gold-ish
        assert any(r > 200 and g > 150 and b < 80 for _, (r, g, b) in sink.last.getcolors(1_000_000))


def test_charts_and_stat_render(tmp_path):
    bar = {"kind": "bar", "points": [{"label": "قطر", "value": 75000}, {"label": "مصر", "value": 3300}],
           "unit": "دولار", "label": "نصيب الفرد", "source": "البنك الدولي"}
    line = {"kind": "line", "lines": [{"label": "السعودية", "points": [(2000, 40.0), (2010, 50.0), (2021, 23.7)]}],
            "unit": "%", "label": "ريع النفط", "source": "البنك الدولي"}
    stat = {"value": 2400, "unit": "دولار", "label": "سعر أونصة الذهب", "source": "رويترز"}
    for kind, data in (("bar", bar), ("line", line), ("stat", stat)):
        sink = Frames()
        chart.render(kind, data, "عنوان", 1.0, (320, 180), tmp_path / f"{kind}.mp4", sink)
        assert sink.count == 30


def test_number_formatting():
    assert canvas.number(2_400) == "2,400"
    assert canvas.number(1_250_000_000, "دولار") == "1.2 مليار دولار"
    assert canvas.number(23.71, "%") == "23.7%"
    assert chart.nice_ticks(0, 57) == [0, 20, 40, 60]


def test_render_beat_falls_back_when_data_is_missing(tmp_path):
    cfg = load_config()
    cfg.root = tmp_path
    clip, notes = vrender.render_beat(cfg, _wb_client([], []), {"type": "chart", "kind": "bar",
                                      "indicator": "NY.GDP.PCAP.CD", "countries": ["SAU", "ARE"], "years": [2020, 2024]},
                                      2.0, (320, 180), tmp_path / "b.mp4", sink=Frames())
    assert clip is None and "error" in notes
    clip, notes = vrender.render_beat(cfg, _wb_client([], []), {"type": "map", "focus": ["EGY"]}, 1.0, (320, 180),
                                      tmp_path / "m.mp4", sink=Frames())
    assert clip == tmp_path / "m.mp4" and "Natural Earth" in notes["credit"]
