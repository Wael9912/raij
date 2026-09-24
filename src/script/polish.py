"""Phase 18 — professional فصحى: one editor pass over a passed draft (owner 2026-09-24).

`polish(cfg, beats, story)` asks the LLM (prompt `script_polish`) for two things per beat: the beat rewritten in
professional Modern Standard Arabic, and a fully vocalized copy for the speech engine (diacritics are what make
neural Arabic voices read correctly). Everything the model returns is verified before it is kept:
- same number of beats;
- a beat's digits unchanged and still supported by the story card (`facts`), else that beat keeps its draft text;
- no dialect word left (`fusha.dialect_words`), else the draft text is kept for that beat;
- the vocalized text must be the final text plus marks only (`fusha.same_letters`), else no `tts` for that beat.
The result is the same beats list with `text` replaced where accepted and a new `tts` key; subtitles, cards and
captions keep using `text`, the voice stage speaks `tts` (src/voice/tts.speech_text). Any LLM failure leaves the
draft untouched — polish is a quality step, never a reason to lose a script.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from src import llm
from src.config import Config
from src.script import facts, fusha

log = logging.getLogger("raij.script")


def polish(cfg: Config, beats: list[dict[str, Any]], story: dict[str, Any] | None = None,
           brand: dict[str, Any] | None = None, client: httpx.Client | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Returns (beats, notes). `notes` = {"model", "changed": n, "kept": [i…], "tts": n} or {"error": …}."""
    texts = [str(b["text"]) for b in beats]
    prompt = llm.load_prompt("script_polish", brand_name=str((brand or {}).get("name") or (brand or {}).get("id") or ""),
                             count=str(len(texts)), beats=json.dumps(texts, ensure_ascii=False, indent=1))
    try:
        data = llm.complete_json(cfg, prompt, client=client)
    except llm.LLMError as exc:
        log.warning("Polish skipped (no model answered): %s", exc)
        return beats, {"error": str(exc)[:200]}
    items = data.get("beats") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != len(beats):
        log.warning("Polish ignored: expected %d beats, got %s", len(beats), len(items) if isinstance(items, list) else "none")
        return beats, {"error": "beat count mismatch"}
    out, kept, changed, voiced = [], [], 0, 0
    for i, (b, item) in enumerate(zip(beats, items)):
        new = str((item or {}).get("text") or "").strip() if isinstance(item, dict) else ""
        tts = str((item or {}).get("tts") or "").strip() if isinstance(item, dict) else ""
        final = b["text"]
        if new and _accept(b["text"], new, story):
            final = new
            changed += int(new != b["text"])
        else:
            kept.append(i)
        beat = {**b, "text": final}
        beat.pop("tts", None)
        if tts and fusha.same_letters(tts, final) and fusha.strip_tashkeel(tts) != tts:
            beat["tts"] = tts
            voiced += 1
        out.append(beat)
    notes = {"model": llm.last_model(), "changed": changed, "tts": voiced}
    if kept:
        notes["kept"] = kept
    return out, notes


def _accept(old: str, new: str, story: dict[str, Any] | None) -> bool:
    if fusha.dialect_words(new):
        return False
    if sorted(facts.numbers(new)) != sorted(facts.numbers(old)):
        return False
    if story is not None and facts.unsupported(new, story):
        return False
    words_old, words_new = len(old.split()), len(new.split())
    return words_new <= max(words_old * 1.3, words_old + 4)   # an editor tightens; a rewrite that balloons is not one


__all__ = ["polish"]
