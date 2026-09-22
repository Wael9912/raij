"""score = view_velocity*w1 + engagement*w2 + recency*w3, each part in [0, 1].

Sources expose different signals, so velocity and engagement are computed per source and then
turned into a percentile *within that source* before weighting. A source with no signal for a
part (RSS has no views) gets a neutral 0.5 there and competes mostly on recency.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

NEUTRAL = 0.5
RECENCY_HALF_LIFE_H = 24.0


@dataclass
class Scored:
    id: int
    score: float
    parts: dict[str, float]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_hours(row: dict[str, Any], now: datetime) -> float:
    ts = _parse_ts(row.get("published_at")) or _parse_ts(row.get("discovered_at")) or now
    return max((now - ts).total_seconds() / 3600, 1.0)


def raw_signals(row: dict[str, Any], now: datetime) -> tuple[float | None, float | None]:
    """(velocity, engagement) in source-native units; None when the source has no such signal."""
    raw = json.loads(row.get("raw_json") or "{}")
    hours = age_hours(row, now)
    views, likes, comments = row.get("views"), row.get("likes"), row.get("comments")
    source = row.get("source")

    if source == "youtube":
        velocity = views / hours if views is not None else None
        engagement = ((likes or 0) + 2 * (comments or 0)) / views if views else None
    elif source == "reddit":
        velocity = likes / hours if likes is not None else None      # upvotes per hour
        engagement = (raw.get("upvote_ratio") or 0) * math.log1p(comments or 0) if comments is not None else None
    elif source == "trends":
        velocity = views / hours if views is not None else None      # approx. searches per hour
        engagement = float(len(raw.get("news") or [])) or None       # how many outlets cover it
    else:
        velocity = engagement = None
    return velocity, engagement


def percentiles(values: dict[int, float | None]) -> dict[int, float]:
    """Map each id to its percentile rank in [0, 1] among non-null values; nulls → NEUTRAL."""
    present = sorted(v for v in values.values() if v is not None)
    out = {}
    for key, v in values.items():
        if v is None or not present:
            out[key] = NEUTRAL
        elif len(present) == 1:
            out[key] = 1.0
        else:
            below = sum(1 for p in present if p < v)
            equal = sum(1 for p in present if p == v)
            out[key] = (below + (equal - 1) / 2) / (len(present) - 1)
    return out


def recency(row: dict[str, Any], now: datetime) -> float:
    return 0.5 ** (age_hours(row, now) / RECENCY_HALF_LIFE_H)


def score_rows(rows: list[dict[str, Any]], weights: dict[str, float], now: datetime | None = None) -> list[Scored]:
    now = now or datetime.now(timezone.utc)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)

    velocity_pct: dict[int, float] = {}
    engagement_pct: dict[int, float] = {}
    for group in by_source.values():
        signals = {r["id"]: raw_signals(r, now) for r in group}
        velocity_pct.update(percentiles({k: v[0] for k, v in signals.items()}))
        engagement_pct.update(percentiles({k: v[1] for k, v in signals.items()}))

    w_v = weights.get("view_velocity", 0.5)
    w_e = weights.get("engagement", 0.3)
    w_r = weights.get("recency", 0.2)
    out = []
    for r in rows:
        parts = {
            "view_velocity": round(velocity_pct[r["id"]], 3),
            "engagement": round(engagement_pct[r["id"]], 3),
            "recency": round(recency(r, now), 3),
        }
        total = w_v * parts["view_velocity"] + w_e * parts["engagement"] + w_r * parts["recency"]
        out.append(Scored(id=r["id"], score=round(total, 4), parts=parts))
    out.sort(key=lambda s: s.score, reverse=True)
    return out
