"""YouTube Data API v3: regional trending charts + recent Shorts searches. Metadata only, never media."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from src.config import Config
from src.discover.common import Candidate, FetchError, SourceResult, SourceSkipped, request, youtube_url
from src.discover.quota import QuotaBudget, QuotaExceeded

log = logging.getLogger("raij.discover.youtube")

API = "https://www.googleapis.com/youtube/v3"
# Unit costs: https://developers.google.com/youtube/v3/determine_quota_cost
COST = {"videos.list": 1, "search.list": 100}

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(iso: str | None) -> int | None:
    m = _DURATION.match(iso or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _int(value) -> int | None:
    return int(value) if value not in (None, "") else None


def _candidate(item: dict, region: str, via: str) -> Candidate:
    sn, st, cd = item.get("snippet", {}), item.get("statistics", {}), item.get("contentDetails", {})
    thumbs = sn.get("thumbnails", {})
    thumb = next((thumbs[k]["url"] for k in ("maxres", "high", "medium", "default") if k in thumbs), None)
    return Candidate(
        source="youtube",
        external_id=item["id"],
        canonical_url=youtube_url(item["id"]),
        title=sn.get("title"),
        thumb_url=thumb,
        views=_int(st.get("viewCount")),
        likes=_int(st.get("likeCount")),
        comments=_int(st.get("commentCount")),
        duration_s=parse_duration(cd.get("duration")),
        published_at=sn.get("publishedAt"),
        region=region,
        raw={
            "via": via,
            "channel_id": sn.get("channelId"),
            "channel_title": sn.get("channelTitle"),
            "category_id": sn.get("categoryId"),
            "tags": (sn.get("tags") or [])[:20],
            "default_language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
            "description": (sn.get("description") or "")[:1000],
        },
    )


class YouTube:
    def __init__(self, cfg: Config, client: httpx.Client, budget: QuotaBudget):
        self.key = cfg.secret("YOUTUBE_API_KEY")
        if not self.key:
            raise SourceSkipped("YOUTUBE_API_KEY not set")
        self.cfg = cfg
        self.client = client
        self.budget = budget

    def _get(self, endpoint: str, params: dict) -> dict:
        self.budget.spend(COST[endpoint])
        path = endpoint.split(".")[0]
        return request(self.client, "GET", f"{API}/{path}", params={**params, "key": self.key}).json()

    def trending(self, region: str, max_results: int) -> list[Candidate]:
        data = self._get("videos.list", {
            "part": "snippet,statistics,contentDetails",
            "chart": "mostPopular",
            "regionCode": region,
            "maxResults": min(max_results, 50),
        })
        return [_candidate(item, region, "trending") for item in data.get("items", [])]

    def shorts_search(self, query: str, region: str, lookback_hours: int, max_results: int) -> list[Candidate]:
        since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        data = self._get("search.list", {
            "part": "id",
            "type": "video",
            "q": query,
            "regionCode": region,
            "videoDuration": "short",   # < 4 minutes
            "order": "viewCount",
            "publishedAfter": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxResults": min(max_results, 50),
        })
        ids = [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
        if not ids:
            return []
        # search.list returns no statistics; one cheap videos.list call fills them in.
        details = self._get("videos.list", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids)})
        return [_candidate(item, region, f"search:{query}") for item in details.get("items", [])]


def estimate_units(cfg: Config) -> int:
    regions = cfg.get("discovery.regions", [])
    queries = cfg.get("discovery.youtube.shorts_queries", [])
    per_search = COST["search.list"] + COST["videos.list"]
    return len(regions) * COST["videos.list"] + len(regions) * len(queries) * per_search


def fetch(cfg: Config, client: httpx.Client, budget: QuotaBudget) -> SourceResult:
    yt = YouTube(cfg, client, budget)
    result = SourceResult()
    regions = cfg.get("discovery.regions", [])
    per_chart = cfg.get("discovery.youtube.trending_per_region", 50)
    per_search = cfg.get("discovery.youtube.results_per_query", 25)
    lookback = cfg.get("discovery.youtube.shorts_lookback_hours", 48)

    # Cheapest first: trending charts cost 1 unit per region, searches cost 101.
    jobs = [("trending", r, None) for r in regions]
    jobs += [("search", r, q) for q in cfg.get("discovery.youtube.shorts_queries", []) for r in regions]
    for kind, region, query in jobs:
        label = f"{kind} {region}" + (f" '{query}'" if query else "")
        try:
            if kind == "trending":
                found = yt.trending(region, per_chart)
            else:
                found = yt.shorts_search(query, region, lookback, per_search)
        except QuotaExceeded as exc:
            result.errors.append(str(exc))
            log.warning("Stopping YouTube discovery: %s", exc)
            break
        except FetchError as exc:
            result.errors.append(f"{label}: {exc}")
            log.warning("YouTube %s failed: %s", label, exc)
            continue
        log.info("YouTube %s: %d videos", label, len(found))
        result.candidates.extend(found)
    return result
