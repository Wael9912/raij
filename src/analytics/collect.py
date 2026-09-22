"""Per-platform metrics for published posts. Each collector returns {post_id: Metric} for the posts it
could read and never raises for a single bad post — a missing number just stays None.

YouTube: Data API `videos.list` statistics (fresh counts, 1 unit per 50 videos) + YouTube Analytics
reports for watch time / retention / shares (1–2 days behind). Instagram: media insights. Facebook:
Reels video_insights. TikTok exports have no metrics (manual upload).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.config import Config
from src.discover.common import FetchError, request

log = logging.getLogger("raij.analytics")


@dataclass
class Metric:
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    avg_watch_s: float | None = None
    retention_pct: float | None = None

    def merge(self, other: "Metric") -> "Metric":
        return Metric(**{k: v if v is not None else getattr(other, k) for k, v in asdict(self).items()})


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --- YouTube -----------------------------------------------------------------

def youtube(cfg: Config, client: httpx.Client, posts: list[dict]) -> dict[int, Metric]:
    from src.publish import youtube as yt
    if not posts or yt.missing(cfg):
        return {}
    auth = {"Authorization": f"Bearer {yt.access_token(cfg, client)}"}
    by_ext = {p["external_id"]: p["id"] for p in posts}
    out: dict[int, Metric] = {}
    ids = list(by_ext)
    for i in range(0, len(ids), 50):
        resp = request(client, "GET", "https://www.googleapis.com/youtube/v3/videos", headers=auth,
                       params={"part": "statistics", "id": ",".join(ids[i:i + 50])}).json()
        for item in resp.get("items", []):
            s = item.get("statistics", {})
            out[by_ext[item["id"]]] = Metric(_int(s.get("viewCount")), _int(s.get("likeCount")),
                                             _int(s.get("commentCount")))
    try:
        start = min(p["published_at"][:10] for p in posts)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rep = request(client, "GET", "https://youtubeanalytics.googleapis.com/v2/reports", headers=auth, params={
            "ids": "channel==MINE", "startDate": start, "endDate": today, "dimensions": "video",
            "metrics": "averageViewDuration,averageViewPercentage,shares", "filters": "video==" + ",".join(ids),
        }).json()
        cols = [c["name"] for c in rep.get("columnHeaders", [])]
        for row in rep.get("rows") or []:
            r = dict(zip(cols, row))
            pid = by_ext.get(r.get("video"))
            if pid is not None:
                extra = Metric(shares=_int(r.get("shares")), avg_watch_s=r.get("averageViewDuration"),
                               retention_pct=r.get("averageViewPercentage"))
                out[pid] = out.get(pid, Metric()).merge(extra)
    except FetchError as exc:                       # analytics lag or API not enabled: counts still count
        log.warning("YouTube Analytics unavailable (%s) — views/likes only", exc)
    return out


# --- Meta --------------------------------------------------------------------

def _values(data: dict) -> dict[str, Any]:
    out = {}
    for m in data.get("data", []):
        if "total_value" in m:
            out[m["name"]] = m["total_value"].get("value")
        elif m.get("values"):
            out[m["name"]] = m["values"][-1].get("value")
    return out


def instagram(cfg: Config, client: httpx.Client, posts: list[dict]) -> dict[int, Metric]:
    from src.publish import meta
    if not posts or meta.missing_instagram(cfg):
        return {}
    g, token = meta._graph(cfg), cfg.secret("META_PAGE_ACCESS_TOKEN")
    out = {}
    for p in posts:
        v = None
        for metrics in ("views,likes,comments,shares,ig_reels_avg_watch_time", "views,likes,comments,shares"):
            try:
                v = _values(request(client, "GET", f"{g}/{p['external_id']}/insights",
                                    params={"metric": metrics, "access_token": token}).json())
                break
            except FetchError as exc:
                log.debug("IG insights %s (%s): %s", p["external_id"], metrics, exc)
        if v is None:
            log.warning("Instagram insights unavailable for post %d", p["id"])
            continue
        ms = v.get("ig_reels_avg_watch_time")
        out[p["id"]] = Metric(_int(v.get("views")), _int(v.get("likes")), _int(v.get("comments")),
                              _int(v.get("shares")), ms / 1000 if isinstance(ms, (int, float)) else None)
    return out


def facebook(cfg: Config, client: httpx.Client, posts: list[dict]) -> dict[int, Metric]:
    from src.publish import meta
    if not posts or meta.missing_facebook(cfg):
        return {}
    g, token = meta._graph(cfg), cfg.secret("META_PAGE_ACCESS_TOKEN")
    out = {}
    for p in posts:
        try:
            v = _values(request(client, "GET", f"{g}/{p['external_id']}/video_insights", params={
                "metric": "fb_reels_total_plays,post_video_avg_time_watched,post_video_likes_by_reaction_type",
                "access_token": token}).json())
        except FetchError as exc:
            log.warning("Facebook insights unavailable for post %d: %s", p["id"], exc)
            continue
        likes = v.get("post_video_likes_by_reaction_type")
        ms = v.get("post_video_avg_time_watched")
        out[p["id"]] = Metric(_int(v.get("fb_reels_total_plays")),
                              sum(likes.values()) if isinstance(likes, dict) else _int(likes),
                              avg_watch_s=ms / 1000 if isinstance(ms, (int, float)) else None)
    return out


COLLECTORS = {"youtube": youtube, "instagram": instagram, "facebook": facebook}


def since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
