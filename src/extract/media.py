"""Real pictures and footage of the story, found on its own source pages (Phase 20).

Owner's rule (2026-09-25): the main subject must be shown with real photos and video from the sources; free
stock clips only fill the remaining time. This module runs inside `extract`, right after the story card, and
records *where* the media is (`stories.media`, JSON); nothing is downloaded here — `assemble.sourcemedia` fetches
and frames the items when a video is rendered.

What counts as source media, in priority order:
  1. the candidate's own video when the source is YouTube;
  2. videos embedded in the articles read (og:video, <video>/<source>, JSON-LD VideoObject, YouTube iframes);
  3. the articles' lead image (og:image / twitter:image / JSON-LD image) and the in-article images;
  4. a YouTube search for the story (`media.youtube_search`), kept only when the title clearly names the topic.
Avatars, logos, icons, tracking pixels and tiny images are dropped by attribute heuristics here and by pixel
size later.
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
import subprocess
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from src import formats
from src.config import Config
from src.discover.common import FetchError
from src.discover.manual import relevant
from src.extract.sources import RunCmd, fetch_html, run_cmd

log = logging.getLogger("raij.extract")

YT_ID = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?(?:.*&)?v=|shorts/|v/|live/)|youtu\.be/)"
                   r"([A-Za-z0-9_-]{11})")
# Anything in the URL, class, id or alt text that marks decoration rather than a picture of the story.
SKIP = re.compile(r"(avatar|logo|icon|author|byline|profile|headshot|sprite|badge|placeholder|spinner|blank|"
                  r"pixel|tracking|emoji|button|advert|/ads?/|1x1|widget|share|social|newsletter|subscribe|"
                  r"comment|related|promo|thumbnail-small|favicon|qrcode|signature|analytics|/collect\?|"
                  r"doubleclick|beacon)", re.I)
BAD_EXT = re.compile(r"\.(svg|gif|ico|bmp)(\?|$)", re.I)
VIDEO_EXT = re.compile(r"\.(mp4|m4v|webm|mov|m3u8)(\?|$)", re.I)
MIN_ATTR_PX = 300           # <img width=…> below this is a thumbnail or an icon
MIN_SIZES_PX = 250          # `sizes="125px"` → the page shows it small (an author avatar with a 2400w srcset)


class _Page(HTMLParser):
    """Collects the tags that can carry media; nothing else is kept."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.imgs: list[dict[str, str]] = []
        self.videos: list[str] = []
        self.iframes: list[str] = []
        self.ldjson: list[str] = []
        self._in_ld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, []).append(a["content"])
        elif tag == "img":
            self.imgs.append(a)
        elif tag in ("video", "source"):
            src = a.get("src") or a.get("data-src") or ""
            if src and (tag == "video" or (a.get("type") or "").startswith("video") or VIDEO_EXT.search(src)):
                self.videos.append(src)
        elif tag == "iframe":
            src = a.get("src") or a.get("data-src") or ""
            if src:
                self.iframes.append(src)
        elif tag in ("lite-youtube", "youtube-video") and a.get("videoid"):
            self.iframes.append(f"https://www.youtube.com/watch?v={a['videoid']}")
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_ld, self._buf = True, []

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.ldjson.append("".join(self._buf))


def _largest_srcset(srcset: str) -> str | None:
    best, best_w = None, -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1 and bits[1].endswith("w") and bits[1][:-1].isdigit():
            w = int(bits[1][:-1])
        elif len(bits) > 1 and bits[1].endswith("x"):
            try:
                w = int(float(bits[1][:-1]) * 1000)
            except ValueError:
                w = 0
        if w > best_w:
            best, best_w = bits[0], w
    return best


def _px(value: str | None) -> int | None:
    if not value:
        return None
    m = re.match(r"\s*(\d+)", value)
    return int(m.group(1)) if m else None


def _img_url(a: dict[str, str]) -> str | None:
    """The biggest URL an <img> offers, or None when its attributes say it is decoration."""
    for k in ("width", "height"):
        px = _px(a.get(k))
        if px is not None and px < MIN_ATTR_PX:
            return None
    sizes = [int(x) for x in re.findall(r"(\d+)px", a.get("sizes") or "")]
    if sizes and max(sizes) < MIN_SIZES_PX:
        return None
    marker = " ".join((a.get("src") or "", a.get("class") or "", a.get("id") or "", a.get("alt") or "",
                       a.get("data-src") or ""))
    if SKIP.search(marker) or re.search(r"display\s*:\s*none", a.get("style") or ""):
        return None
    for key in ("srcset", "data-srcset"):
        if a.get(key):
            url = _largest_srcset(a[key])
            if url:
                return url
    for key in ("data-src", "data-lazy-src", "data-original", "src"):
        if a.get(key) and not a[key].startswith("data:"):
            return a[key]
    return None


