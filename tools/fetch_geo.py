"""Download Natural Earth 1:50m countries (public domain) into assets/geo/ — the map layer of src/visuals.
The file is committed, so this is only needed to refresh it.

    uv run python tools/fetch_geo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.visuals.geo import COUNTRIES  # noqa: E402

URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
       "ne_50m_admin_0_countries.geojson")

if __name__ == "__main__":
    COUNTRIES.parent.mkdir(parents=True, exist_ok=True)
    resp = httpx.get(URL, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    COUNTRIES.write_bytes(resp.content)
    print(f"{COUNTRIES} ({len(resp.content) // 1024} KB)")
