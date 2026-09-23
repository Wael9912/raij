"""Yesterday's most-viewed Arabic Wikipedia articles (Wikimedia REST pageviews API, keyless, CC0).

What Arabic readers looked up en masse is a trend signal the Google Trends feeds miss (people, events, terms
from TV). Navigation pages and special namespaces are dropped; the article itself is the source text later.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from src.config import Config
from src.discover.common import Candidate, FetchError, SourceResult, iso_utc, request

log = logging.getLogger("raij.discover.wiki")

TOP = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{lang}.wikipedia.org/all-access/{y}/{m}/{d}"
UA = {"User-Agent": "raij/0.1 (https://github.com/Wael9912/raij; Arabic video channel; trend discovery)"}   # Wikimedia needs a contact URL
SKIP_PREFIXES = ("خاص:", "Special:", "ويكيبيديا:", "Wikipedia:", "ملف:", "File:", "تصنيف:", "Category:",
                 "بوابة:", "Portal:", "قالب:", "Template:", "مساعدة:", "Help:", "نقاش:", "Talk:", "مستخدم:", "User:")
SKIP_TITLES = {"الصفحة_الرئيسية", "Main_Page", "-", "بحث"}


def parse_top(payload: dict, lang: str, day: str, limit: int) -> list[Candidate]:
    out = []
    for art in (payload.get("items") or [{}])[0].get("articles") or []:
        title = str(art.get("article") or "")
        if not title or title in SKIP_TITLES or title.startswith(SKIP_PREFIXES) or ":" in title.split("_")[0]:
            continue
        out.append(Candidate(
            source="wiki", external_id=f"{lang}:{title}",
            canonical_url=f"https://{lang}.wikipedia.org/wiki/{quote(title)}",
            title=title.replace("_", " "), views=int(art.get("views") or 0),
            published_at=iso_utc(datetime.strptime(day, "%Y/%m/%d")), region="AR" if lang == "ar" else "US",
            raw={"rank": art.get("rank"), "day": day, "lang": lang}))
        if len(out) >= limit:
            break
    return out


def fetch(cfg: Config, client: httpx.Client) -> SourceResult:
    lang = str(cfg.get("discovery.wikipedia.lang", "ar"))
    limit = int(cfg.get("discovery.wikipedia.top", 40))
    result = SourceResult()
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    for back in (0, 1):                                  # the day may not be published yet early in the day
        day = yesterday - timedelta(days=back)
        url = TOP.format(lang=lang, y=day.strftime("%Y"), m=day.strftime("%m"), d=day.strftime("%d"))
        try:
            resp = request(client, "GET", url, headers=UA, retries=1)
        except FetchError as exc:
            if "HTTP 404" in str(exc) and back == 0:
                continue
            result.errors.append(str(exc))
            log.warning("Wikipedia top views failed: %s", exc)
            return result
        try:
            result.candidates = parse_top(resp.json(), lang, day.strftime("%Y/%m/%d"), limit)
        except (ValueError, KeyError) as exc:
            result.errors.append(f"bad payload: {exc}")
            return result
        log.info("Wikipedia %s top views %s: %d articles", lang, day.strftime("%Y-%m-%d"), len(result.candidates))
        return result
    return result
