"""Copy detection between a script and its source text, offline and dependency-free.

Score = word-trigram containment: the share of the script's 3-word sequences that also appear in
the source, after Arabic normalization. An independent retelling of the same facts shares names
and a few set phrases (low score); lifted sentences share long runs (high score). Containment
alone misses one or two verbatim sentences inside an otherwise original script (two lifted
15-word sentences ≈ 0.27), so `longest_run` also reports the longest shared word sequence (A5).

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


def longest_run(script: str, source: str) -> tuple[int, str]:
    """Length and text of the longest sequence of consecutive normalized words the script shares with
    the source (plain DP over the two word lists; scripts are ~100 words, sources ≤ 20k chars).
    Numbers extend a run but don't count toward its length: a date and an age ("في 28 ديسمبر 2025 عن
    عمر ناهز 91 عاما", seen in a real passed script) are facts the retelling must repeat, not copied prose."""
    a, b = normalize(script).split(), normalize(source).split()
    if not a or not b:
        return 0, ""
    weight = [0 if w.isdigit() else 1 for w in a]
    best, best_len, best_end = 0, 0, 0
    prev_n = [0] * (len(b) + 1)          # matched tokens in the run ending at (i, j)
    prev_w = [0] * (len(b) + 1)          # of which non-numeric
    for i in range(1, len(a) + 1):
        cur_n, cur_w = [0] * (len(b) + 1), [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur_n[j] = prev_n[j - 1] + 1
                cur_w[j] = prev_w[j - 1] + weight[i - 1]
                if cur_w[j] > best:
                    best, best_len, best_end = cur_w[j], cur_n[j], i
        prev_n, prev_w = cur_n, cur_w
    return best, " ".join(a[best_end - best_len:best_end])


def shared_phrases(script: str, source: str, n: int = 3, limit: int = 12) -> list[str]:
    """The script's own n-grams that also occur in the source, for a rewrite note."""
    shared = ngrams(script, n) & ngrams(source, n)
    return [" ".join(g) for g in sorted(shared)][:limit]