def _ld_media(blob: str) -> tuple[list[str], list[str], list[str]]:
    """(image URLs, video URLs, YouTube embed URLs) from a JSON-LD block; tolerant of junk."""
    try:
        data = json.loads(blob.strip())
    except ValueError:
        return [], [], []
    images: list[str] = []
    videos: list[str] = []
    embeds: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, list):
            for n in node:
                walk(n, depth + 1)
        elif isinstance(node, dict):
            t = str(node.get("@type") or "")
            if t == "ImageObject" and node.get("url"):
                images.append(str(node["url"]))
            elif t == "VideoObject":
                if node.get("contentUrl"):
                    videos.append(str(node["contentUrl"]))
                if node.get("embedUrl"):
                    embeds.append(str(node["embedUrl"]))
            for k, v in node.items():
                if k in ("image", "thumbnailUrl") and isinstance(v, str):
                    images.append(v)
                elif k in ("image", "video", "associatedMedia", "@graph", "mainEntity", "itemListElement"):
                    walk(v, depth + 1)
    walk(data)
    return images, videos, embeds


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _key(url: str) -> str:
    """Dedupe key: same path is the same picture, whatever the crop/size query — except YouTube, whose id *is*
    the query."""
    s = urlsplit(url)
    if "youtube" in s.netloc or "youtu.be" in s.netloc:
        return url
    return f"{s.netloc.lower()}{s.path}"


def page_media(html_text: str, page_url: str, max_images: int = 6) -> list[dict[str, Any]]:
    """Media items on one page: videos and YouTube embeds first, then the lead image, then in-article images."""
    p = _Page()
    try:
        p.feed(html_text)
    except Exception as exc:                                     # a broken page never breaks extract
        log.debug("media parse failed for %s: %s", page_url, exc)
    src = _domain(page_url)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, url: str, **extra: Any) -> None:
        url = htmllib.unescape(url.strip())
        if not url or url.startswith("data:"):
            return
        url = urljoin(page_url, url)
        if not url.startswith("http"):
            return
        if kind == "youtube":
            m = YT_ID.search(url)
            if not m:
                return
            url = f"https://www.youtube.com/watch?v={m.group(1)}"
        k = f"{kind}:{_key(url)}"
        if k in seen:
            return
        seen.add(k)
        items.append({"kind": kind, "url": url, "page": page_url, "source": src, **extra})

    lead_w = _px((p.meta.get("og:image:width") or [None])[0])
    lead_h = _px((p.meta.get("og:image:height") or [None])[0])
    ld_images: list[str] = []
    ld_videos: list[str] = []
    ld_embeds: list[str] = []
    for blob in p.ldjson:
        i, v, e = _ld_media(blob)
        ld_images += i
        ld_videos += v
        ld_embeds += e

    for key in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        for url in p.meta.get(key, []):
            if YT_ID.search(url):
                add("youtube", url)
            elif VIDEO_EXT.search(url):
                add("video", url)
    for url in p.videos + ld_videos:
        if YT_ID.search(url):
            add("youtube", url)
        elif VIDEO_EXT.search(url):
            add("video", url)
    for url in p.iframes + ld_embeds + p.meta.get("twitter:player", []):
        if YT_ID.search(url):
            add("youtube", url)

    images: list[tuple[str, dict[str, Any]]] = []
    alt = (p.meta.get("og:image:alt") or p.meta.get("twitter:image:alt") or [""])[0]
    for key in ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src"):
        for url in p.meta.get(key, []):
            images.append((url, {"lead": True, "alt": alt, "w": lead_w, "h": lead_h}))
    for url in ld_images:
        images.append((url, {"lead": True}))
    for a in p.imgs:
        url = _img_url(a)
        if url:
            images.append((url, {"alt": (a.get("alt") or "")[:120]}))
    n_images = 0
    for url, extra in images:
        if n_images >= max_images:
            break
        if BAD_EXT.search(url):
            continue
        before = len(items)
        add("image", url, **{k: v for k, v in extra.items() if v})
        n_images += len(items) - before
    return items


# --- YouTube search -------------------------------------------------------------

