"""Configurable RSS/Atom feeds (news sites, blogs, YouTube channel feeds)."""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone

import feedparser
import httpx

from src.config import Config
from src.discover.common import Candidate, FetchError, SourceResult, canonicalize_url, iso_utc, request

log = logging.getLogger("raij.discover.rss")


def _feeds(cfg: Config) -> list[dict]:
    """Feeds may be plain URLs or {url, region, name} mappings."""
    out = []
    for f in cfg.get("discovery.rss.feeds", []) or []:
        out.append({"url": f} if isinstance(f, str) else dict(f))
    return out


def _thumb(entry) -> str | None:
    for key in ("media_thumbnail", "media_content"):
        for m in entry.get(key) or []:
            if m.get("url"):
                return m["url"]
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and (link.get("type") or "").startswith("image"):
            return link.get("href")
    return None


def parse_feed(content: bytes, feed: dict, max_items: int) -> list[Candidate]:
    parsed = feedparser.parse(content)
    out = []
    for entry in parsed.entries[:max_items]:
        link = entry.get("link")
        if not link:
            continue
        url = canonicalize_url(link)
        ts = entry.get("published_parsed") or entry.get("updated_parsed")
        published = iso_utc(datetime.fromtimestamp(calendar.timegm(ts), tz=timezone.utc)) if ts else None
        out.append(Candidate(
            source="rss",
            external_id=url,
            canonical_url=url,
            title=entry.get("title"),
            thumb_url=_thumb(entry),
            published_at=published,
            region=feed.get("region"),
            raw={
                "feed": feed.get("name") or parsed.feed.get("title") or feed["url"],
                "summary": (entry.get("summary") or "")[:1000],
            },
        ))
    return out


def fetch(cfg: Config, client: httpx.Client) -> SourceResult:
    result = SourceResult()
    max_items = cfg.get("discovery.rss.max_items_per_feed", 20)
    for feed in _feeds(cfg):
        try:
            resp = request(client, "GET", feed["url"])
            found = parse_feed(resp.content, feed, max_items)
        except FetchError as exc:
            result.errors.append(f"{feed['url']}: {exc}")
            log.warning("RSS %s failed: %s", feed["url"], exc)
            continue
        log.info("RSS %s: %d items", feed.get("name") or feed["url"], len(found))
        result.candidates.extend(found)
    return result
