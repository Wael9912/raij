"""Background music (Phase 20): a credited pool under assets/music/, picked by mood.

`assets/music/pool.json` (written by `tools/fetch_music.py`) lists tracks with their licence and moods:
  [{"file": "cipher.mp3", "title": "Cipher", "artist": "Kevin MacLeod", "site": "incompetech.com",
    "license": "CC BY 4.0", "moods": ["tech", "upbeat"]}, ...]
A video gets a track whose moods match its category/series (`music.moods` in config; long videos prefer
"calm"), rotated by video id so consecutive videos differ. CC BY needs credit: the pick's credit line goes into
`videos.notes.credits`, which review and publish already print in every caption.
Loose files in assets/music without a pool.json still work as before (no credit line).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.config import Config

log = logging.getLogger("raij.assemble")

MUSIC_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".flac"}
POOL_FILE = "pool.json"


def pool(cfg: Config) -> list[dict[str, Any]]:
    """Tracks from pool.json whose file exists (missing files are skipped, not fatal)."""
    path = cfg.root / "assets" / "music" / POOL_FILE
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        log.warning("assets/music/pool.json unreadable: %s", exc)
        return []
    out = []
    for e in entries if isinstance(entries, list) else []:
        f = path.parent / str(e.get("file") or "")
        if e.get("file") and f.exists() and f.stat().st_size > 0:
            out.append({**e, "path": f})
    return out


def credit_line(track: dict[str, Any]) -> str:
    who = track.get("artist") or "Unknown"
    site = f" ({track['site']})" if track.get("site") else ""
    return f"Music: {track.get('title') or track['file']} by {who}{site}, {track.get('license') or 'licensed'}"


def pick(cfg: Config, video_id: int, category: str | None = None, kind: str = "short",
         series: str | None = None) -> tuple[Path | None, str | None]:
    """(repo-relative path, credit line) for this video, or (None, None) when there is no music."""
    if not cfg.get("music.enabled", True):
        return None, None
    tracks = pool(cfg)
    if not tracks:
        loose = sorted(p for p in (cfg.root / "assets" / "music").glob("*") if p.suffix.lower() in MUSIC_EXT)
        return (loose[video_id % len(loose)].relative_to(cfg.root), None) if loose else (None, None)
    moods = cfg.get("music.moods", {}) or {}
    wanted = set(moods.get(category or "", []) or []) | set(moods.get(series or "", []) or [])
    if kind == "long":
        wanted |= set(moods.get("long", ["calm"]))
    if not wanted:
        wanted = set(moods.get("default", []) or [])
    fitting = [t for t in tracks if wanted & set(t.get("moods") or [])] if wanted else []
    choice = (fitting or tracks)[video_id % len(fitting or tracks)]
    return choice["path"].relative_to(cfg.root), credit_line(choice)
