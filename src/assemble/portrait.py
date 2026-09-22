"""Licensed photos of public figures, from Wikimedia Commons.

B-roll is faceless, so when a beat is about a real public figure we show their actual photo. News
photos (AP, Getty…) are copyrighted, so the only source is the lead image of their English
Wikipedia article, and only if it is hosted on Commons under a free licence (public domain, CC0,
CC BY, CC BY-SA). Wikipedia's locally hosted "fair use" images are refused. CC BY/BY-SA require
credit: it is drawn on the frame and recorded in the manifest for the post caption.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from src.assemble import brand
from src.config import Config
from src.discover.common import FetchError, request

log = logging.getLogger("raij.assemble")

WIKI_API = "https://en.wikipedia.org/w/api.php"
# Wikimedia asks API clients for a descriptive User-Agent.
UA = {"User-Agent": "raij/0.1 (Arabic news shorts; licensed Commons photos with attribution)"}
FREE = re.compile(r"^(public domain|pd\b.*|cc0.*|cc by(-sa)? [\d.]+.*)$", re.I)
W, H = 1080, 1920


@dataclass
class Photo:
    person: str
    title: str             # Wikipedia article
    file: str              # Commons file name
    url: str               # download URL (scaled)
    page: str              # Commons file page
    author: str
    license: str
    path: str = ""         # repo-relative, once downloaded

    @property
    def credit(self) -> str:
        return f"Photo: {self.author} / {self.license} via Wikimedia Commons"


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text or ""))).strip()


def lookup(client: httpx.Client, person: str) -> Photo | None:
    """Freely licensed Commons photo for `person`, or None (no article, disambiguation, no image,
    or the image isn't free)."""
    q = request(client, "GET", WIKI_API, headers=UA, params={
        "action": "query", "format": "json", "titles": person, "redirects": 1,
        "prop": "pageimages|pageprops", "piprop": "name", "ppprop": "disambiguation"}).json()
    page = next(iter(q.get("query", {}).get("pages", {}).values()), {})
    if "missing" in page or "disambiguation" in page.get("pageprops", {}) or not page.get("pageimage"):
        return None
    info = request(client, "GET", WIKI_API, headers=UA, params={
        "action": "query", "format": "json", "titles": f"File:{page['pageimage']}", "prop": "imageinfo",
        "iiprop": "url|extmetadata", "iiurlwidth": W,
        "iiextmetadatafilter": "LicenseShortName|Artist|NonFree"}).json()
    file_page = next(iter(info.get("query", {}).get("pages", {}).values()), {})
    if file_page.get("imagerepository") != "shared":          # local file = Wikipedia fair use
        return None
    ii = (file_page.get("imageinfo") or [{}])[0]
    meta = ii.get("extmetadata", {})
    licence = _plain(meta.get("LicenseShortName", {}).get("value", ""))
    if str(meta.get("NonFree", {}).get("value", "")).lower() == "true" or not FREE.match(licence):
        log.info("Photo of %s skipped: licence %r is not free", person, licence)
        return None
    return Photo(person, page["title"], page["pageimage"], ii.get("thumburl") or ii.get("url", ""),
                 ii.get("descriptionurl", ""), _plain(meta.get("Artist", {}).get("value", "")) or "Unknown",
                 licence)


def download(cfg: Config, client: httpx.Client, photo: Photo) -> Photo:
    stock = cfg.root / "assets" / "stock"
    stock.mkdir(parents=True, exist_ok=True)
    dest = stock / f"wikimedia_{hashlib.sha1(photo.file.encode()).hexdigest()[:12]}.jpg"
    if not dest.exists() or dest.stat().st_size == 0:
        resp = request(client, "GET", photo.url, headers=UA)
        dest.write_bytes(resp.content)
    photo.path = str(dest.relative_to(cfg.root))
    return photo


def compose(src: Path, credit: str, out: Path) -> Path:
    """1080×1920 frame: the photo, uncropped, over a blurred darkened fill of itself, with the credit
    in small type near the top (clear of the subtitle band)."""
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    bg = ImageOps.fit(img, (W, H)).filter(ImageFilter.GaussianBlur(40))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    fg = ImageOps.contain(img, (980, 1150))
    bg.paste(fg, ((W - fg.width) // 2, 140 + (1150 - fg.height) // 2))
    d = ImageDraw.Draw(bg)
    font = brand.font(26, weight=700)
    text = credit if font.getlength(credit) <= W - 60 else credit[:90] + "…"
    d.text((W / 2, 100), text, font=font, fill=(235, 235, 235), anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.save(out, quality=92)
    return out


def find(cfg: Config, client: httpx.Client, person: str) -> Photo | None:
    """lookup + download, never raising: a missing photo just means faceless b-roll for that beat."""
    try:
        photo = lookup(client, person)
        return download(cfg, client, photo) if photo else None
    except (FetchError, ValueError, KeyError, OSError) as exc:
        log.warning("Photo lookup for %s failed: %s", person, exc)
        return None
