"""Owner-provided work (Phase 13): a topic to research or a finished script to voice, sent from Telegram
(`/topic`, `/script`) or the CLI (`topic`, `script`). Both become `candidates` rows with source `manual`,
already `selected`, carrying the ask in `candidates.wanted`, so the normal extract → script → voice →
assemble → review stages produce them like any trending pick.

Research for a topic is keyless: Bing News RSS search (Arabic + English editions; publisher links the extract
stage reads with trafilatura), Google News RSS for headlines when Bing has nothing, and Wikipedia's plain-text
extract for background — only when the article title names the topic.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

import httpx

from src import formats
from src.config import Config
from src.discover.common import FetchError, request

log = logging.getLogger("raij.discover.manual")

# Bing News RSS links carry the publisher URL in `url=` — readable by trafilatura. Google News RSS links are
# JavaScript interstitials (nothing to extract), so it is only the fallback for headlines.
BING_RSS = "https://www.bing.com/news/search?q={q}&format=rss&setlang={lang}&cc={gl}"
NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{lang}"
WIKI_API = "https://{lang}.wikipedia.org/w/api.php"
EDITIONS = (("ar", "SA"), ("en", "US"))
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Safari/537.36")
WIKI_UA = {"User-Agent": "raij/0.1 (https://github.com/Wael9912/raij; Arabic video channel; topic research)"}   # Wikimedia needs a contact URL
_ARABIC = re.compile(r"[؀-ۿ]")
_STOP = {"لماذا", "كيف", "ماذا", "هل", "هذا", "هذه", "العام", "عام", "في", "من", "إلى", "على", "عن", "ما", "مع",
         "the", "why", "how", "what", "is", "are", "of", "in", "to", "a", "an", "this", "year", "and", "for"}
MAX_TEXT = 6000                     # a pasted script; Telegram messages are ≤4096 chars anyway


def _stamp(cfg: Config) -> str:
    return datetime.now(ZoneInfo(cfg.get("schedule.timezone", "Africa/Cairo"))).strftime("%Y-%m-%d %H:%M:%S")


def _external_id(kind: str, text: str) -> str:
    return f"{kind}:{hashlib.sha1(text.strip().encode('utf-8')).hexdigest()[:16]}"


def title_of(text: str, max_words: int = 14) -> str:
    """First sentence/line of a text, trimmed, as the candidate title."""
    first = re.split(r"[\n.!؟?]", text.strip(), maxsplit=1)[0].strip()
    words = first.split()
    return " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")


def add_topic(cfg: Config, conn: sqlite3.Connection, text: str, fmts: list[str], platforms: list[str],
              category: str | None = None) -> int:
    """A topic (any language, a sentence or a few words) → a selected manual candidate. Returns its id.
    Re-sending the same topic reuses the row (and re-selects it if it had finished or failed)."""
    text = " ".join(text.split())[:500]
    if not text:
        raise ValueError("empty topic")
    ext = _external_id("topic", text.lower())
    wanted = formats.encode(fmts, platforms, kind="topic", text=text)
    return _upsert(cfg, conn, ext, text, wanted, category, raw={"kind": "topic", "text": text})


def add_script(cfg: Config, conn: sqlite3.Connection, text: str, platforms: list[str], kind: str | None = None,
               category: str | None = None) -> tuple[int, str]:
    """An owner-written script → a selected manual candidate whose wanted.text is voiced as written (the script
    stage only splits it into beats and adds b-roll keywords). The format follows the word count unless given.
    Returns (candidate id, kind)."""
    text = text.strip()[:MAX_TEXT]
    if len(text.split()) < 12:
        raise ValueError("a script needs at least a dozen words")
    kind = kind if kind in formats.KINDS else formats.kind_for_words(cfg, len(text.split()))
    ext = _external_id("script", text)
    wanted = formats.encode([kind], platforms, kind="script", text=text)
    cid = _upsert(cfg, conn, ext, title_of(text), wanted, category, raw={"kind": "script", "words": len(text.split())})
    return cid, kind


def _upsert(cfg: Config, conn: sqlite3.Connection, ext: str, title: str, wanted: str, category: str | None,
            raw: dict[str, Any]) -> int:
    """One row per ask. The same text sent again while the first is still in progress (selected/extracted) only
    updates the ask; after that it is a *new* row (suffix :2, :3 …) — the earlier story, scripts and cards stay
    as history and never block the new production."""
    stamp = _stamp(cfg)
    rows = conn.execute("SELECT id, status, external_id FROM candidates WHERE source = 'manual' "
                        "AND (external_id = ? OR external_id LIKE ?) ORDER BY id", (ext, ext + ":%")).fetchall()
    with conn:
        for row in rows:
            if row["status"] in ("selected", "extracted"):
                conn.execute("UPDATE candidates SET selected_at = ?, wanted = ?, category = coalesce(?, category), "
                             "last_seen_at = datetime('now') WHERE id = ?", (stamp, wanted, category, row["id"]))
                return int(row["id"])
        if rows:
            ext = f"{ext}:{len(rows) + 1}"
        url = f"manual:{ext}"
        cur = conn.execute(
            "INSERT INTO candidates (source, external_id, canonical_url, title, region, raw_json, category, "
            "retellable, ad_safe, audience_fit, status, selected_at, wanted) "
            "VALUES ('manual', ?, ?, ?, 'AR', ?, ?, 1, 1, 5, 'selected', ?, ?)",
            (ext, url, title, json.dumps(raw, ensure_ascii=False), category, stamp, wanted))
        return int(cur.lastrowid)


# --- research -------------------------------------------------------------------------

def _clean(text: str | None) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def keywords(text: str) -> list[str]:
    """Content words of a topic (stopwords and short tokens dropped, Arabic ال/و prefixes stripped) — the search
    query, and the relevance check for what comes back."""
    out = []
    for w in re.findall(r"[\w\u0600-\u06FF]+", text):
        w = w.strip("ـ").lower()
        if w in _STOP or len(w) < 3:
            continue
        core = re.sub(r"^(و|ف|ب|ل|ك)?(ال)", "", w) if _ARABIC.search(w) else w
        if len(core) >= 3:
            out.append(core)
    return list(dict.fromkeys(out))


def relevant(text: str, query: str) -> bool:
    """At least one of the topic's content words appears in `text` (a title)."""
    kws = keywords(query)
    if not kws:
        return True
    hay = " ".join(keywords(text)) + " " + text.lower()
    return any(k in hay for k in kws)


