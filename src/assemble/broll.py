"""Stock b-roll: search Pexels then Pixabay by beat keywords, download into assets/stock/.

Both are free with a key and allow commercial use without attribution; we still record the
provider, clip id, page URL and author per clip in videos.broll_manifest. Portrait clips are
preferred (no crop); landscape ones get center-cropped at render. Downloads are cached by
provider+id and never fetched twice.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from src.config import Config
from src.discover.common import FetchError, request

log = logging.getLogger("raij.assemble")

PEXELS_URL = "https://api.pexels.com/videos/search"
PEXELS_PHOTOS_URL = "https://api.pexels.com/v1/search"
PIXABAY_URL = "https://pixabay.com/api/videos/"
TARGET_W = 1080
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")      # provider ids become file names (S3)
# Pexels/Pixabay return no tags, but the clip's page slug names the shot ("pouring-beer-into-a-glass-…"): footage
# a Gulf audience (and advertisers) would object to is dropped by slug — a gold video opened on a beer glass live.
BLOCKED_SLUG = re.compile(r"\b(beer|wine|alcohol|liquor|whisk(e)?y|vodka|cocktail|champagne|bar-counter|casino|"
                          r"poker|gambling|bikini|lingerie|nude|naked|sexy|cigarette|smoking|vape|hookah|pork|"
                          r"bacon|tattoo|twerk|strip)\b", re.I)
MAX_CLIP_BYTES = 200 * 2**20                       # a stock clip is never this big; an error page or bomb might be


class BrollError(RuntimeError):
    """No usable clips for this beat (deterministic: the video fails)."""


class BrollUnavailable(RuntimeError):
    """Every stock search errored (429/5xx/network) — nothing was actually looked up, so the video
    is left for the next run instead of being failed for good (A7)."""


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
    previews: list[str] = field(default_factory=list)   # small still frames, for the face check
    still: bool = False   # a photo (Ken Burns at render) — the fallback when no clip fits

    @property
    def portrait(self) -> bool:
        return self.height > self.width

    def fits(self, orientation: str) -> bool:
        return self.portrait == (orientation == "portrait")


def _pick_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Smallest file that is still ≥1080 on its short side, else the largest available."""
    usable = [f for f in files if f.get("link") and f.get("width") and f.get("height")]
    if not usable:
        return None
    short = lambda f: min(f["width"], f["height"])           # noqa: E731
    big = [f for f in usable if short(f) >= TARGET_W]
    return min(big, key=short) if big else max(usable, key=short)


