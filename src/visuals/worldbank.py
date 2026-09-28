"""World Bank Indicators API (keyless, CC BY 4.0): the channel's primary data source for charts.

Only indicators in CATALOG may be charted — each carries the Arabic label and unit printed on the frame, so the
script model can't invent a series or mislabel one. Responses are cached per (indicator, countries, years) in
data/cache/worldbank/ for a week.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

from src.config import Config
from src.discover.common import FetchError, request

API = "https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}"
SOURCE = "البنك الدولي"
CACHE_DAYS = 7

# id → (Arabic label, unit). Units: "دولار" values are scaled (مليون/مليار) by canvas.number; "%" sticks.
CATALOG: dict[str, tuple[str, str]] = {
    "NY.GDP.MKTP.CD": ("الناتج المحلي الإجمالي", "دولار"),
    "NY.GDP.PCAP.CD": ("نصيب الفرد من الناتج المحلي", "دولار"),
    "NY.GDP.MKTP.KD.ZG": ("نمو الناتج المحلي", "%"),
    "FP.CPI.TOTL.ZG": ("معدل التضخم", "%"),
    "NY.GDP.PETR.RT.ZS": ("ريع النفط من الناتج المحلي", "%"),
    "NE.EXP.GNFS.ZS": ("الصادرات من الناتج المحلي", "%"),
    "NE.IMP.GNFS.ZS": ("الواردات من الناتج المحلي", "%"),
    "BX.KLT.DINV.CD.WD": ("صافي الاستثمار الأجنبي المباشر", "دولار"),
    "FI.RES.TOTL.CD": ("الاحتياطيات الدولية", "دولار"),
    "BX.TRF.PWKR.CD.DT": ("تحويلات العاملين في الخارج", "دولار"),
    "ST.INT.RCPT.CD": ("إيرادات السياحة الدولية", "دولار"),
    "ST.INT.ARVL": ("عدد السياح الوافدين", "سائح"),
    "SP.POP.TOTL": ("عدد السكان", "نسمة"),
    "SP.URB.TOTL.IN.ZS": ("سكان المدن", "%"),
    "SL.UEM.TOTL.ZS": ("معدل البطالة", "%"),
    "SL.UEM.1524.ZS": ("بطالة الشباب", "%"),
    "GC.DOD.TOTL.GD.ZS": ("الدين الحكومي من الناتج المحلي", "%"),
    "EG.USE.PCAP.KG.OE": ("استهلاك الطاقة للفرد", "كغ مكافئ نفط"),
    "EG.ELC.RNEW.ZS": ("الكهرباء من مصادر متجددة", "%"),
    "ER.H2O.INTR.PC": ("موارد المياه العذبة للفرد", "م³"),
    "AG.LND.ARBL.ZS": ("الأراضي الصالحة للزراعة", "%"),
    "TM.VAL.FOOD.ZS.UN": ("الغذاء من الواردات السلعية", "%"),
    "PA.NUS.FCRF": ("سعر الصرف الرسمي مقابل الدولار", "وحدة"),
    "NY.GNS.ICTR.ZS": ("الادخار الإجمالي من الناتج", "%"),
    "MS.MIL.XPND.GD.ZS": ("الإنفاق العسكري من الناتج", "%"),
}


class DataUnavailable(RuntimeError):
    """The World Bank has no values for this request (or couldn't be reached)."""


def _cache_path(cfg: Config, key: str):
    d = cfg.root / "data" / "cache" / "worldbank"
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")


def fetch(cfg: Config, client: httpx.Client, indicator: str, countries: list[str], first: int,
          last: int) -> dict[str, dict[int, float]]:
    """{ISO3: {year: value}} for the years the World Bank has (nulls dropped)."""
    if indicator not in CATALOG:
        raise DataUnavailable(f"indicator {indicator} is not in the catalog")
    key = f"{indicator}|{';'.join(sorted(countries))}|{first}|{last}"
    path = _cache_path(cfg, key)
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_DAYS * 86400:
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        url = API.format(countries=";".join(countries), indicator=indicator)
        try:
            resp = request(client, "GET", url, params={"format": "json", "date": f"{first}:{last}",
                                                       "per_page": 2000})
            raw = resp.json()
        except (FetchError, httpx.HTTPError, ValueError) as exc:
            raise DataUnavailable(f"World Bank unreachable: {exc}") from exc
        path.write_text(json.dumps(raw), encoding="utf-8")
    rows = raw[1] if isinstance(raw, list) and len(raw) > 1 and isinstance(raw[1], list) else []
    out: dict[str, dict[int, float]] = {}
    for r in rows:
        if r.get("value") is None:
            continue
        out.setdefault(r["countryiso3code"], {})[int(r["date"])] = float(r["value"])
    if not out:
        raise DataUnavailable(f"no {indicator} values for {', '.join(countries)} in {first}–{last}")
    return out


def series(cfg: Config, client: httpx.Client, spec: dict[str, Any]) -> dict[str, Any]:
    """Resolve a World Bank chart spec into plottable data:
    bar  → latest year every country has (else each country's latest, year noted), one bar per country;
    line → one line per country across the years."""
    from src.visuals import geo
    ind = spec["indicator"]
    label, unit = CATALOG[ind]
    first, last = spec.get("years") or [2000, time.gmtime().tm_year]
    data = fetch(cfg, client, ind, spec["countries"], int(first), int(last))
    names = {iso: c.name_ar for iso, c in geo.countries().items()}
    if spec["kind"] == "bar":
        common = set.intersection(*(set(v) for v in data.values())) if data else set()
        points = []
        for iso in spec["countries"]:
            vals = data.get(iso)
            if not vals:
                continue
            year = max(common) if common else max(vals)
            points.append({"label": names.get(iso, iso), "value": vals[year], "year": year})
        years = sorted({p["year"] for p in points})
        note = str(years[0]) if len(years) == 1 else f"{years[0]}–{years[-1]}"
        return {"kind": "bar", "points": points, "unit": unit, "label": label, "note": note, "source": SOURCE}
    lines = [{"label": names.get(iso, iso), "points": sorted(data[iso].items())} for iso in spec["countries"]
             if iso in data and len(data[iso]) >= 2]
    if not lines:
        raise DataUnavailable(f"not enough years of {ind} for a line")
    return {"kind": "line", "lines": lines, "unit": unit, "label": label, "source": SOURCE}
