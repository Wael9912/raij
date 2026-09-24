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

from src import formats, llm
from src.config import Config
from src.script import facts, fusha, polish, similarity

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
_ARABIC_LETTER = re.compile(r"[\u0621-\u064A\u0671-\u06D3]")
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
    hook_title_alt: str | None = None     # second headline for the A/B test across platforms (Phase 12)
    kind: str = "short"                   # short | long (src/formats.py)
    model: str | None = None              # which LLM wrote it (Phase 18: weak fallbacks wrote dialect)
    polish: dict[str, Any] | None = None  # editor pass result (Phase 18)

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


def validate(data: Any, min_words: int, max_words: int, kind: str = "short", strict_roles: bool = True) -> Draft:
    """`strict_roles=False` (owner-written scripts) only needs a hook first; long scripts keep per-section
    "chapter" titles on beats (Phase 15: on-screen chapter cards + YouTube chapters)."""
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
        chapter = clean_title(b.get("chapter"), max_words=6)
        if chapter and role == "body":
            beat["chapter"] = chapter
        beats.append(beat)
    roles = [b["role"] for b in beats]
    if not beats or roles[0] != "hook":
        raise DraftError(f"beats must start with a hook, got {roles[:1]}")
    if strict_roles:
        if roles[-1:] != ["cta"] or "body" not in roles or "payoff" not in roles:
            raise DraftError(f"beats must run hook → body… → payoff → cta, got {roles}")
        if [ROLES_ORDER.index(r) for r in roles] != sorted(ROLES_ORDER.index(r) for r in roles):
            raise DraftError(f"beats out of order: {roles}")
    if kind == "long" and strict_roles and sum(1 for b in beats if b.get("chapter")) < 2:
        raise DraftError("a long script needs at least 3 sections, each opening with a \"chapter\" title")
    tags = [t if t.startswith("#") else f"#{t}" for t in (str(t).strip().replace(" ", "_")
                                                           for t in data.get("hashtags") or []) if t.strip("#")]
    title = clean_title(data.get("hook_title"), hook=beats[0]["text"])
    alt = clean_title(data.get("hook_title_alt"), hook=beats[0]["text"])
    if alt and title and similarity.normalize(alt) == similarity.normalize(title):
        alt = None                                     # a copy of A is no B
    draft = Draft(beats, str(data.get("description_en") or "").strip(), tags[:10], title,
                  str(data.get("series") or "").strip() or None, alt, kind=kind)
    if draft.words < min_words:
        raise DraftError(f"script is {draft.words} words — too short; add about "
                         f"{min_words + 10 - draft.words} words", draft)
    if draft.words > max_words:
        raise DraftError(f"script is {draft.words} words — too long; cut about "
                         f"{draft.words - max_words + 10} words", draft)
    return draft


def clean_title(value: Any, max_words: int = 6, hook: str | None = None) -> str | None:
    """A usable on-screen hook title, or None. A bad title never fails the draft (it costs a retry of
    the whole script); the video just goes without one. It needs at least one Arabic *letter* (Arabic
    punctuation like "؟؟" alone doesn't count) and must not just repeat the spoken hook (A14)."""
    title = re.sub(r"\s+", " ", str(value or "")).strip().strip(".،")
    if not title or _STRAY.search(title) or not _ARABIC_LETTER.search(title) \
            or not 1 <= len(title.split()) <= max_words:
        return None
    if hook and similarity.normalize(title) == similarity.normalize(hook):
        return None
    return title


def _bullets(items: list[str]) -> str:
    return "\n".join(f"  • {i}" for i in items) or "  • (none)"


def cta_line(brand: dict[str, Any], series: str | None, seed: int = 0) -> str:
    """The closing line for a series, rotated by `seed` (story or video id) through `brands[].cta` (Phase 12).
    Falls back to the brand's default list, then to the classic "تابعنا للمزيد"."""
    ctas = brand.get("cta") or {}
    options = [str(c) for c in (ctas.get(series or "") or ctas.get("default") or []) if str(c).strip()]
    if not options:
        return "تابعنا للمزيد"
    return options[int(seed) % len(options)]


def series_lines(brand: dict[str, Any], seed: int = 0) -> str:
    """Series names with their closing-line spirit, for the script prompt."""
    names = list(dict.fromkeys((brand.get("series") or {}).values()))
    if not names:
        return "  (none — omit \"series\")"
    return "\n".join(f'  • "{n}" — closing line like: "{cta_line(brand, n, seed)}"' for n in names)


