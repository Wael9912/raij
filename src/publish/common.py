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
    title: str                            # short headline (YouTube title, TikTok first line) — variant A
    caption: str                          # full caption: headline, Arabic line, description, tags, sources, credits
    hashtags: list[str]
    category: str | None = None
    series: str | None = None             # series badge name → YouTube playlist (Phase 12)
    hook_ar: str = ""                     # the spoken Arabic hook: first line of the description (SEO)
    title_alt: str | None = None          # variant B headline (Phase 12 A/B): Instagram/Facebook use it
    caption_alt: str | None = None        # the caption with variant B on top


def seo_tags(cfg: Any, category: str | None) -> list[str]:
    """Fixed niche tags (`publish.seo_tags`) for a category plus the default set, as "#tag" strings."""
    if cfg is None:
        return []
    fixed = list(cfg.get(f"publish.seo_tags.{category}", []) or []) if category else []
    fixed += list(cfg.get("publish.seo_tags.default", []) or [])
    return [t if str(t).startswith("#") else f"#{t}" for t in (str(x).strip().replace(" ", "_") for x in fixed) if t]


def merge_tags(script_tags: list[str], fixed: list[str], limit: int = 15) -> list[str]:
    """Script tags first (they're story-specific), then the fixed set, no duplicates."""
    out: list[str] = []
    seen: set[str] = set()
    for t in list(script_tags) + list(fixed):
        key = t.lstrip("#").lower()
        if key and key not in seen:
            seen.add(key)
            out.append(t if t.startswith("#") else f"#{t}")
    return out[:limit]


def _series(ctx: dict[str, Any], notes: dict[str, Any], script_notes: dict[str, Any]) -> str | None:
    return str(notes.get("series") or script_notes.get("series") or "") or None


def post_text(ctx: dict[str, Any], cfg: Any = None) -> PostText:
    """Caption from the approved script. Photo credits are always included (CC BY requires it).
    Layout (Phase 12 SEO): headline / spoken Arabic hook / English description / tags / sources / credits."""
    notes = json.loads(ctx.get("notes") or "{}")
    script_notes = json.loads(ctx.get("script_notes") or "{}")
    beats = json.loads(ctx.get("beats") or "[]")
    hook_ar = str(beats[0]["text"] if beats else "").strip()
    headline = script_notes.get("hook_title") or notes.get("hook_title") or hook_ar
    alt = script_notes.get("hook_title_alt") or None
    tags = merge_tags(json.loads(ctx.get("hashtags") or "[]"), seo_tags(cfg, ctx.get("category")))
    domains = sorted({urlsplit(u).hostname.removeprefix("www.") for u in json.loads(ctx.get("sources") or "[]")
                      if urlsplit(u).hostname})
    tail = [ctx.get("description_en") or "", " ".join(tags)]
    if domains:
        tail.append("المصادر: " + "، ".join(domains))
    tail += [f"📷 {c}" for c in notes.get("credits") or []]

    def build(head: str) -> str:
        parts = [head, hook_ar if hook_ar and hook_ar != head else ""] + tail
        return "\n\n".join(p.strip() for p in parts if p and p.strip())

    return PostText(headline.strip(), build(headline), tags, ctx.get("category"),
                    series=_series(ctx, notes, script_notes), hook_ar=hook_ar,
                    title_alt=str(alt).strip() if alt else None, caption_alt=build(str(alt)) if alt else None)
