"""Reddit API (app-only OAuth): top posts of the day from configured subreddits."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from src.config import Config
from src.discover.common import Candidate, FetchError, SourceResult, SourceSkipped, iso_utc, request

log = logging.getLogger("raij.discover.reddit")

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"


def _token(client: httpx.Client, client_id: str, secret: str, user_agent: str) -> str:
    resp = request(
        client, "POST", TOKEN_URL,
        auth=(client_id, secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": user_agent},
    )
    token = resp.json().get("access_token")
    if not token:
        raise FetchError("Reddit token response had no access_token (check client id/secret)")
    return token


def _candidate(post: dict, subreddit: str) -> Candidate:
    thumb = post.get("thumbnail")
    if not (thumb or "").startswith("http"):
        thumb = None
    video = ((post.get("media") or {}).get("reddit_video") or {})
    return Candidate(
        source="reddit",
        external_id=post["id"],
        canonical_url=f"https://www.reddit.com/comments/{post['id']}",
        title=post.get("title"),
        thumb_url=thumb,
        views=None,                      # Reddit doesn't expose view counts
        likes=post.get("score"),
        comments=post.get("num_comments"),
        duration_s=video.get("duration"),
        published_at=iso_utc(datetime.fromtimestamp(post.get("created_utc", 0), tz=timezone.utc)),
        region=None,
        raw={
            "subreddit": subreddit,
            "permalink": post.get("permalink"),
            "linked_url": post.get("url_overridden_by_dest") or post.get("url"),
            "is_video": post.get("is_video"),
            "upvote_ratio": post.get("upvote_ratio"),
            "flair": post.get("link_flair_text"),
            "selftext": (post.get("selftext") or "")[:2000],
        },
    )


def fetch(cfg: Config, client: httpx.Client) -> SourceResult:
    cid, secret = cfg.secret("REDDIT_CLIENT_ID"), cfg.secret("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        raise SourceSkipped("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set")
    user_agent = cfg.secret("REDDIT_USER_AGENT", "raij/0.1")
    limit = cfg.get("discovery.reddit.limit_per_sub", 25)

    token = _token(client, cid, secret, user_agent)
    headers = {"Authorization": f"bearer {token}", "User-Agent": user_agent}
    result = SourceResult()
    for sub in cfg.get("discovery.reddit.subreddits", []):
        try:
            data = request(
                client, "GET", f"{API}/r/{sub}/top",
                params={"t": "day", "limit": min(limit, 100), "raw_json": 1},
                headers=headers,
            ).json()
        except FetchError as exc:
            result.errors.append(f"r/{sub}: {exc}")
            log.warning("Reddit r/%s failed: %s", sub, exc)
            continue
        posts = [c["data"] for c in data.get("data", {}).get("children", [])]
        found = [_candidate(p, sub) for p in posts if not p.get("stickied") and not p.get("over_18")]
        log.info("Reddit r/%s: %d posts", sub, len(found))
        result.candidates.extend(found)
    return result
