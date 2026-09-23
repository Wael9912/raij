"""Instagram Reels + Facebook Reels via the Meta Graph API, both with resumable upload (no public URL needed).

Instagram: create a REELS container with upload_type=resumable → send the bytes to rupload → poll the
container until FINISHED → media_publish. Facebook: /{page}/video_reels start → rupload → finish with
video_state=PUBLISHED. The page token goes in the POST body or the rupload Authorization header, never
in logged URLs. The final publish calls are not retried automatically, so a flaky response can't post twice.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

import httpx

from src.config import Config
from src.discover.common import request
from src.publish.common import Posted, PostText, PublishError

log = logging.getLogger("raij.publish")
KEYS = {"instagram": ("META_IG_USER_ID", "META_PAGE_ACCESS_TOKEN"),
        "facebook": ("META_PAGE_ID", "META_PAGE_ACCESS_TOKEN")}


def _graph(cfg: Config) -> str:
    return f"https://graph.facebook.com/{cfg.get('publish.meta_graph_version', 'v25.0')}"


def _missing(cfg: Config, platform: str) -> str | None:
    unset = [k for k in KEYS[platform] if not cfg.secret(k)]
    return f"{', '.join(unset)} not set (SETUP.md §5)" if unset else None


def missing_instagram(cfg: Config) -> str | None:
    return _missing(cfg, "instagram")


def missing_facebook(cfg: Config) -> str | None:
    return _missing(cfg, "facebook")


def _rupload(client: httpx.Client, url: str, token: str, video: Path) -> dict:
    data = video.read_bytes()
    resp = request(client, "POST", url, content=data, timeout=600,
                   headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))})
    body = resp.json()
    if body.get("success") is False:
        raise PublishError(f"upload refused: {str(body)[:200]}")
    return body


def publish_instagram(cfg: Config, client: httpx.Client, video: Path, text: PostText, video_id: int,
                      sleep: Callable[[float], None] = time.sleep) -> Posted:
    ig, token = cfg.secret("META_IG_USER_ID"), cfg.secret("META_PAGE_ACCESS_TOKEN")
    g = _graph(cfg)
    container = request(client, "POST", f"{g}/{ig}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": (text.caption_alt or text.caption)[:2200],
        "share_to_feed": "true", "access_token": token}).json()
    cid, upload_url = container.get("id"), container.get("uri")
    if not cid or not upload_url:
        raise PublishError(f"Instagram gave no container/upload URL: {str(container)[:200]}")
    _rupload(client, upload_url, token, video)

    wait, waited = float(cfg.get("publish.poll_seconds", 5)), 0.0
    limit = float(cfg.get("publish.poll_timeout_seconds", 600))
    while True:
        st = request(client, "GET", f"{g}/{cid}", params={"fields": "status_code,status", "access_token": token}).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise PublishError(f"Instagram processing {code}: {st.get('status', '')}"[:300])
        if waited >= limit:
            raise PublishError(f"Instagram still processing after {limit:.0f}s ({code})")
        sleep(wait)
        waited += wait

    media = request(client, "POST", f"{g}/{ig}/media_publish", retries=0,
                    data={"creation_id": cid, "access_token": token}).json()
    mid = media.get("id")
    if not mid:
        raise PublishError(f"Instagram media_publish returned no id: {str(media)[:200]}")
    try:
        link = request(client, "GET", f"{g}/{mid}", params={"fields": "permalink", "access_token": token}).json()
        url = link.get("permalink") or ""
    except Exception:                                    # published anyway; the link is a nicety
        url = ""
    return Posted(mid, url or f"https://www.instagram.com/ (media {mid})")


def publish_facebook(cfg: Config, client: httpx.Client, video: Path, text: PostText, video_id: int) -> Posted:
    page, token = cfg.secret("META_PAGE_ID"), cfg.secret("META_PAGE_ACCESS_TOKEN")
    g = _graph(cfg)
    start = request(client, "POST", f"{g}/{page}/video_reels",
                    data={"upload_phase": "start", "access_token": token}).json()
    vid, upload_url = start.get("video_id"), start.get("upload_url")
    if not vid or not upload_url:
        raise PublishError(f"Facebook gave no video id/upload URL: {str(start)[:200]}")
    _rupload(client, upload_url, token, video)
    done = request(client, "POST", f"{g}/{page}/video_reels", retries=0, data={
        "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
        "description": (text.caption_alt or text.caption)[:2200], "access_token": token}).json()
    if not done.get("success"):
        raise PublishError(f"Facebook finish failed: {str(done)[:200]}")
    return Posted(str(vid), f"https://www.facebook.com/reel/{vid}")
