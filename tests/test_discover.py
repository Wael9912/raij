import json
from datetime import datetime, timezone

import httpx
import pytest

from src import db
from src.config import load_config
from src.discover import runner, trends, youtube
from src.discover.common import Candidate, canonicalize_url, dedupe, upsert_candidates
from src.discover.quota import QuotaBudget, QuotaExceeded

TRENDS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>Kick</title><ht:approx_traffic>2K+</ht:approx_traffic>
<pubDate>Mon, 21 Sep 2026 12:40:00 -0700</pubDate><ht:picture>https://img/x.jpg</ht:picture>
<ht:news_item><ht:news_item_title>Story</ht:news_item_title>
<ht:news_item_url>https://news.example/a</ht:news_item_url></ht:news_item></item>
<item><title>ياسر إبراهيم</title><ht:approx_traffic>500+</ht:approx_traffic></item>
</channel></rss>""".encode()

RSS_XML = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>A</title><link>https://www.example.com/a/?utm_source=x</link>
<pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>A again</title><link>https://www.example.com/a</link></item>
</channel></rss>"""


def _yt_video(vid, views=1000):
    return {
        "id": vid,
        "snippet": {"title": f"t{vid}", "publishedAt": "2026-09-21T10:00:00Z",
                    "thumbnails": {"high": {"url": "https://i.ytimg.com/x.jpg"}}},
        "statistics": {"viewCount": str(views), "likeCount": "10", "commentCount": "2"},
        "contentDetails": {"duration": "PT1M5S"},
    }


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "googleapis.com/youtube/v3/videos" in url:
        if request.url.params.get("chart"):
            region = request.url.params["regionCode"]
            if region == "SD":
                return httpx.Response(400, json={"error": {"message": "regionCode not supported"}})
            return httpx.Response(200, json={"items": [_yt_video("aaaaaaaaaaa"), _yt_video(f"{region}xxxxxxxxx")]})
        ids = request.url.params["id"].split(",")
        return httpx.Response(200, json={"items": [_yt_video(i, 5000) for i in ids]})
    if "googleapis.com/youtube/v3/search" in url:
        return httpx.Response(200, json={"items": [{"id": {"videoId": "bbbbbbbbbbb"}}]})
    if "access_token" in url:
        return httpx.Response(200, json={"access_token": "tok"})
    if "oauth.reddit.com" in url:
        assert request.headers["Authorization"] == "bearer tok"
        posts = [
            {"id": "p1", "title": "TIL", "score": 900, "num_comments": 40, "created_utc": 1790000000},
            {"id": "p2", "title": "pinned", "stickied": True, "created_utc": 1790000000},
            {"id": "p3", "title": "nsfw", "over_18": True, "created_utc": 1790000000},
        ]
        return httpx.Response(200, json={"data": {"children": [{"data": p} for p in posts]}})
    if "trends.google.com" in url:
        return httpx.Response(200, content=TRENDS_XML)
    if "feeds.example" in url:
        return httpx.Response(200, content=RSS_XML)
    return httpx.Response(404)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    for k, v in {"YOUTUBE_API_KEY": "SECRETKEY", "REDDIT_CLIENT_ID": "id", "REDDIT_CLIENT_SECRET": "s"}.items():
        monkeypatch.setenv(k, v)
    cfg = load_config()
    cfg.data["discovery"]["regions"] = ["SD", "EG", "US"]
    cfg.data["discovery"]["youtube"]["shorts_queries"] = ["facts"]
    cfg.data["discovery"]["reddit"]["subreddits"] = ["todayilearned"]
    cfg.data["discovery"]["trends"]["geos"] = ["EG"]
    cfg.data["discovery"]["rss"]["feeds"] = [{"url": "https://feeds.example/rss", "region": "SD"}]
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    yield cfg, conn, client
    conn.close()


@pytest.mark.parametrize("url,expected", [
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://m.youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://old.reddit.com/r/x/comments/abc123/some_title/", "https://www.reddit.com/comments/abc123"),
    ("HTTPS://Example.com/a/?utm_source=x&b=2&a=1#frag", "https://example.com/a?a=1&b=2"),
])
def test_canonicalize_url(url, expected):
    assert canonicalize_url(url) == expected