def search_pexels(client: httpx.Client, key: str, query: str, orientation: str = "portrait") -> list[Clip]:
    resp = request(client, "GET", PEXELS_URL, headers={"Authorization": key},
                   params={"query": query, "orientation": orientation, "per_page": 15, "size": "medium"})
    clips = []
    for v in resp.json().get("videos", []):
        f = _pick_file(v.get("video_files") or [])
        if f:
            pics = [p["picture"] for p in v.get("video_pictures") or [] if p.get("picture")]
            previews = [pics[i] for i in sorted({len(pics) // 4, len(pics) // 2, 3 * len(pics) // 4})] if pics else \
                [v["image"]] if v.get("image") else []
            clips.append(Clip("pexels", str(v["id"]), f["link"], v.get("url", ""),
                              (v.get("user") or {}).get("name", ""), f["width"], f["height"],
                              float(v.get("duration") or 0), "Pexels License", previews=previews))
    return clips


def search_pexels_photos(client: httpx.Client, key: str, query: str, orientation: str = "portrait") -> list[Clip]:
    """Photos as a fallback when no faceless clip matches (Ken Burns at render): far cheaper to find than video
    for abstract or local subjects. Same Pexels key and licence."""
    resp = request(client, "GET", PEXELS_PHOTOS_URL, headers={"Authorization": key},
                   params={"query": query, "orientation": orientation, "per_page": 15})
    clips = []
    for p in resp.json().get("photos", []):
        src = p.get("src") or {}
        link = src.get("large2x") or src.get("large") or src.get("original")
        if not link:
            continue
        clips.append(Clip("pexels-photo", str(p["id"]), link, p.get("url", ""), p.get("photographer", ""),
                          int(p.get("width") or 0), int(p.get("height") or 0), 8.0, "Pexels License",
                          previews=[src.get("medium") or link], still=True))
    return clips


def search_pixabay(client: httpx.Client, key: str, query: str, orientation: str = "portrait") -> list[Clip]:
    resp = request(client, "GET", PIXABAY_URL, params={"key": key, "q": query, "per_page": 20, "safesearch": "true"})
    clips = []
    for h in resp.json().get("hits", []):
        files = [{"link": f.get("url"), "width": f.get("width"), "height": f.get("height")}
                 for f in (h.get("videos") or {}).values()]
        f = _pick_file(files)
        if f:
            thumbs = [v.get("thumbnail") for v in (h.get("videos") or {}).values() if v.get("thumbnail")]
            clips.append(Clip("pixabay", str(h["id"]), f["link"], h.get("pageURL", ""), h.get("user", ""),
                              f["width"], f["height"], float(h.get("duration") or 0), "Pixabay Content License",
                              previews=thumbs[:1]))
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


def clips_needed(seconds: float, cut_every: float = CUT_EVERY) -> int:
    return min(MAX_CLIPS_PER_BEAT, max(1, math.ceil(seconds / max(cut_every, 1.0))))


def _faceless(client: httpx.Client, clip: Clip, orientation: str = "portrait") -> bool:
    from src.assemble import faces
    min_area = faces.MIN_AREA if orientation == "portrait" else faces.MIN_AREA_WIDE
    return not faces.has_face(client, clip.previews, min_area=min_area)


def choose(cfg: Config, client: httpx.Client, keywords: list[str], need: float, used: set[str],
           recent: set[str] | None = None, faceless=_faceless, orientation: str = "portrait",
           cut_every: float = CUT_EVERY) -> list[Clip]:
    """Clips for one beat — one per ~`cut_every` s of it — never reused within the video, and preferring the
    frame's orientation, not used in recent videos, and short (small downloads) but long enough to fill a cut.
    With video.faceless, clips whose previews show a face are skipped; the next keyword is searched
    when a keyword doesn't yield enough faceless clips. No clip at all → Pexels *photos* (Ken Burns) before
    giving up (`video.photo_fallback`)."""
    recent = recent or set()
    max_clips = clips_needed(need, cut_every)
    per_clip = need / max_clips
    keys = providers(cfg)
    if not keys:
        raise BrollError("no stock footage key: set PEXELS_API_KEY or PIXABAY_API_KEY in .env")
    if not cfg.get("video.faceless", True):
        check = lambda _client, _clip: True                                       # noqa: E731
    elif faceless is _faceless:
        check = lambda cl, c: _faceless(cl, c, orientation)                       # noqa: E731
    else:
        check = faceless
    picked: list[Clip] = []
    checked: set[str] = set()
    rejected = searched = errored = 0
    searches: list[tuple[str, Any]] = [(kw, SEARCH[name], key) for kw in keywords for name, key in keys]
    if cfg.get("video.photo_fallback", True) and dict(keys).get("pexels"):
        searches += [(kw, search_pexels_photos, dict(keys)["pexels"]) for kw in keywords[:2]]
    for kw, search, key in searches:
        if len(picked) >= max_clips or (picked and search is search_pexels_photos):
            break                                          # photos only when no clip was found at all
        try:
            found = search(client, key, kw, orientation)
            searched += 1
        except (FetchError, ValueError) as exc:
            errored += 1
            log.warning("%s search %r failed: %s", getattr(search, "__name__", "stock"), kw, exc)
            continue
        fresh = [c for c in found if f"{c.provider}:{c.id}" not in used | checked
                 and (c.still or 2 <= c.duration <= MAX_CLIP_SECONDS)
                 and not BLOCKED_SLUG.search(c.page.replace("-", " "))]
        fresh.sort(key=lambda c: (not c.fits(orientation), f"{c.provider}:{c.id}" in recent, c.duration < per_clip,
                                  c.duration))
        for c in fresh:
            if len(picked) >= max_clips:
                break
            key = f"{c.provider}:{c.id}"
            if key in checked:
                continue
            checked.add(key)
            if not check(client, c):
                rejected += 1
                continue
            picked.append(c)
            used.add(key)
    if rejected:
        log.info("Skipped %d clip(s) showing faces for %s", rejected, keywords)
    if not picked:
        if errored and not searched:
            raise BrollUnavailable(f"stock search failed {errored}× for {keywords} — nothing looked up")
        raise BrollError(f"no usable faceless clips for {keywords}")
    return picked


def download(cfg: Config, client: httpx.Client, clip: Clip) -> Clip:
    # The provider's id names the file (S3): only a plain token may reach the path, and a clip is a video of
    # bounded size — an HTML error page or a multi-GB file is refused before it lands in assets/stock.
    if not (SAFE_ID.fullmatch(clip.provider) and SAFE_ID.fullmatch(clip.id)):
        raise BrollError(f"refusing clip with unsafe id {clip.provider!r}:{clip.id!r}")
    stock = cfg.root / "assets" / "stock"
    stock.mkdir(parents=True, exist_ok=True)
    dest = stock / f"{clip.provider}_{clip.id}.{'jpg' if clip.still else 'mp4'}"
    if not dest.exists() or dest.stat().st_size == 0:
        part = dest.with_suffix(".part")
        try:
            with client.stream("GET", clip.url, follow_redirects=True, timeout=120) as resp:
                if resp.status_code >= 400:
                    raise BrollError(f"download {clip.provider}:{clip.id} failed: HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                want = "image/" if clip.still else "video/"
                if ctype and not (ctype.startswith(want) or ctype == "application/octet-stream"):
                    raise BrollError(f"download {clip.provider}:{clip.id} is not a {want[:-1]} ({ctype})")
                declared = int(resp.headers.get("content-length") or 0)
                if declared > MAX_CLIP_BYTES:
                    raise BrollError(f"download {clip.provider}:{clip.id} too big ({declared / 2**20:.0f} MB)")
                size = 0
                with open(part, "wb") as f:
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > MAX_CLIP_BYTES:
                            raise BrollError(f"download {clip.provider}:{clip.id} exceeded {MAX_CLIP_BYTES >> 20} MB")
                        f.write(chunk)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        part.rename(dest)
    clip.path = str(dest.relative_to(cfg.root))
    return clip


def manifest_entry(clip: Clip, beat: int, start: float, dur: float) -> dict[str, Any]:
    d = asdict(clip)
    d.pop("url")
    return {**d, "beat": beat, "at": round(start, 3), "seconds": round(dur, 3)}
