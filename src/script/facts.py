"""Number check: every figure a script states must come from its story card.

Models slip when restating figures (953,531 → 995,000; 211 → 201). Scripts write numbers as
digits, so each one can be matched against the card's numbers, allowing honest rounding only:
a script figure may differ from a card figure by at most half the place value of its own last
non-zero digit — rounding to nearest ("950 ألف" for 953,531 passes; "995 ألف", "201" for 211,
or "20 ألف" for 12,000 do not) — and never by more than MAX_DRIFT of the card's figure, so a
coarse round-up ("2 مليون" for 1,500,001) is refused too (A4).
Small counts (≤ 12) are too often spelled out on the card to check.
"""
from __future__ import annotations

import json
import re
from typing import Any

_NUM = re.compile(r"(\d+(?:[.,٫٬]\d+)*)\s*(ألف|آلاف|الف|مليون|ملايين|مليار|thousand|million|billion|[kKmM]\b)?")
_SCALE = {"ألف": 1e3, "آلاف": 1e3, "الف": 1e3, "thousand": 1e3, "k": 1e3,
          "مليون": 1e6, "ملايين": 1e6, "million": 1e6, "m": 1e6, "مليار": 1e9, "billion": 1e9}
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
SMALL = 12
MAX_DRIFT = 0.2         # a rounded figure may never be off by more than this share of the real one


def numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text.translate(_AR_DIGITS)):
        digits = m.group(1).replace("٬", ",").replace("٫", ".")
        # "953,531" is thousands grouping; "2.5" is a decimal.
        value = float(digits.replace(",", "")) if "," in digits else float(digits)
        scale = _SCALE.get((m.group(2) or "").lower(), 1)
        out.append(value * scale)
    return out


def card_text(story: dict[str, Any]) -> str:
    claims = " ".join(str(c.get("claim", "")) for c in json.loads(story.get("claims") or "[]"))
    facts = " ".join(json.loads(story.get("key_facts") or "[]"))
    return " ".join([story.get("hook") or "", facts, claims, story.get("why_trending") or ""])


def unsupported(script: str, story: dict[str, Any]) -> list[str]:
    """Numbers in the script that match nothing on the card (within rounding)."""
    known = numbers(card_text(story))
    bad = []
    for n in numbers(script):
        if n <= SMALL:
            continue
        if not any(supported(n, m) for m in known):
            bad.append(f"{n:g}")
    return bad


def supported(n: float, m: float) -> bool:
    """Script figure `n` is card figure `m`, or `m` rounded to nearest at n's own precision."""
    if n == m:
        return True
    return abs(n - m) <= _place(n) / 2 and abs(n - m) <= MAX_DRIFT * abs(m)


def _place(n: float) -> float:
    """Place value of the last non-zero digit: 950000 → 10000, 211 → 1, 2.5 → 0.1."""
    if n != int(n):
        return 10 ** -len(f"{n:g}".split(".")[1])
    n, place = int(n), 1
    while n and n % 10 == 0:
        n //= 10
        place *= 10
    return place
