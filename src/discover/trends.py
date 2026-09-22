"""Google Trends "Trending now" per-country RSS feed.

The brief named pytrends, but that library was archived in 2025 and its unofficial endpoints
break often. The public trending RSS feed is free, keyless, and gives the same daily signal.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from src.config import Config
from src.discover.common import Candidate, FetchError, SourceResult, iso_utc, request

log = logging.getLogger("raij.discover.trends")

FEED = "https://trends.google.com/trending/rss"
HT = "{https://trends.google.com/trending/rss}"


def parse_traffic(text: str | None) -> int | None:
    """'2K+' → 2000, '500+' → 500, '1M+' → 1000000."""
    m = re.match(r"^\s*([\d.,]+)\s*([KkMm]?)", text or "")
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(n * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1))


def parse_feed(xml: bytes, geo: str) -> list[Candidate]:
    out = []
    for item in ET.fromstring(xml).iter("item"):
        term = (item.findtext("title") or "").strip()
        if not term:
            continue
        news = [
            {
                "title": n.findtext(f"{HT}news_item_title"),
                "url": n.findtext(f"{HT}news_item_url"),
                "source": n.findtext(f"{HT}news_item_source"),
            }
            for n in item.findall(f"{HT}news_item")
        ]
        pub = item.findtext("pubDate")
        out.append(Candidate(
            source="trends",
            external_id=f"{geo}:{term.lower()}",
            canonical_url=f"https://trends.google.com/trends/explore?geo={geo}&q={quote(term.lower())}",
            title=term,
            thumb_url=item.findtext(f"{HT}picture"),
            views=parse_traffic(item.findtext(f"{HT}approx_traffic")),   # approx. searches
            published_at=iso_utc(parsedate_to_datetime(pub)) if pub else None,
            region=geo,
            raw={"approx_traffic": item.findtext(f"{HT}approx_traffic"), "news": news},
        ))
    return out


def fetch(cfg: Config, client: httpx.Client) -> SourceResult:
    result = SourceResult()
    for geo in cfg.get("discovery.trends.geos", []):
        try:
            resp = request(client, "GET", FEED, params={"geo": geo})
            found = parse_feed(resp.content, geo)
        except (FetchError, ET.ParseError) as exc:
            result.errors.append(f"{geo}: {exc}")
            log.warning("Trends %s failed: %s", geo, exc)
            continue
        log.info("Trends %s: %d terms", geo, len(found))
        result.candidates.extend(found)
    return result
