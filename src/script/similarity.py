"""Copy detection between a script and its source text, offline and dependency-free.

Score = word-trigram containment: the share of the script's 3-word sequences that also appear in
the source, after Arabic normalization. An independent retelling of the same facts shares names
and a few set phrases (low score); lifted sentences share long runs (high score).

Only meaningful when both texts are in the same script: an Arabic draft vs an English source
scores ~0 regardless, so `comparable()` gates whether the check applies.
"""
from __future__ import annotations

import re

_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")   # harakat + tatweel
_ARABIC_LETTER = re.compile(r"[\u0621-\u064A]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")
_TOKEN = re.compile(r"\w+")
_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize(text: str) -> str:
    """Fold spelling variants so trivially different forms of a word compare equal."""
    text = _DIACRITICS.sub("", text).translate(_FOLD).translate(_DIGITS).lower()
    return " ".join(_TOKEN.findall(text))


def ngrams(text: str, n: int = 3) -> set[tuple[str, ...]]:
    words = normalize(text).split()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def arabic_ratio(text: str) -> float:
    ar, lat = len(_ARABIC_LETTER.findall(text)), len(_LATIN_LETTER.findall(text))
    return ar / (ar + lat) if ar + lat else 0.0


def comparable(script: str, source: str) -> bool:
    """Both texts mostly in the same script (Arabic vs Latin)."""
    return (arabic_ratio(script) >= 0.5) == (arabic_ratio(source) >= 0.5)


def containment(script: str, source: str, n: int = 3) -> float:
    grams = ngrams(script, n)
    if not grams:
        return 0.0
    return len(grams & ngrams(source, n)) / len(grams)


def shared_phrases(script: str, source: str, n: int = 3, limit: int = 12) -> list[str]:
    """The script's own n-grams that also occur in the source, for a rewrite note."""
    shared = ngrams(script, n) & ngrams(source, n)
    return [" ".join(g) for g in sorted(shared)][:limit]