def yt_search(query: str, run: RunCmd = run_cmd, limit: int = 6, max_seconds: float = 900,
              min_seconds: float = 20) -> list[dict[str, Any]]:
    """Recent-ish YouTube videos about `query` (yt-dlp's flat search, keyless), only those whose title names the
    topic, most viewed first. [{kind: youtube, url, title, duration, source, channel}]"""
    if not query.strip():
        return []
    proc = run(["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings", f"ytsearch{limit}:{query}"])
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        log.info("YouTube search failed for %r: %s", query, (proc.stderr or "").strip()[-160:])
        return []
    found = []
    for line in (proc.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        dur = float(d.get("duration") or 0)
        title = str(d.get("title") or "")
        if not d.get("id") or not (min_seconds <= dur <= max_seconds) or not relevant(title, query):
            continue
        if d.get("live_status") in ("is_live", "is_upcoming"):
            continue
        found.append({"kind": "youtube", "url": f"https://www.youtube.com/watch?v={d['id']}", "title": title[:120],
                      "duration": dur, "source": "youtube.com", "channel": str(d.get("channel") or d.get("uploader") or ""),
                      "views": int(d.get("view_count") or 0), "search": True})
    found.sort(key=lambda x: -x["views"])
    return found


def search_query(row: dict[str, Any]) -> str:
    """What to search video for: the trend's first headline (a bare trend term like «حمار» finds nothing useful),
    the owner's topic text, else the candidate title."""
    raw = json.loads(row.get("raw_json") or "{}")
    if row.get("source") == "trends":
        heads = [n.get("title") for n in raw.get("news") or [] if n.get("title")]
        if heads:
            return str(heads[0])
    if row.get("source") == "manual":
        text = (formats.wanted(row).get("text") or "").strip()
        if text and formats.wanted(row).get("kind") == "topic":
            return text
        if text:                                    # an owner script: its first line names the story
            return text.splitlines()[0][:120]
    return str(row.get("title") or "")


# --- entry point -----------------------------------------------------------------

def collect(cfg: Config, client: httpx.Client, row: dict[str, Any], urls: list[str],
            run: RunCmd = run_cmd) -> list[dict[str, Any]]:
    """All source media for a candidate, in priority order, capped by `media.max_items`. Never raises."""
    if not cfg.get("media.enabled", True):
        return []
    max_items = int(cfg.get("media.max_items", 10))
    max_pages = int(cfg.get("media.max_pages", 4))
    per_page = int(cfg.get("media.max_images_per_page", 6))
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(item: dict[str, Any]) -> None:
        k = f"{item['kind']}:{_key(item['url'])}"
        if k not in seen:
            seen.add(k)
            items.append(item)

    if row.get("source") == "youtube" and row.get("canonical_url"):
        push({"kind": "youtube", "url": row["canonical_url"], "page": row["canonical_url"], "source": "youtube.com",
              "title": row.get("title") or "", "own": True})
    pages = [u for u in urls if u and u.startswith("http") and "youtube.com" not in u and "youtu.be" not in u]
    if row.get("source") == "rss" and row.get("canonical_url") and row["canonical_url"] not in pages:
        pages.insert(0, row["canonical_url"])
    for url in pages[:max_pages]:
        try:
            html_text = fetch_html(client, url)
        except (FetchError, httpx.HTTPError) as exc:
            log.info("media: page %s unreadable: %s", url, exc)
            continue
        for item in page_media(html_text, url, max_images=per_page):
            push(item)
    if cfg.get("media.youtube_search", True):
        query = search_query(row)
        try:
            hits = yt_search(query, run=run, limit=int(cfg.get("media.youtube_search_limit", 6)),
                             max_seconds=float(cfg.get("media.max_video_seconds", 900)))
        except (OSError, subprocess.SubprocessError) as exc:     # yt-dlp missing or hung
            log.info("media: YouTube search unavailable: %s", exc)
            hits = []
        for hit in hits[: int(cfg.get("media.youtube_results", 2))]:
            push(hit)
    # Videos and lead images first, then the rest, then search results — the assembler takes them in order.
    def priority(it: dict[str, Any]) -> int:
        if it.get("search"):
            return 3
        if it["kind"] != "image":
            return 0
        return 1 if it.get("lead") else 2
    items.sort(key=priority)
    kept = items[:max_items]
    log.info("media: %d item(s) for #%s — %s", len(kept), row.get("id"),
             ", ".join(f"{it['kind']}@{it['source']}" for it in kept) or "none")
    return kept

