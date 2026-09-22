"""Draft one Arabic script from a story card, validate it, and gate it for similarity.

The prompt is built from the card only; stories.transcript is read solely by the similarity gate.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from src import llm
from src.config import Config
from src.script import facts, similarity

log = logging.getLogger("raij.script")

ROLES_ORDER = ("hook", "body", "payoff", "cta")
_ASCII_TERM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '&-]*$")
# Faceless b-roll: stock "people" shots read as the story's real person. Keywords naming people are
# dropped (hands, crowds and silhouettes stay allowed); real public figures get a licensed photo instead.
_PERSON_WORD = re.compile(
    r"\b(man|men|woman|women|person|people|guy|girl|boy|lady|ladies|gentleman|businessman|businesswoman|"
    r"executive|manager|coach|player|athlete|actor|actress|celebrity|star|official|president|leader|judge|"
    r"doctor|nurse|patient|fan|fans|friend|friends|family|couple|child|children|kid|kids|elderly|senior|"
    r"portrait|face|faces|selfie|smiling|user|worker|student|teacher|customer|audience)\b", re.I)
_ARABIC = re.compile(r"[\u0600-\u06FF]")
# Weaker models sometimes emit stray CJK/Hangul/kana inside Arabic words (e.g. "مانニング").
_STRAY = re.compile(r"[^\s\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFFA-Za-z0-9"
                    r".,:;!?%'\"()\[\]«»\-–—/…“”‘’#&+]")


class DraftError(ValueError):
    """The LLM answered, but the draft breaks the format or length rules."""

    def __init__(self, message: str, draft: "Draft | None" = None):
        super().__init__(message)
        self.draft = draft


@dataclass
class Draft:
    beats: list[dict[str, Any]]
    description_en: str
    hashtags: list[str]
    hook_title: str | None = None         # on-screen headline (assemble); None → video has no title card
    series: str | None = None             # model's series pick; assemble keeps it only if it's a brand series

    @property
    def body_ar(self) -> str:
        return "\n".join(b["text"] for b in self.beats)

    @property
    def words(self) -> int:
        return len(self.body_ar.split())


@dataclass
class Outcome:
    """Every version written for one (story, brand); the last one is final."""
    versions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def final(self) -> dict[str, Any]:
        return self.versions[-1]


def validate(data: Any, min_words: int, max_words: int) -> Draft:
    if not isinstance(data, dict) or not isinstance(data.get("beats"), list):
        raise DraftError("reply has no beats list")
    beats = []
    for b in data["beats"]:
        if not isinstance(b, dict) or not str(b.get("text") or "").strip():
            raise DraftError("a beat has no text")
        role = str(b.get("role") or "").lower()
        if role not in ROLES_ORDER:
            raise DraftError(f"unknown beat role {role!r}")
        kws = [str(k).strip() for k in b.get("broll_keywords") or [] if str(k).strip()]
        kws = [k for k in kws if _ASCII_TERM.match(k) and not _PERSON_WORD.search(k)][:4]
        if not kws:
            raise DraftError(f"{role} beat has no usable b-roll keywords — give English scene keywords with "
                             f"no people in them (objects, places, hands, crowds, nature)")
        text = str(b["text"]).strip()
        stray = sorted(set(_STRAY.findall(text)))
        if stray:
            raise DraftError(f"{role} beat contains stray non-Arabic characters {''.join(stray)!r}")
        beat = {"role": role, "text": text, "broll_keywords": kws}
        person = str(b.get("person") or "").strip()
        if person and _ASCII_TERM.match(person):
            beat["person"] = person
        beats.append(beat)
    roles = [b["role"] for b in beats]
    if roles[:1] != ["hook"] or roles[-1:] != ["cta"] or "body" not in roles or "payoff" not in roles:
        raise DraftError(f"beats must run hook → body… → payoff → cta, got {roles}")
    if [ROLES_ORDER.index(r) for r in roles] != sorted(ROLES_ORDER.index(r) for r in roles):
        raise DraftError(f"beats out of order: {roles}")
    tags = [t if t.startswith("#") else f"#{t}" for t in (str(t).strip().replace(" ", "_")
                                                           for t in data.get("hashtags") or []) if t.strip("#")]
    draft = Draft(beats, str(data.get("description_en") or "").strip(), tags[:8],
                  clean_title(data.get("hook_title")), str(data.get("series") or "").strip() or None)
    if draft.words < min_words:
        raise DraftError(f"script is {draft.words} words — too short; add about "
                         f"{min_words + 10 - draft.words} words", draft)
    if draft.words > max_words:
        raise DraftError(f"script is {draft.words} words — too long; cut about "
                         f"{draft.words - max_words + 10} words", draft)
    return draft


def clean_title(value: Any, max_words: int = 6) -> str | None:
    """A usable on-screen hook title, or None. A bad title never fails the draft (it costs a retry of
    the whole script); the video just goes without one."""
    title = re.sub(r"\s+", " ", str(value or "")).strip().strip(".،")
    if not title or _STRAY.search(title) or not _ARABIC.search(title) or not 1 <= len(title.split()) <= max_words:
        return None
    return title


def _bullets(items: list[str]) -> str:
    return "\n".join(f"  • {i}" for i in items) or "  • (none)"


def build_prompt(cfg: Config, story: dict[str, Any], brand: dict[str, Any], extra: str = "") -> str:
    claims = [f"{c.get('claim')} ({c.get('source') or 'source'})" for c in json.loads(story.get("claims") or "[]")]
    return llm.load_prompt(
        "script_write",
        brand_name=brand.get("name") or brand["id"],
        tone=brand.get("tone") or "warm and curious",
        hook=story.get("hook") or "",
        key_facts=_bullets(json.loads(story.get("key_facts") or "[]")),
        claims=_bullets(claims),
        why_trending=story.get("why_trending") or "",
        # Aim inside the accepted range: models tend to undershoot word counts.
        target_min=str(cfg.get("script.min_words", 85) + 10),
        target_max=str(cfg.get("script.max_words", 115) - 10),
        series=" / ".join(f'"{v}"' for v in (brand.get("series") or {}).values()) or "(none — omit it)",
        extra=f"\n{extra.strip()}\n" if extra.strip() else "",
    )


def draft(cfg: Config, story: dict[str, Any], brand: dict[str, Any], extra: str = "",
          client: httpx.Client | None = None) -> Draft:
    """One draft; if it breaks the rules, retry once telling the model what was wrong."""
    lo, hi = cfg.get("script.min_words", 85), cfg.get("script.max_words", 115)

    def attempt(note: str) -> Draft:
        d = validate(llm.complete_json(cfg, build_prompt(cfg, story, brand, note), client=client), lo, hi)
        bad = facts.unsupported(d.body_ar, story)
        if bad:
            raise DraftError(f"these numbers are not on the story card: {', '.join(bad)} — use only the "
                             f"card's figures", d)
        return d

    try:
        return attempt(extra)
    except DraftError as exc:
        log.info("Story %s: draft rejected (%s); retrying once", story["id"], exc)
        return attempt(f"{extra}\nYOUR PREVIOUS DRAFT WAS REJECTED: {exc}. Fix that and follow every rule.")


def _row(d: Draft, sim: float | None, status: str, version: int, notes: dict[str, Any]) -> dict[str, Any]:
    look = {k: v for k, v in (("hook_title", d.hook_title), ("series", d.series)) if v}
    return {"version": version, "body_ar": d.body_ar, "beats": d.beats, "description_en": d.description_en,
            "hashtags": d.hashtags, "similarity": sim, "status": status,
            "notes": {"words": d.words, **look, **notes}}


def _rejected(exc: DraftError, version: int) -> dict[str, Any]:
    if exc.draft:
        return _row(exc.draft, None, "rejected", version, {"reason": str(exc)})
    return {"version": version, "body_ar": "", "beats": [], "description_en": "", "hashtags": [],
            "similarity": None, "status": "rejected", "notes": {"reason": str(exc)}}


def write_script(cfg: Config, story: dict[str, Any], brand: dict[str, Any], edit_note: str = "",
                 client: httpx.Client | None = None) -> Outcome:
    """Draft → similarity gate → at most one rewrite. Raises llm.LLMError if no model answers."""
    threshold = cfg.get("script.similarity_threshold", 0.35)
    source = story.get("transcript") or ""
    extra = f"EDITOR'S NOTE — apply this: {edit_note}" if edit_note else ""
    out = Outcome()
    try:
        d = draft(cfg, story, brand, extra, client=client)
    except DraftError as exc:
        out.versions.append(_rejected(exc, 1))
        return out

    for version in (1, 2):
        if not similarity.comparable(d.body_ar, source):
            out.versions.append(_row(d, None, "passed", version, {"gate": "skipped: source in another language"}))
            return out
        sim = round(similarity.containment(d.body_ar, source), 3)
        if sim <= threshold:
            out.versions.append(_row(d, sim, "passed", version, {"gate": "passed"}))
            return out
        phrases = similarity.shared_phrases(d.body_ar, source)
        if version == 2:
            out.versions.append(_row(d, sim, "rejected", version,
                                     {"reason": f"similarity {sim} > {threshold} after rewrite", "shared": phrases}))
            return out
        out.versions.append(_row(d, sim, "superseded", version, {"reason": f"similarity {sim} > {threshold}",
                                                                 "shared": phrases}))
        log.info("Story %s: similarity %.2f > %.2f, rewriting once", story["id"], sim, threshold)
        rewrite = (f"{extra}\nYOUR PREVIOUS DRAFT STAYED TOO CLOSE TO THE SOURCE WORDING. Retell it in your "
                   f"own words with a different angle and sentence structure. Avoid these phrases: "
                   f"{' | '.join(phrases)}")
        try:
            d = draft(cfg, story, brand, rewrite, client=client)
        except DraftError as exc:
            out.versions.append(_rejected(exc, 2))
            return out
    return out
