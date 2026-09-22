"""Source text for a selected candidate: article text, news bundle, Reddit selftext, or YouTube
transcript. Media is only ever downloaded into a temp dir that is deleted before returning.
"""
from __future__ import annotations

import html
import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
import trafilatura

from src.config import Config
from src.discover.common import FetchError, request

log = logging.getLogger("raij.extract")
# trafilatura logs every page it can't parse at ERROR/WARNING; we report failures ourselves.
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)

# News sites often 403 an unknown bot UA; articles are fetched once, for summarizing only.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
SUB_LANGS = ("ar", "en")

RunCmd = Callable[[list[str]], subprocess.CompletedProcess]


class ExtractError(RuntimeError):
    """No usable source text for this candidate."""


@dataclass
class SourceText:
    text: str
    src: str                                   # autosubs|whisper|article|news|selftext|summary
    urls: list[str] = field(default_factory=list)


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


# --- articles ----------------------------------------------------------------

def article_text(client: httpx.Client, url: str) -> str:
    """Main text of a web article ('' if the page has none, e.g. a paywall or video page)."""
    resp = request(client, "GET", url, retries=1, headers={"User-Agent": BROWSER_UA},
                   follow_redirects=True)
    text = trafilatura.extract(resp.text, url=url, include_comments=False, include_tables=False)
    return (text or "").strip()


def _from_rss(cfg: Config, client: httpx.Client, row: dict[str, Any], raw: dict[str, Any]) -> SourceText:
    url = row["canonical_url"]
    try:
        text = article_text(client, url)
    except FetchError as exc:
        log.warning("Article fetch failed for #%s: %s", row["id"], exc)
        text = ""
    if len(text) >= cfg.get("extract.min_article_chars", 400):
        return SourceText(text, "article", [url])
    summary = html.unescape(re.sub(r"<[^>]+>", " ", raw.get("summary") or "")).strip()
    return SourceText(f"{row['title'] or ''}\n\n{summary}".strip(), "summary", [url])


def _from_trends(cfg: Config, client: httpx.Client, row: dict[str, Any], raw: dict[str, Any]) -> SourceText:
    min_chars = cfg.get("extract.min_article_chars", 400)
    parts, urls, errors = [], [], []
    for item in raw.get("news") or []:
        if len(urls) >= cfg.get("extract.max_articles", 3):
            break
        if not item.get("url"):
            continue
        try:
            text = article_text(client, item["url"])
        except FetchError as exc:
            errors.append(str(exc))
            continue
        if len(text) < min_chars:
            continue
        parts.append(f"[{item.get('source') or 'source'}] {item.get('title') or ''}\n{text}")
        urls.append(item["url"])
    if errors:
        log.warning("Trends #%s: %d article fetch(es) failed: %s", row["id"], len(errors), "; ".join(errors))
    if parts:
        return SourceText("\n\n---\n\n".join(parts), "news", urls)
    # No article was readable; headlines alone are thin, but name the story.
    heads = [f"[{n.get('source') or 'source'}] {n['title']}" for n in raw.get("news") or [] if n.get("title")]
    return SourceText("\n".join([f"Search term: {row['title']}"] + heads), "headlines",
                      [n["url"] for n in raw.get("news") or [] if n.get("url")])


def _from_reddit(cfg: Config, client: httpx.Client, row: dict[str, Any], raw: dict[str, Any]) -> SourceText:
    selftext = (raw.get("selftext") or "").strip()
    if len(selftext) >= cfg.get("extract.min_article_chars", 400):
        return SourceText(f"{row['title']}\n\n{selftext}", "selftext", [row["canonical_url"]])
    linked = raw.get("linked_url") or ""
    if linked.startswith("http") and "reddit.com" not in linked and "redd.it" not in linked:
        try:
            text = article_text(client, linked)
        except FetchError as exc:
            log.warning("Reddit #%s linked article failed: %s", row["id"], exc)
            text = ""
        if text:
            return SourceText(f"{row['title']}\n\n{text}", "article", [linked])
    return SourceText(f"{row['title']}\n\n{selftext}".strip(), "selftext", [row["canonical_url"]])