def test_parse_duration():
    assert youtube.parse_duration("PT1M5S") == 65
    assert youtube.parse_duration("PT2H") == 7200
    assert youtube.parse_duration("P0D") == 0
    assert youtube.parse_duration("garbage") is None


def test_trends_feed():
    items = trends.parse_feed(TRENDS_XML, "EG")
    assert [c.views for c in items] == [2000, 500]
    assert items[0].published_at == "2026-09-21T19:40:00Z"
    assert items[0].raw["news"][0]["url"] == "https://news.example/a"
    assert items[1].title == "ياسر إبراهيم"


def test_quota_budget_blocks_overspend(env):
    _, conn, _ = env
    now = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)   # still Sept 21 in Pacific time
    b = QuotaBudget(conn, "youtube", 150, now=now)
    assert b.day == "2026-09-21"
    b.spend(100)
    with pytest.raises(QuotaExceeded):
        b.spend(100)
    assert QuotaBudget(conn, "youtube", 150, now=now).used() == 100   # persisted across instances


def test_dedupe_merges_regions():
    a = Candidate("youtube", "x", "u", views=10, region="EG")
    b = Candidate("youtube", "x", "u", views=20, region="SA")
    [merged] = dedupe([a, b])
    assert merged.region == "EG,SA" and merged.views == 20


def test_upsert_refreshes_metrics(env):
    _, conn, _ = env
    c = Candidate("rss", "u1", "https://e.com/1", title="t", views=None)
    assert upsert_candidates(conn, [c]) == (1, 0)
    c.views = 99
    assert upsert_candidates(conn, [c]) == (0, 1)
    assert conn.execute("SELECT views FROM candidates").fetchone()[0] == 99


def test_discover_end_to_end(env, caplog):
    cfg, conn, client = env
    assert runner.discover(cfg, conn, client=client) == 0

    by_source = dict(conn.execute("SELECT source, COUNT(*) FROM candidates GROUP BY source").fetchall())
    # YouTube: 'aaa…' trends in EG+US (deduped), plus one per region, plus the search hit. SD 400s.
    assert by_source == {"youtube": 4, "reddit": 1, "trends": 2, "rss": 1}
    shared = conn.execute("SELECT region FROM candidates WHERE external_id = 'aaaaaaaaaaa'").fetchone()
    assert shared["region"] == "EG,US"

    run = conn.execute("SELECT status, notes FROM runs WHERE command = 'discover'").fetchone()
    assert run["status"] == "partial"                     # SD chart failed, others fine
    notes = json.loads(run["notes"])
    assert "regionCode not supported" in notes["youtube"]["errors"][0]
    # 3 charts × 1 + 3 searches × (100 + 1)
    assert QuotaBudget(conn, "youtube", 5000).used() == 306
    assert "SECRETKEY" not in caplog.text and "SECRETKEY" not in run["notes"]

    # Second run: nothing new, metrics refreshed.
    assert runner.discover(cfg, conn, client=client) == 0
    notes = json.loads(conn.execute("SELECT notes FROM runs ORDER BY id DESC").fetchone()["notes"])
    assert notes["reddit"] == {"fetched": 1, "new": 0, "updated": 1}


def test_discover_survives_missing_keys_and_quota(env, monkeypatch):
    cfg, conn, client = env
    monkeypatch.delenv("REDDIT_CLIENT_ID")
    cfg.data["discovery"]["youtube"]["daily_quota_budget"] = 50   # enough for charts, not searches
    assert runner.discover(cfg, conn, client=client) == 0
    notes = json.loads(conn.execute("SELECT notes FROM runs").fetchone()["notes"])
    assert "skipped" in notes["reddit"]
    assert any("exceed" in e for e in notes["youtube"]["errors"])
    assert notes["trends"]["new"] == 2


def test_discover_dry_run_makes_no_calls(env):
    cfg, conn, _ = env
    def boom(request):
        raise AssertionError("network call during dry run")
    client = httpx.Client(transport=httpx.MockTransport(boom))
    assert runner.discover(cfg, conn, dry_run=True, client=client) == 0
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_migration_adds_last_seen(tmp_path):
    conn = db.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, canonical_url TEXT UNIQUE)")
    db.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(candidates)")}
    assert "last_seen_at" in cols