def _publisher_url(link: str) -> str:
    """Bing's apiclick redirect carries the real URL in `url=`; anything else is returned as is."""
    parts = urlsplit(link)
    if parts.hostname and parts.hostname.endswith("bing.com"):
        real = parse_qs(parts.query).get("url")
        if real:
            return real[0]
    return link


def _parse_items(root: ET.Element, seen: set[str], limit: int, out: list[dict[str, str]]) -> None:
    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        link = _publisher_url((item.findtext("link") or "").strip())
        if not title or not link or link in seen:
            continue
        seen.add(link)
        src = next((e for e in item if e.tag.endswith("source") or e.tag.endswith("Source")), None)
        out.append({"title": title, "url": link, "source": _clean(src.text if src is not None else "") or
                    (urlsplit(link).hostname or "").removeprefix("www."),
                    "published": (item.findtext("pubDate") or "").strip(),
                    "readable": "news.google.com" not in link})
        if len(out) >= limit:
            return


def news_search(client: httpx.Client, query: str, limit: int = 8) -> list[dict[str, str]]:
    """Recent articles about `query`: Bing News RSS (direct publisher links), the Arabic edition first for an
    Arabic query; Google News RSS as a fallback for headlines only (its links can't be read).
    Returns [{title, url, source, published, readable}]. The query is reduced to its content words — a full
    question ("why did gold hit a record this year") finds far less than "gold record"."""
    q = " ".join(keywords(query)[:6]) or query
    order = EDITIONS if _ARABIC.search(query) else tuple(reversed(EDITIONS))
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for lang, gl in order:
        try:
            resp = request(client, "GET", BING_RSS.format(q=quote(q), lang=lang, gl=gl), retries=1,
                           headers={"User-Agent": BROWSER_UA})
            _parse_items(ET.fromstring(resp.content), seen, limit, out)
        except (FetchError, ET.ParseError) as exc:
            log.warning("Bing news search (%s) failed: %s", lang, exc)
        if len(out) >= limit:
            return out
    if not out:
        for lang, gl in order:
            url = NEWS_RSS.format(q=quote(q), hl=f"{lang}-{gl}" if lang == "en" else lang, gl=gl, lang=lang)
            try:
                resp = request(client, "GET", url, retries=1)
                _parse_items(ET.fromstring(resp.content), seen, limit, out)
            except (FetchError, ET.ParseError) as exc:
                log.warning("Google news search (%s) failed: %s", lang, exc)
            if len(out) >= limit:
                break
    return out


def wikipedia_extract(client: httpx.Client, query: str, max_chars: int = 6000) -> tuple[str, str] | None:
    """Plain-text extract of the best-matching Wikipedia article (Arabic for Arabic queries, else English) —
    only if its title actually names the topic (a loose search once returned the Yemen war for "gold price");
    (text, article URL) or None."""
    lang = "ar" if _ARABIC.search(query) else "en"
    q = " ".join(keywords(query)[:5]) or query
    try:
        found = request(client, "GET", WIKI_API.format(lang=lang), retries=1, headers=WIKI_UA, params={
            "action": "query", "list": "search", "srsearch": q, "srlimit": 3, "format": "json"}).json()
        hits = [h for h in found.get("query", {}).get("search") or [] if relevant(str(h.get("title") or ""), query)]
        if not hits:
            return None
        title = hits[0]["title"]
        page = request(client, "GET", WIKI_API.format(lang=lang), retries=1, headers=WIKI_UA, params={
            "action": "query", "prop": "extracts", "explaintext": 1, "exsectionformat": "plain",
            "titles": title, "format": "json", "redirects": 1}).json()
        p = next(iter(page.get("query", {}).get("pages", {}).values()), {})
        text = (p.get("extract") or "").strip()
    except (FetchError, ValueError, KeyError, TypeError) as exc:
        log.warning("Wikipedia lookup for %r failed: %s", query, exc)
        return None
    if len(text) < 200:
        return None
    return text[:max_chars], f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