# --- YouTube -----------------------------------------------------------------

_VTT_TIMING = re.compile(r"^\d{2}:\d{2}(:\d{2})?\.\d{3}\s+-->")
_VTT_TAG = re.compile(r"<[^>]+>")


def parse_vtt(vtt: str) -> str:
    """WebVTT → plain text. YouTube auto-subs repeat each line as it scrolls, so consecutive
    duplicates are dropped."""
    lines: list[str] = []
    in_cues = False                            # everything before the first cue timing is header
    for raw in vtt.splitlines():
        line = raw.strip()
        if _VTT_TIMING.match(line):
            in_cues = True
            continue
        if not in_cues or not line or line.isdigit() or line.startswith(("NOTE", "STYLE")):
            continue
        text = html.unescape(_VTT_TAG.sub("", line)).strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return " ".join(lines)


def youtube_subs(url: str, run: RunCmd = run_cmd) -> str:
    """Auto/manual subtitles (Arabic preferred, else English) as plain text; '' if none."""
    with tempfile.TemporaryDirectory(prefix="raij-subs-") as tmp:
        run(["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs",
             "--sub-langs", ",".join(SUB_LANGS), "--sub-format", "vtt", "--no-playlist",
             "-o", str(Path(tmp) / "subs.%(ext)s"), url])
        for lang in SUB_LANGS:
            for path in sorted(Path(tmp).glob(f"subs.{lang}*.vtt")):
                text = parse_vtt(path.read_text(encoding="utf-8", errors="replace"))
                if text:
                    return text
    return ""


def _whisper(audio: Path, model_size: str) -> str:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ExtractError("no subtitles and faster-whisper is not installed (uv sync --group whisper)") from None
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio), vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def youtube_whisper(url: str, model_size: str = "small", run: RunCmd = run_cmd,
                    transcribe: Callable[[Path, str], str] | None = None) -> str:
    """Download audio to a temp dir, transcribe, and always delete the audio."""
    tmp = Path(tempfile.mkdtemp(prefix="raij-audio-"))
    try:
        proc = run(["yt-dlp", "-f", "bestaudio", "--no-playlist", "-o", str(tmp / "audio.%(ext)s"), url])
        files = [p for p in tmp.iterdir() if p.name.startswith("audio.")]
        if not files:
            raise ExtractError(f"audio download failed: {(proc.stderr or '').strip()[-200:]}")
        return (transcribe or _whisper)(files[0], model_size)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _from_youtube(cfg: Config, client: httpx.Client, row: dict[str, Any], raw: dict[str, Any],
                  run: RunCmd = run_cmd) -> SourceText:
    url = row["canonical_url"]
    if not shutil.which("yt-dlp") and run is run_cmd:
        raise ExtractError("yt-dlp is not installed")
    text = youtube_subs(url, run=run)
    if text:
        return SourceText(text, "autosubs", [url])
    max_s = cfg.get("extract.whisper_max_seconds", 900)
    if row.get("duration_s") and row["duration_s"] > max_s:
        raise ExtractError(f"no subtitles and video is longer than {max_s}s (whisper skipped)")
    text = youtube_whisper(url, cfg.get("extract.whisper_model", "small"), run=run)
    if not text:
        raise ExtractError("whisper returned no speech")
    return SourceText(text, "whisper", [url])


ROUTES = {"rss": _from_rss, "trends": _from_trends, "reddit": _from_reddit, "youtube": _from_youtube}


def source_text(cfg: Config, client: httpx.Client, row: dict[str, Any], run: RunCmd = run_cmd) -> SourceText:
    route = ROUTES.get(row["source"])
    if route is None:
        raise ExtractError(f"no extractor for source {row['source']!r}")
    raw = json.loads(row.get("raw_json") or "{}")
    if row["source"] == "youtube":
        return _from_youtube(cfg, client, row, raw, run=run)
    return route(cfg, client, row, raw)
