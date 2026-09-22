"""Stock b-roll: search Pexels then Pixabay by beat keywords, download into assets/stock/.

Both are free with a key and allow commercial use without attribution; we still record the
provider, clip id, page URL and author per clip in videos.broll_manifest. Portrait clips are
preferred (no crop); landscape ones get center-cropped at render. Downloads are cached by
provider+id and never fetched twice.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from src.config import Config
from src.discover.common import FetchError, request

log = logging.getLogger("raij.assemble")

PEXELS_URL = "https://api.pexels.com/videos/search"
PIXABAY_URL = "https://pixabay.com/api/videos/"
TARGET_W = 1080


class BrollError(RuntimeError):
    pass


@dataclass
class Clip:
    provider: str
    id: str
    url: str              # download URL of the chosen file
    page: str             # human-facing page, for the manifest
    author: str
    width: int
    height: int
    duration: float
    license: str
    path: str = ""        # repo-relative, once downloaded

    @property
    def portrait(self) -> bool:
        return self.height > self.width


def _pick_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Smallest file that is still ≥1080 on its short side, else the largest available."""
    usable = [f for f in files if f.get("link") and f.get("width") and f.get("height")]
    if not usable:
        return None
    short = lambda f: min(f["width"], f["height"])           # noqa: E731
    big = [f for f in usable if short(f) >= TARGET_W]
    return min(big, key=short) if big else max(usable, key=short)


def search_pexels(client: httpx.Client, key: str, query: str) -> list[Clip]:
    resp = request(client, "GET", PEXELS_URL, headers={"Authorization": key},
                   params={"query": query, "orientation": "portrait", "per_page": 15, "size": "medium"})
    clips = []
    for v in resp.json().get("videos", []):
        f = _pick_file(v.get("video_files") or [])
        if f:
            clips.append(Clip("pexels", str(v["id"]), f["link"], v.get("url", ""),
                              (v.get("user") or {}).get("name", ""), f["width"], f["height"],
                              float(v.get("duration") or 0), "Pexels License"))
    return clips


def search_pixabay(client: httpx.Client, key: str, query: str) -> list[Clip]:
    resp = request(client, "GET", PIXABAY_URL, params={"key": key, "q": query, "per_page": 20, "safesearch": "true"})
    clips = []
    for h in resp.json().get("hits", []):
        files = [{"link": f.get("url"), "width": f.get("width"), "height": f.get("height")}
                 for f in (h.get("videos") or {}).values()]
        f = _pick_file(files)
        if f:
            clips.append(Clip("pixabay", str(h["id"]), f["link"], h.get("pageURL", ""), h.get("user", ""),
                              f["width"], f["height"], float(h.get("duration") or 0), "Pixabay Content License"))
    return clips


def providers(cfg: Config) -> list[tuple[str, str]]:
    out = []
    if cfg.secret("PEXELS_API_KEY"):
        out.append(("pexels", cfg.secret("PEXELS_API_KEY")))
    if cfg.secret("PIXABAY_API_KEY"):
        out.append(("pixabay", cfg.secret("PIXABAY_API_KEY")))
    return out


SEARCH = {"pexels": search_pexels, "pixabay": search_pixabay}


MAX_CLIP_SECONDS = 60        # longer stock clips are big downloads for the few seconds we use
CUT_EVERY = 7.0              # aim for a new shot about this often
MAX_CLIPS_PER_BEAT = 4


def clips_needed(seconds: float) -> int:
    return min(MAX_CLIPS_PER_BEAT, max(1, math.ceil(seconds / CUT_EVERY)))


def choose(cfg: Config, client: httpx.Client, keywords: list[str], need: float, used: set[str],
           recent: set[str] | None = None) -> list[Clip]:
    """Clips for one beat — one per ~7s of it — never reused within the video, and preferring
    portrait, not used in recent videos, and short (small downloads) but long enough to fill a cut."""
    recent = recent or set()
    max_clips = clips_needed(need)
    per_clip = need / max_clips
    keys = providers(cfg)
    if not keys:
        raise BrollError("no stock footage key: set PEXELS_API_KEY or PIXABAY_API_KEY in .env")
    found: list[Clip] = []
    for kw in keywords:
        for name, key in keys:
            try:
                found += SEARCH[name](client, key, kw)
            except (FetchError, ValueError) as exc:
                log.warning("%s search %r failed: %s", name, kw, exc)
        good = [c for c in found if c.portrait and f"{c.provider}:{c.id}" not in used | recent]
        if len(good) >= max_clips:
            break                                     # good enough; spare the quota
    seen: set[str] = set()
    fresh = []
    for c in found:
        key = f"{c.provider}:{c.id}"
        if key not in used and key not in seen and 2 <= c.duration <= MAX_CLIP_SECONDS:
            seen.add(key)
            fresh.append(c)
    fresh.sort(key=lambda c: (not c.portrait, f"{c.provider}:{c.id}" in recent, c.duration < per_clip, c.duration))
    picked: list[Clip] = []
    for c in fresh[:max_clips]:
        picked.append(c)
        used.add(f"{c.provider}:{c.id}")
    if not picked:
        raise BrollError(f"no usable clips for {keywords}")
    return picked


def download(cfg: Config, client: httpx.Client, clip: Clip) -> Clip:
    stock = cfg.root / "assets" / "stock"
    stock.mkdir(parents=True, exist_ok=True)
    dest = stock / f"{clip.provider}_{clip.id}.mp4"
    if not dest.exists() or dest.stat().st_size == 0:
        part = dest.with_suffix(".part")
        with client.stream("GET", clip.url, follow_redirects=True, timeout=120) as resp:
            if resp.status_code >= 400:
                raise BrollError(f"download {clip.provider}:{clip.id} failed: HTTP {resp.status_code}")
            with open(part, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        part.rename(dest)
    clip.path = str(dest.relative_to(cfg.root))
    return clip


def manifest_entry(clip: Clip, beat: int, start: float, dur: float) -> dict[str, Any]:
    d = asdict(clip)
    d.pop("url")
    return {**d, "beat": beat, "at": round(start, 3), "seconds": round(dur, 3)}
