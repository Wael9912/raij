"""Shared discovery plumbing: candidate model, URL canonicalization, HTTP, DB upsert."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

log = logging.getLogger("raij.discover")

USER_AGENT = "raij/0.1 (+trend discovery; contact via repo owner)"

# Query params that only track the click, never identify the content.
_TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|igshid|mc_cid|mc_eid|ref|ref_src|si|feature)$", re.I)
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass
class Candidate:
    source: str
    external_id: str
    canonical_url: str
    title: str | None = None
    thumb_url: str | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    duration_s: int | None = None
    published_at: str | None = None   # ISO-8601 UTC, "YYYY-MM-DDTHH:MM:SSZ"
    region: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceResult:
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class FetchError(RuntimeError):
    """HTTP failure with a message that is safe to log (no API keys)."""


class SourceSkipped(RuntimeError):
    """Source is disabled or missing credentials."""


def youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def canonicalize_url(url: str) -> str:
    """Normalize a URL so the same content always maps to one string."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    query = parse_qsl(parts.query, keep_blank_values=False)

    # YouTube: watch?v=, youtu.be/ID, /shorts/ID, /embed/ID → one watch URL.
    if host.endswith("youtube.com") or host == "youtu.be":
        vid = None
        if host == "youtu.be":
            vid = path.strip("/").split("/")[0]
        elif path.startswith(("/shorts/", "/embed/", "/live/")):
            vid = path.split("/")[2]
        else:
            vid = dict(query).get("v")
        if vid and _YT_ID.match(vid):
            return youtube_url(vid)

    # Reddit: any permalink form → /comments/<id>.
    if host.endswith("reddit.com") or host == "redd.it":
        m = re.search(r"/comments/([a-z0-9]+)", path)
        if m:
            return f"https://www.reddit.com/comments/{m.group(1)}"
        if host == "redd.it" and path.strip("/"):
            return f"https://www.reddit.com/comments/{path.strip('/')}"

    query = sorted((k, v) for k, v in query if not _TRACKING_PARAMS.match(k))
    if len(path) > 1:
        path = path.rstrip("/")
    netloc = host + (f":{parts.port}" if parts.port and parts.port not in (80, 443) else "")
    return urlunsplit(((parts.scheme or "https").lower(), netloc, path, urlencode(query), ""))


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_client() -> httpx.Client:
    return httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def request(
    client: httpx.Client, method: str, url: str, *, retries: int = 2, **kwargs: Any
) -> httpx.Response:
    """HTTP with retry on 429/5xx. Errors never include the query string (it may hold keys)."""
    safe_url = url.split("?")[0]
    for attempt in range(retries + 1):
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            if attempt == retries:
                raise FetchError(f"{method} {safe_url}: {type(exc).__name__}") from None
        else:
            if resp.status_code < 400:
                return resp
            if resp.status_code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise FetchError(f"{method} {safe_url}: HTTP {resp.status_code} {_api_message(resp)}".rstrip())
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def _api_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message", ""))[:200]
    return str(body.get("message", err or ""))[:200] if isinstance(body, dict) else ""


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse same-URL candidates within a run; the same video can trend in several regions."""
    by_url: dict[str, Candidate] = {}
    for c in candidates:
        seen = by_url.get(c.canonical_url)
        if seen is None:
            by_url[c.canonical_url] = c
            continue
        regions = {r for r in (seen.region or "").split(",") + (c.region or "").split(",") if r}
        seen.region = ",".join(sorted(regions)) or None
        for attr in ("views", "likes", "comments"):
            new = getattr(c, attr)
            if new is not None and (getattr(seen, attr) or 0) < new:
                setattr(seen, attr, new)
    return list(by_url.values())


_UPSERT = """
INSERT INTO candidates (source, external_id, canonical_url, title, thumb_url, views, likes,
                        comments, duration_s, published_at, region, raw_json)
VALUES (:source, :external_id, :canonical_url, :title, :thumb_url, :views, :likes,
        :comments, :duration_s, :published_at, :region, :raw_json)
ON CONFLICT(canonical_url) DO UPDATE SET
    views      = COALESCE(excluded.views, views),
    likes      = COALESCE(excluded.likes, likes),
    comments   = COALESCE(excluded.comments, comments),
    thumb_url  = COALESCE(excluded.thumb_url, thumb_url),
    raw_json   = excluded.raw_json,
    last_seen_at = datetime('now')
"""


def upsert_candidates(conn: sqlite3.Connection, candidates: list[Candidate]) -> tuple[int, int]:
    """Insert new candidates, refresh metrics on known ones. Returns (inserted, updated)."""
    inserted = updated = 0
    for c in candidates:
        exists = conn.execute(
            "SELECT 1 FROM candidates WHERE canonical_url = ? OR (source = ? AND external_id = ?)",
            (c.canonical_url, c.source, c.external_id),
        ).fetchone()
        row = {**c.__dict__, "raw_json": json.dumps(c.raw, ensure_ascii=False)}
        row.pop("raw")
        try:
            conn.execute(_UPSERT, row)
        except sqlite3.IntegrityError:
            # Same (source, external_id) under a different URL — keep the first URL.
            log.debug("Skipping %s:%s (id already stored under another URL)", c.source, c.external_id)
            continue
        if exists:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    return inserted, updated