def build_prompt(cfg: Config, story: dict[str, Any], brand: dict[str, Any], extra: str = "", winners: str = "",
                 kind: str = "short") -> str:
    claims = [f"{c.get('claim')} ({c.get('source') or 'source'})" for c in json.loads(story.get("claims") or "[]")]
    lo, hi = formats.get(cfg, kind).target_words()       # aim inside the accepted range: models undershoot
    return llm.load_prompt(
        "script_write_long" if kind == "long" else "script_write",
        brand_name=brand.get("name") or brand["id"],
        tone=brand.get("tone") or "warm and curious",
        hook=story.get("hook") or "",
        key_facts=_bullets(json.loads(story.get("key_facts") or "[]")),
        claims=_bullets(claims),
        why_trending=story.get("why_trending") or "",
        target_min=str(lo),
        target_max=str(hi),
        series=series_lines(brand, int(story.get("id") or 0)),
        extra=f"\n{extra.strip()}\n" if extra.strip() else "",
        winners=f"\n{winners.strip()}\n" if winners.strip() else "",
    )


def draft(cfg: Config, story: dict[str, Any], brand: dict[str, Any], extra: str = "",
          client: httpx.Client | None = None, winners: str = "", kind: str = "short") -> Draft:
    """One draft; if it breaks the rules, retry once telling the model what was wrong."""
    fmt = formats.get(cfg, kind)
    lo, hi = fmt.min_words, fmt.max_words

    def attempt(note: str) -> Draft:
        d = validate(llm.complete_json(cfg, build_prompt(cfg, story, brand, note, winners, kind), client=client),
                     lo, hi, kind=kind)
        d.model = llm.last_model()
        bad = facts.unsupported(d.body_ar, story)
        if bad:
            raise DraftError(f"these numbers are not on the story card: {', '.join(bad)} — use only the "
                             f"card's figures", d)
        slang = fusha.dialect_words(d.body_ar)
        if slang:
            raise DraftError(f"the script contains colloquial words ({', '.join(slang)}) — write professional "
                             f"Modern Standard Arabic (الفصحى) only, no dialect anywhere, not even in the hook or CTA", d)
        return d

    try:
        return attempt(extra)
    except DraftError as exc:
        log.info("Story %s: draft rejected (%s); retrying once", story["id"], exc)
        return attempt(f"{extra}\nYOUR PREVIOUS DRAFT WAS REJECTED: {exc}. Fix that and follow every rule.")


def _row(d: Draft, sim: float | None, status: str, version: int, notes: dict[str, Any]) -> dict[str, Any]:
    look = {k: v for k, v in (("hook_title", d.hook_title), ("hook_title_alt", d.hook_title_alt),
                              ("series", d.series), ("model", d.model), ("polish", d.polish)) if v}
    return {"version": version, "kind": d.kind, "body_ar": d.body_ar, "beats": d.beats,
            "description_en": d.description_en, "hashtags": d.hashtags, "similarity": sim, "status": status,
            "notes": {"words": d.words, **look, **notes}}


def _rejected(exc: DraftError, version: int, kind: str = "short") -> dict[str, Any]:
    if exc.draft:
        return _row(exc.draft, None, "rejected", version, {"reason": str(exc)})
    return {"version": version, "kind": kind, "body_ar": "", "beats": [], "description_en": "", "hashtags": [],
            "similarity": None, "status": "rejected", "notes": {"reason": str(exc)}}


def write_script(cfg: Config, story: dict[str, Any], brand: dict[str, Any], edit_note: str = "",
                 client: httpx.Client | None = None, winners: str = "", kind: str = "short") -> Outcome:
    """Draft → similarity gate → at most one rewrite. Raises llm.LLMError if no model answers."""
    threshold = cfg.get("script.similarity_threshold", 0.35)
    max_run = int(cfg.get("script.max_shared_run", 8))
    source = story.get("transcript") or ""
    extra = f"EDITOR'S NOTE — apply this: {edit_note}" if edit_note else ""
    out = Outcome()
    try:
        d = draft(cfg, story, brand, extra, client=client, winners=winners, kind=kind)
    except DraftError as exc:
        out.versions.append(_rejected(exc, 1, kind))
        return out

    for version in (1, 2):
        if not similarity.comparable(d.body_ar, source):
            _polish(cfg, d, story, brand, client)
            out.versions.append(_row(d, None, "passed", version, {"gate": "skipped: source in another language"}))
            return out
        sim = round(similarity.containment(d.body_ar, source), 3)
        run, run_text = similarity.longest_run(d.body_ar, source)
        if sim <= threshold and run < max_run:
            _polish(cfg, d, story, brand, client)
            out.versions.append(_row(d, sim, "passed", version, {"gate": "passed", "shared_run": run}))
            return out
        phrases = similarity.shared_phrases(d.body_ar, source)
        # A lifted sentence is the more specific finding (A5); otherwise it's the overall overlap.
        why = (f"{run} consecutive words copied from the source" if run >= max_run
               else f"similarity {sim} > {threshold}")
        if run >= max_run and run_text not in phrases:
            phrases = [run_text] + phrases
        if version == 2:
            out.versions.append(_row(d, sim, "rejected", version,
                                     {"reason": f"{why} after rewrite", "shared": phrases, "shared_run": run}))
            return out
        out.versions.append(_row(d, sim, "superseded", version, {"reason": why, "shared": phrases,
                                                                 "shared_run": run}))
        log.info("Story %s: %s, rewriting once", story["id"], why)
        rewrite = (f"{extra}\nYOUR PREVIOUS DRAFT STAYED TOO CLOSE TO THE SOURCE WORDING. Retell it in your "
                   f"own words with a different angle and sentence structure. Avoid these phrases: "
                   f"{' | '.join(phrases)}")
        try:
            d = draft(cfg, story, brand, rewrite, client=client, winners=winners, kind=kind)
        except DraftError as exc:
            out.versions.append(_rejected(exc, 2, kind))
            return out
    return out


