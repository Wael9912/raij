"""Video formats (Phase 13/15): `short` (1080×1920, ≤60 s, Shorts/Reels) and `long` (1920×1080, 2–5 min,
a regular YouTube video). Each stage reads the format of the script/video it is working on instead of the
old global `script.*` / `voice.*` / `video.*` limits, which now only feed the `short` defaults.

Long videos are landscape on purpose: YouTube files any vertical video ≤3 min as a Short (no API flag can
prevent it), so a 2–3 minute vertical "long" video would still land in the Shorts shelf.

`candidates.wanted` (JSON) records what the owner asked for when they picked an item in Telegram or sent
a topic/script: {"formats": ["short", "long"], "platforms": [...], "kind": "trend|topic|script",
"text": "...", "by": "owner"}. Rows without it are the daily automatic picks (formats ["short"], the brand's
platforms).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

KINDS = ("short", "long")
LABEL = {"short": "📱 Short", "long": "🎬 Long"}


@dataclass(frozen=True)
class Format:
    kind: str
    width: int
    height: int
    max_seconds: float          # finished video including the end card
    voice_max: float            # voice track; the rest is the end card
    voice_min: float            # shorter is kept with a warning
    min_words: int
    max_words: int
    voice_rate: str | None      # TTS rate override; None → the brand voice's rate
    cut_every: float            # aim for a new shot about this often (b-roll)
    hook_title_seconds: float

    @property
    def portrait(self) -> bool:
        return self.height > self.width

    @property
    def orientation(self) -> str:
        return "portrait" if self.portrait else "landscape"

    @property
    def frame(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def label(self) -> str:
        return LABEL.get(self.kind, self.kind)

    def target_words(self) -> tuple[int, int]:
        """What the model is asked for — inside the accepted range, because models undershoot."""
        pad = max(10, (self.max_words - self.min_words) // 8)
        return self.min_words + pad, self.max_words - pad


# Measured Arabic neural TTS: ~1.85 words/s at +0 %, ~2.05 at +10 %.
DEFAULTS: dict[str, dict[str, Any]] = {
    "short": {"width": 1080, "height": 1920, "max_seconds": 60, "voice_max_seconds": 58, "voice_min_seconds": 40,
              "min_words": 85, "max_words": 115, "voice_rate": None, "cut_every": 7.0, "hook_title_seconds": 2.5},
    "long": {"width": 1920, "height": 1080, "max_seconds": 300, "voice_max_seconds": 290, "voice_min_seconds": 100,
             "min_words": 240, "max_words": 520, "voice_rate": "+0%", "cut_every": 8.0, "hook_title_seconds": 3.0},
}


def get(cfg: Any, kind: str | None) -> Format:
    """The format `kind` with config overrides (`formats.<kind>.*`; the legacy `script/voice/video` keys still
    feed `short`). Unknown kinds fall back to `short`."""
    kind = kind if kind in KINDS else "short"
    d = dict(DEFAULTS[kind])
    if cfg is not None:
        if kind == "short":                                     # legacy single-format keys
            legacy = {"min_words": "script.min_words", "max_words": "script.max_words",
                      "voice_max_seconds": "voice.max_seconds", "voice_min_seconds": "voice.min_seconds",
                      "max_seconds": "video.max_seconds", "hook_title_seconds": "video.hook_title_seconds"}
            for k, key in legacy.items():
                v = cfg.get(key)
                if v is not None:
                    d[k] = v
        over = cfg.get(f"formats.{kind}", {}) or {}
        d.update({k: v for k, v in over.items() if k in d})
    return Format(kind, int(d["width"]), int(d["height"]), float(d["max_seconds"]), float(d["voice_max_seconds"]),
                  float(d["voice_min_seconds"]), int(d["min_words"]), int(d["max_words"]),
                  str(d["voice_rate"]) if d.get("voice_rate") else None, float(d["cut_every"]),
                  float(d["hook_title_seconds"]))


def kind_for_words(cfg: Any, words: int) -> str:
    """The format a given script length fits: `short` if it can be spoken inside the short limit."""
    return "short" if words <= get(cfg, "short").max_words else "long"


# --- candidates.wanted ---------------------------------------------------------------

def wanted(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    raw = row.get("wanted")
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def wanted_formats(row: dict[str, Any] | None) -> list[str]:
    fmts = [k for k in (wanted(row).get("formats") or []) if k in KINDS]
    return list(dict.fromkeys(fmts)) or ["short"]


def wanted_platforms(row: dict[str, Any] | None, brand: dict[str, Any] | None) -> list[str]:
    """The owner's platform choice for this item, else the brand's list."""
    chosen = [str(p) for p in (wanted(row).get("platforms") or [])]
    if chosen:
        return list(dict.fromkeys(chosen))
    return list((brand or {}).get("platforms") or [])


def is_manual(row: dict[str, Any] | None) -> bool:
    """Picked or provided by the owner (Telegram), as opposed to the daily automatic selection."""
    return wanted(row).get("by") == "owner"


def encode(formats: list[str], platforms: list[str], kind: str = "trend", text: str | None = None,
           by: str = "owner", **extra: Any) -> str:
    data: dict[str, Any] = {"formats": [k for k in formats if k in KINDS] or ["short"],
                            "platforms": list(dict.fromkeys(platforms)), "kind": kind, "by": by, **extra}
    if text:
        data["text"] = text
    return json.dumps(data, ensure_ascii=False)


def with_format(fmt: Format, **changes: Any) -> Format:
    return replace(fmt, **changes)
