"""Professional Standard Arabic (الفصحى) helpers — owner's call 2026-09-24: "professional Arabic standard script
and voice for all future products" (this replaces Phase 12's "MSA with a light Gulf touch").

- `dialect_words(text)`: colloquial markers from Egyptian, Gulf and Levantine Arabic that must not appear in a
  script (a weak fallback model wrote video #39's script entirely in Egyptian: "ليه … مولع اليومين دول … ما هداش").
  The draft is rejected and rewritten when any is found; the list is deliberately conservative — every entry is a
  word that has no Standard Arabic reading in normal prose.
- `strip_tashkeel(text)`: remove diacritics; `same_letters(a, b)`: identical apart from diacritics/punctuation
  (the vocalized TTS text must be the polished text plus marks, nothing else).
"""
from __future__ import annotations

import re

_TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_NOT_LETTER = re.compile(r"[^\w]", re.U)

# Dialect words (normalized: no diacritics, hamza variants kept as written). Egyptian, Gulf, Levantine.
DIALECT = {
    # Egyptian
    "ليه", "ايه", "إيه", "إزاي", "ازاي", "دلوقتي", "دلوقت", "اليومين", "دول", "ده", "دي", "مش", "علشان", "عشان",
    "كده", "كدة", "بتاع", "بتاعة", "بتاعت", "فين", "مين", "امتى", "إمتى", "اوي", "أوي", "لسه", "لسة", "برضو", "برضه",
    "كمان", "معلش", "معليش", "يلا", "يالا", "حاجة", "حاجات", "عايز", "عايزة", "عاوز", "عاوزة", "ملوش", "مفيش",
    "مافيش", "محدش", "مالوش", "مولع", "واصع", "خالص", "طب", "طيب", "بقى", "بقت", "زي", "زيي", "النهارده", "امبارح",
    "بكرة", "بكره", "جوه", "بره",
    # Gulf
    "وايد", "زين", "ترى", "شنو", "ليش", "وش", "وشو", "يالله", "حيل", "ابغى", "أبغى", "ابي", "ودي", "توه", "الحين",
    "الحينه", "شلون", "وين", "هاذي", "هذي", "ذولا", "ذيلا", "شوي", "شوية", "اشوف", "أشوف", "تشوف", "نشوف", "يشوف",
    "هالشي", "هالشيء", "هالمرة", "هاليوم", "عاد", "مب", "مو", "ماكو", "اكو", "أكو", "هسه", "هسة",
    # Levantine
    "هيك", "هلق", "هلأ", "شو", "كتير", "منيح", "منيحة", "بدي", "بدك", "بدنا", "بدهم", "لسا", "ليكي", "هاد", "هاي",
}
# Words above that also read as MSA in some positions; they never trigger by themselves.
_SAFE = {"طب", "طيب", "بقى", "بقت", "زي", "عاد", "مو", "دول", "حيل"}
_ACTIVE = DIALECT - _SAFE
# "ما هداش", "مبيعرفش", "مش عارف" — the Egyptian ش-negation has no MSA reading at all.
_SH_NEG = re.compile(r"\b(?:ما\s+|م)[ء-ي]{2,}ش\b")


def strip_tashkeel(text: str) -> str:
    return _TASHKEEL.sub("", text or "")


def same_letters(a: str, b: str) -> bool:
    """True when `a` and `b` have the same letters/digits in the same order (diacritics, punctuation ignored)."""
    return _NOT_LETTER.sub("", strip_tashkeel(a)) == _NOT_LETTER.sub("", strip_tashkeel(b))


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s،؛؟.,;:!?()\[\]«»\"'…،؛؟-]+", strip_tashkeel(text)) if t]


def dialect_words(text: str) -> list[str]:
    """Colloquial words found in `text`, in order of first appearance (no duplicates)."""
    found: list[str] = []
    for tok in _tokens(text):
        bare = tok
        for pre in ("و", "ف", "ب", "ل", "ك", "ال"):     # clitics: "وليه", "بالحين" (one layer is enough)
            if bare in _ACTIVE:
                break
            if bare.startswith(pre) and len(bare) > len(pre) + 1 and bare[len(pre):] in _ACTIVE:
                bare = bare[len(pre):]
                break
        if bare in _ACTIVE and bare not in found:
            found.append(bare)
    for m in _SH_NEG.finditer(strip_tashkeel(text)):
        if m.group(0) not in found:
            found.append(m.group(0))
    return found


__all__ = ["DIALECT", "dialect_words", "same_letters", "strip_tashkeel"]
