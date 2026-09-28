"""Set the YouTube channel page to the current brand (Phase 21): name, description, keywords, default language,
banner (assets/brand/banner_youtube.png), and optionally unlist every video / hide every playlist uploaded
before a cut-off — the off-niche «رائج» backlog.

    uv run python tools/channel_page.py --dry-run
    uv run python tools/channel_page.py [--unlist-before 2026-09-28] [--hide-playlists]

The avatar can't be set through the API: upload assets/brand/avatar.png in YouTube Studio → Customization.
Quota: channels.update 50, banner 50, each videos.update 50, each playlists.update 50 units.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import ROOT, load_config  # noqa: E402
from src.publish import youtube as yt  # noqa: E402

API = "https://www.googleapis.com/youtube/v3"
BANNER_API = "https://www.googleapis.com/upload/youtube/v3/channelBanners/insert"

DESCRIPTION = """خريطة المال: نشرح الاقتصاد على الخريطة.

لماذا تغتني دول وتفتقر أخرى؟ كيف يحرك مضيق ضيق أسعار النفط في العالم كله؟ من أين يأتي الذهب وإلى أين يذهب؟

حلقات تشرح النفط والغاز، والذهب والعملات، وطرق التجارة والموانئ، والمشاريع الكبرى، بخرائط ورسوم بيانية من إعداد القناة، وبيانات من مصادر مفتوحة مثل البنك الدولي.

📅 حلقة جديدة كل اثنين وخميس، ومقطع قصير كل يوم.

المحتوى للشرح والتثقيف فقط، وليس نصيحة مالية أو استثمارية. التعليق الصوتي بصوت مولد آليا.

Money Map: the economy of the Arab world and beyond, explained with maps and data."""

KEYWORDS = ('"خريطة المال" اقتصاد النفط الذهب "طرق التجارة" الخليج السعودية "مضيق هرمز" "قناة السويس" '
            'العملات "الاقتصاد العالمي" economy geoeconomics "economics explained" maps')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--unlist-before", help="unlist public videos published before this date (YYYY-MM-DD)")
    ap.add_argument("--hide-playlists", action="store_true", help="make every existing playlist private")
    args = ap.parse_args()
    cfg = load_config()
    brand = cfg.brands[0]
    with httpx.Client(timeout=120) as c:
        h = {"Authorization": f"Bearer {yt.access_token(cfg, c)}"}
        ch = c.get(f"{API}/channels", params={"part": "brandingSettings", "mine": "true"}, headers=h).json()["items"][0]
        banner = ROOT / "assets" / "brand" / "banner_youtube.png"
        branding = {"channel": {**ch["brandingSettings"].get("channel", {}), "title": brand["name"],
                                "description": DESCRIPTION, "keywords": KEYWORDS, "defaultLanguage": "ar"}}
        if args.dry_run:
            print(json.dumps(branding, ensure_ascii=False, indent=1))
        else:
            r = c.post(BANNER_API, params={"uploadType": "media"}, content=banner.read_bytes(),
                       headers={**h, "Content-Type": "image/png"})
            r.raise_for_status()
            branding["image"] = {"bannerExternalUrl": r.json()["url"]}
            r = c.put(f"{API}/channels", params={"part": "brandingSettings"},
                      json={"id": ch["id"], "brandingSettings": branding}, headers=h)
            print("channel:", r.status_code, r.text[:300] if r.status_code >= 400 else "ok")
            r.raise_for_status()

        if args.unlist_before:
            uploads = c.get(f"{API}/channels", params={"part": "contentDetails", "mine": "true"},
                            headers=h).json()["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            ids, tok = [], None
            while True:
                r = c.get(f"{API}/playlistItems", params={"part": "contentDetails", "playlistId": uploads,
                                                          "maxResults": 50, **({"pageToken": tok} if tok else {})},
                          headers=h).json()
                ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])
                        if i["contentDetails"].get("videoPublishedAt", "9999") < args.unlist_before]
                tok = r.get("nextPageToken")
                if not tok:
                    break
            done = 0
            for i in range(0, len(ids), 50):
                items = c.get(f"{API}/videos", params={"part": "status", "id": ",".join(ids[i:i + 50])},
                              headers=h).json().get("items", [])
                for v in items:
                    if v["status"]["privacyStatus"] != "public":
                        continue
                    status = {k: v["status"][k] for k in ("embeddable", "license", "publicStatsViewable",
                                                          "selfDeclaredMadeForKids") if k in v["status"]}
                    status["privacyStatus"] = "unlisted"
                    if args.dry_run:
                        done += 1
                        continue
                    r = c.put(f"{API}/videos", params={"part": "status"}, json={"id": v["id"], "status": status},
                              headers=h)
                    if r.status_code >= 400:
                        print("video", v["id"], r.status_code, r.text[:200])
                    else:
                        done += 1
            print(f"unlisted {done}/{len(ids)} video(s) published before {args.unlist_before}"
                  + (" (dry run)" if args.dry_run else ""))

        if args.hide_playlists:
            pls = c.get(f"{API}/playlists", params={"part": "snippet,status", "mine": "true", "maxResults": 50},
                        headers=h).json().get("items", [])
            for p in pls:
                if p["status"]["privacyStatus"] == "private":
                    continue
                if not args.dry_run:
                    r = c.put(f"{API}/playlists", params={"part": "snippet,status"},
                              json={"id": p["id"], "snippet": {"title": p["snippet"]["title"],
                                                                "description": p["snippet"].get("description", "")},
                                    "status": {"privacyStatus": "private"}}, headers=h)
                    r.raise_for_status()
                print("playlist hidden:", p["snippet"]["title"] + (" (dry run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
