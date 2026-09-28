"""Render a beat's visual spec into an mp4 of the beat's length (assets/generated/visuals/<video>/beat_<i>.mp4).
Missing data (World Bank has no values, geo file absent) returns None: the beat falls back to stock footage."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from src.config import Config
from src.visuals import canvas, chart, geo, mapviz, worldbank

log = logging.getLogger("raij.visuals")


def out_dir(cfg: Config, video_id: int) -> Path:
    return cfg.root / "assets" / "generated" / "visuals" / str(video_id)


def render_beat(cfg: Config, client: httpx.Client, spec: dict[str, Any], seconds: float, size: tuple[int, int],
                out: Path, sink: canvas.FrameSink | None = None) -> tuple[Path | None, dict[str, Any]]:
    """(clip, manifest notes). The notes name the data source so captions can credit it."""
    sink = sink or canvas.ffmpeg_sink(cfg)
    kind = spec["type"]
    try:
        if kind == "map":
            mapviz.render(spec, seconds, size, out, sink)
            return out, {"kind": "map", "credit": "Maps: Natural Earth (public domain)"}
        if kind == "chart" and spec.get("indicator"):
            data = worldbank.series(cfg, client, spec)
            chart.render(data["kind"], data, spec.get("title"), seconds, size, out, sink)
            return out, {"kind": f"chart:{data['kind']}", "indicator": spec["indicator"],
                         "credit": "Data: World Bank Open Data (CC BY 4.0)"}
        if kind == "chart":
            data = {"kind": spec["kind"], "unit": spec.get("unit"), "source": spec.get("source"), "sort": False}
            if spec["kind"] == "bar":
                data["points"] = spec["points"]
            else:
                data["lines"] = [{"label": "", "points": [(i, p["value"]) for i, p in enumerate(spec["points"])]}]
                data["x_labels"] = [p["label"] for p in spec["points"]]
            chart.render(spec["kind"], data, spec.get("title"), seconds, size, out, sink)
            return out, {"kind": f"chart:{spec['kind']}", "credit": None}
        if kind == "stat":
            chart.render("stat", spec, spec.get("title"), seconds, size, out, sink)
            return out, {"kind": "stat", "credit": None}
    except (worldbank.DataUnavailable, geo.GeoMissing, ValueError) as exc:
        log.warning("Visual (%s) skipped, stock instead: %s", kind, exc)
        return None, {"kind": kind, "error": str(exc)[:200]}
    return None, {"kind": kind, "error": "unknown visual"}
