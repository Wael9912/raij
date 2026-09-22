"""Shared publish pieces: what a platform gets (PostText), what it returns (Posted), and errors."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


class PublishSkipped(RuntimeError):
    """The platform isn't configured (missing keys); nothing was attempted."""


class PublishError(RuntimeError):
    """The platform refused or failed the post; message is safe to log (no tokens)."""


@dataclass
class Posted:
    external_id: str
    url: str
    status: str = "published"             # or 'exported' (TikTok folder)


@dataclass
class PostText:
    title: str                            # short headline (YouTube title, TikTok first line)
    caption: str                          # full caption: headline, description, tags, sources, credits
    hashtags: list[str]
    category: str | None = None


def post_text(ctx: dict[str, Any]) -> PostText:
    """Caption from the approved script. Photo credits are always included (CC BY requires it)."""
    notes = json.loads(ctx.get("notes") or "{}")
    script_notes = json.loads(ctx.get("script_notes") or "{}")
    beats = json.loads(ctx.get("beats") or "[]")
    headline = script_notes.get("hook_title") or notes.get("hook_title") or (beats[0]["text"] if beats else "")
    tags = json.loads(ctx.get("hashtags") or "[]")
    domains = sorted({urlsplit(u).hostname.removeprefix("www.") for u in json.loads(ctx.get("sources") or "[]")
                      if urlsplit(u).hostname})
    parts = [headline, ctx.get("description_en") or "", " ".join(tags)]
    if domains:
        parts.append("المصادر: " + "، ".join(domains))
    parts += [f"📷 {c}" for c in notes.get("credits") or []]
    return PostText(headline.strip(), "\n\n".join(p.strip() for p in parts if p and p.strip()), tags,
                    ctx.get("category"))