def _polish(cfg: Config, d: Draft, story: dict[str, Any], brand: dict[str, Any], client: httpx.Client | None) -> None:
    """Phase 18: the editor pass (professional فصحى + vocalized TTS text) on a draft that passed every gate.
    Off with `script.polish: false`. The polished body must still pass the copy gate, else the draft stays."""
    if not cfg.get("script.polish", True):
        return
    beats, notes = polish.polish(cfg, d.beats, story, brand, client=client)
    source = story.get("transcript") or ""
    body = "\n".join(b["text"] for b in beats)
    if similarity.comparable(body, source) and similarity.containment(body, source) > cfg.get("script.similarity_threshold", 0.35):
        notes = {**notes, "error": "polished text too close to the source — draft kept"}
        beats = [{**b, "text": o["text"]} for b, o in zip(beats, d.beats)]
    d.beats = beats
    d.polish = notes


# --- owner-written scripts (Phase 13) -----------------------------------------------------

_PUNCT = re.compile(r"[^\w\s]", re.U)


def _norm_words(text: str) -> list[str]:
    return _PUNCT.sub(" ", similarity.normalize(text)).split()


def same_text(original: str, produced: str) -> bool:
    """The beats must still be the owner's script: same words in the same order, allowing punctuation and typo
    fixes (≥ 90 % of words unchanged, length within 10 %)."""
    a, b = _norm_words(original), _norm_words(produced)
    if not a or not b or not 0.9 <= len(b) / len(a) <= 1.1:
        return False
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.9


def segment_script(cfg: Config, text: str, brand: dict[str, Any], kind: str, edit_note: str = "",
                   client: httpx.Client | None = None, seed: int = 0) -> Outcome:
    """Split an owner-written script into beats with b-roll keywords, titles and caption fields — no
    rewriting, no fact gate, no similarity gate (it is their text). With `edit_note` the model may change the
    wording as asked, so the verbatim check is skipped. Raises llm.LLMError if no model answers."""
    fmt = formats.get(cfg, kind)
    extra = f"EDITOR'S NOTE — apply this to the text (you may rewrite where needed): {edit_note}" if edit_note else ""

    def attempt(note: str) -> Draft:
        prompt = llm.load_prompt("script_segment", brand_name=brand.get("name") or brand["id"], text=text,
                                 series=series_lines(brand, seed), extra=f"\n{note.strip()}\n" if note.strip() else "")
        d = validate(llm.complete_json(cfg, prompt, client=client), 0, 10 ** 6, kind=kind, strict_roles=False)
        if not edit_note and not same_text(text, d.body_ar):
            raise DraftError("the beats changed the owner's words — split the script, don't rewrite it", d)
        if d.words > fmt.max_words:
            raise DraftError(f"script is {d.words} words — the {kind} format takes at most {fmt.max_words}; "
                             f"{'cut it' if edit_note else 'it will be spoken faster if it fits'}", d)
        return d

    out = Outcome()
    try:
        try:
            d = attempt(extra)
        except DraftError as exc:
            log.info("Owner script: %s; retrying once", exc)
            d = attempt(f"{extra}\nYOUR PREVIOUS ANSWER WAS REJECTED: {exc}. Fix that and follow every rule.")
    except DraftError as exc:
        out.versions.append(_rejected(exc, 1, kind))
        return out
    out.versions.append(_row(d, None, "passed", 1, {"gate": "skipped: owner-written script"}))
    return out
