"""Phase 12 — content strategy I: niche/region/fit weights, ad-safe gate, round-robin screening, posting
windows, SEO title/description/tags, playlists, CTA rotation, A/B titles."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from src import db
from src.assemble import brand
from src.config import load_config
from src.discover.common import Candidate, upsert_candidates
from src.publish import meta, runner, windows, youtube
from src.publish.common import Posted, PostText, PublishError, merge_tags, post_text
from src.rank import runner as rank
from src.rank.retellability import parse_verdicts
from src.rank.score import Scored
from src.script import write

KEYS = ("META_PAGE_ID", "META_IG_USER_ID", "META_PAGE_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "GEMINI_API_KEY", "GROQ_API_KEY")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET_FILE", "client_secret.json")
    for key in KEYS:
        monkeypatch.setenv(key, "")
    cfg = load_config()
    cfg.root = tmp_path
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


# --- classify: new fields ---------------------------------------------------------

def test_verdict_carries_fit_evergreen_ad_safe_format_with_safe_defaults():
    payload = {"items": [
        {"id": 1, "retellable": True, "category": "tech", "audience_fit": 5, "evergreen": True, "ad_safe": False,
         "format": "howto"},
        {"id": 2, "retellable": True, "category": "money", "audience_fit": "9", "format": "poem"},   # malformed
        {"id": 3, "retellable": True, "category": "tools"},                                          # missing
    ]}
    got = {v.id: v for v in parse_verdicts(payload, {1, 2, 3}, {"tech", "money", "tools"})}
    assert (got[1].audience_fit, got[1].evergreen, got[1].ad_safe, got[1].format) == (5, True, False, "howto")
    assert (got[2].audience_fit, got[2].format) == (5, None)                # clamped to 1–5, bad format dropped
    assert (got[3].audience_fit, got[3].evergreen, got[3].ad_safe, got[3].format) == (3, False, True, None)


def test_classify_prompt_lists_all_categories_and_the_audience(env):
    cfg, _, _ = env
    from src.rank.retellability import classify
    prompts = []

    def handler(req):
        prompts.append(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"items": []}'}]}}]})

    cfg.data["ranking"]["audience"] = "Gulf viewers"
    with pytest.MonkeyPatch.context() as m:
        m.setenv("GEMINI_API_KEY", "g")
        classify(cfg, [{"id": 1, "source": "rss", "title": "t", "raw_json": "{}"}],
                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "Gulf viewers" in prompts[0]
    assert "tech, money, wow-facts, life-hack, tools, news-lite, sports, culture, political" in prompts[0]
    assert "audience_fit" in prompts[0] and "ad_safe" in prompts[0]


# --- selection weights ------------------------------------------------------------

def _row(i, cat="tech", region="SA", score=0.6, fit=None, evergreen=0, ad_safe=1, topic=None):
    return {"id": i, "score": score, "retellable": 1, "status": "ranked", "category": cat, "region": region,
            "audience_fit": fit, "evergreen": evergreen, "ad_safe": ad_safe, "topic": topic or f"t{i}"}


def test_weights_multiply_category_region_fit_and_evergreen(env):
    cfg, _, _ = env
    cfg.data["ranking"].update(category_weights={"tech": 1.0, "life-hack": 1.05},
                               region_weights={"SA": 1.0, "EG": 0.8, "US": 0.7}, region_default_weight=0.5,
                               fit_weight=0.15, evergreen_bonus=0.1)
    w = rank.Weights(cfg)
    assert w.factor(_row(1)) == 1.0
    assert w.factor(_row(1, region="US")) == 0.7
    assert w.factor(_row(1, region="EG,SA")) == 1.0                    # trending in two regions: the best one counts
    assert w.factor(_row(1, region="ZZ")) == 0.5                       # unknown region → default
    assert w.factor(_row(1, region=None)) == 1.0                       # no region (YouTube search) → neutral
    assert w.factor(_row(1, fit=5)) == pytest.approx(1.3)
    assert w.factor(_row(1, fit=1)) == pytest.approx(0.7)
    assert w.factor(_row(1, cat="life-hack", evergreen=1)) == pytest.approx(1.05 * 1.1)
    assert rank.Weights().factor(_row(1, region="US", fit=5)) == 1.0    # no config → the pre-12 ordering


def test_pick_prefers_gulf_fit_and_skips_unsafe(env):
    cfg, _, _ = env
    cats = {"tech", "money", "wow-facts"}
    w = rank.Weights(cfg)
    rows = [_row(1, score=0.78, region="US", fit=1),                    # the NFL obituary: high score, no fit
            _row(2, score=0.59, region="SA", fit=5, cat="money"),       # Gulf money story
            _row(3, score=0.70, region="SA", fit=4, ad_safe=0),         # tragedy: never
            _row(4, score=0.59, region="US", fit=4, cat="wow-facts", evergreen=1)]
    picked = [r["id"] for r in rank.pick(rows, 3, cats, 2, weights=w)]
    assert picked[0] == 2 and 3 not in picked and set(picked) == {2, 4, 1}
    assert [r["id"] for r in rank.pick(rows, 3, cats, 2)][0] == 1        # without weights the old order holds


def test_screen_pool_round_robins_sources():
    rows = {i: {"id": i, "source": "trends" if i <= 5 else "rss", "retellable": None} for i in range(1, 11)}
    rows[2]["retellable"] = 1                                          # already screened
    scored = [Scored(i, 1 - i / 100, {}) for i in range(1, 11)]        # trends outscore every RSS item
    got = [r["id"] for r in rank.screen_pool(scored, rows, 6)]
    assert got == [1, 6, 3, 7, 4, 8]
    assert [r["id"] for r in rank.screen_pool(scored, rows, 20)] == [1, 6, 3, 7, 4, 8, 5, 9, 10]


def test_rank_stores_fit_fields_and_rejects_unsafe(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    upsert_candidates(conn, [
        Candidate(source="rss", external_id="a", canonical_url="https://x.example/a", title="safe", region="SA"),
        Candidate(source="rss", external_id="b", canonical_url="https://x.example/b", title="crash", region="SA"),
    ])

    def handler(req):
        prompt = json.loads(req.content)["contents"][0]["parts"][0]["text"]
        ids = {it["title"]: it["id"] for it in json.loads(prompt.split("Items (JSON):\n")[1].split("\n\nRespond")[0])}
        reply = {"items": [
            {"id": ids["safe"], "retellable": True, "category": "tech", "topic": "a", "reason": "r",
             "audience_fit": 4, "evergreen": True, "ad_safe": True, "format": "explainer"},
            {"id": ids["crash"], "retellable": True, "category": "tech", "topic": "b", "reason": "r",
             "audience_fit": 5, "ad_safe": False, "format": "story"}]}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]})

    assert rank.rank(cfg, conn, client=httpx.Client(transport=httpx.MockTransport(handler)), out_dir=tmp) == 0
    rows = {r["title"]: dict(r) for r in conn.execute("SELECT * FROM candidates")}
    assert rows["safe"]["status"] == "selected" and rows["crash"]["status"] == "rejected"
    assert (rows["safe"]["audience_fit"], rows["safe"]["evergreen"], rows["safe"]["ad_safe"],
            rows["safe"]["format"]) == (4, 1, 1, "explainer")
    report = json.loads(next(tmp.glob("*.json")).read_text(encoding="utf-8"))
    assert report["selected"][0]["audience_fit"] == 4 and report["selected"][0]["format"] == "explainer"


# --- posting windows --------------------------------------------------------------

def _set_windows(cfg, times, tz="Asia/Riyadh", minutes=150):
    cfg.data["publish"]["windows"] = {"timezone": tz, "times": times, "open_minutes": minutes}


def test_window_current_and_next(env):
    cfg, _, _ = env
    _set_windows(cfg, ["08:00", "21:00"])
    tz = ZoneInfo("Asia/Riyadh")
    at = lambda h, m=0: datetime(2026, 9, 23, h, m, tzinfo=tz).astimezone(timezone.utc)   # noqa: E731
    assert windows.current(cfg, at(7, 59)) is None
    w = windows.current(cfg, at(8, 0))
    assert w.label == "08:00" and w.start == at(8, 0) and w.end == at(10, 30)
    assert windows.current(cfg, at(10, 29)).label == "08:00"
    assert windows.current(cfg, at(10, 30)) is None
    assert windows.current(cfg, at(23, 29)).label == "21:00"
    late = datetime(2026, 9, 24, 0, 10, tzinfo=tz).astimezone(timezone.utc)                 # after midnight
    assert windows.current(cfg, late) is None
    _set_windows(cfg, ["23:00"], minutes=120)
    assert windows.current(cfg, late).label == "23:00"                                       # yesterday's window
    assert windows.next_start(cfg, at(12)) == at(23)
    assert windows.next_start(cfg, at(23, 30)) == datetime(2026, 9, 24, 23, tzinfo=tz).astimezone(timezone.utc)
    _set_windows(cfg, [])
    assert windows.current(cfg, at(8)) is None and windows.next_start(cfg) is None and not windows.enabled(cfg)


def _approved(cfg, conn, vid):
    from tests.test_publish import _video
    _video(cfg, conn, vid=vid)


class Fake:
    def __init__(self, fail=0):
        self.calls, self.fail = [], fail

    def missing(self, cfg):
        return None

    def __call__(self, cfg, client, path, text, video_id):
        self.calls.append(video_id)
        if self.fail:
            self.fail -= 1
            raise PublishError("boom")
        return Posted(f"ext{video_id}", f"https://p.example/{video_id}")


def _open_now(cfg):
    """A window that opened 10 minutes ago in the configured zone."""
    tz = ZoneInfo(cfg.get("publish.windows.timezone"))
    start = datetime.now(tz) - timedelta(minutes=10)
    _set_windows(cfg, [start.strftime("%H:%M")], tz=str(tz), minutes=120)


def test_one_video_per_window_oldest_first(env):
    cfg, conn, _ = env
    cfg.brands[0]["platforms"] = ["youtube"]
    _approved(cfg, conn, 1)
    _approved(cfg, conn, 2)
    yt = Fake()
    plats = {"youtube": (yt.missing, yt)}
    quiet = type("B", (), {"send_message": lambda self, chat, text, **kw: None})()

    # No window open → nothing goes out, nothing is written.
    _set_windows(cfg, ["00:00"], minutes=1)
    if windows.current(cfg):                                       # the one minute after midnight Riyadh
        _set_windows(cfg, ["12:00"], minutes=1)
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet) == 0
    assert yt.calls == [] and conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 0

    # Window open → exactly one video (the older approval), the other waits.
    _open_now(cfg)
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet) == 0
    assert yt.calls == [1]
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet) == 0
    assert yt.calls == [1]                                         # same window: still taken
    assert dict(conn.execute("SELECT id, status FROM videos")) == {1: "published", 2: "approved"}

    # The next window (simulated by ageing the first post before this window's start) takes video 2.
    conn.execute("UPDATE posts SET published_at = datetime('now', '-3 hours')")
    conn.commit()
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet) == 0
    assert yt.calls == [1, 2]


def test_failed_first_upload_leaves_the_window_free(env):
    cfg, conn, _ = env
    cfg.brands[0]["platforms"] = ["youtube"]
    _approved(cfg, conn, 1)
    _approved(cfg, conn, 2)
    yt = Fake(fail=1)
    plats = {"youtube": (yt.missing, yt)}
    quiet = type("B", (), {"send_message": lambda self, chat, text, **kw: None})()
    _open_now(cfg)
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet) == 0
    assert yt.calls == [1, 2]                                      # 1 failed (backs off), 2 used the window
    assert dict(conn.execute("SELECT video_id, status FROM posts")) == {1: "failed", 2: "published"}


def test_partly_published_video_finishes_outside_windows(env):
    cfg, conn, _ = env
    cfg.brands[0]["platforms"] = ["youtube", "instagram"]
    _approved(cfg, conn, 1)
    yt, ig = Fake(), Fake(fail=1)
    plats = {"youtube": (yt.missing, yt), "instagram": (ig.missing, ig)}
    quiet = type("B", (), {"send_message": lambda self, chat, text, **kw: None})()
    _open_now(cfg)
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet) == 0
    assert yt.calls == [1] and ig.calls == [1]                     # YouTube out, Instagram failed once
    # Backoff passed, but no window is open: the Instagram retry still goes (the video already started).
    conn.execute("UPDATE posts SET last_attempt_at = datetime('now', '-2 hours') WHERE platform = 'instagram'")
    conn.commit()
    _set_windows(cfg, ["00:00"], minutes=1)
    if windows.current(cfg):
        _set_windows(cfg, ["12:00"], minutes=1)
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet) == 0
    assert ig.calls == [1, 1]
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == "published"


def test_dry_run_reports_the_window(env, caplog):
    cfg, conn, _ = env
    _approved(cfg, conn, 1)
    _open_now(cfg)
    import logging
    with caplog.at_level(logging.INFO, logger="raij.publish"):
        assert runner.publish(cfg, conn, dry_run=True) == 0
    assert any("posting window:" in r.message and "(free)" in r.message for r in caplog.records)
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 0


# --- SEO: caption, title, tags, playlists -------------------------------------------

def _ctx(**over):
    base = {"id": 7, "notes": json.dumps({"series": "عالم التقنية", "credits": []}),
            "script_notes": json.dumps({"hook_title": "عطل مفاجئ يضرب ميتا", "hook_title_alt": "لماذا توقف فيسبوك؟"},
                                       ensure_ascii=False),
            "beats": json.dumps([{"role": "hook", "text": "هل توقف فيسبوك اليوم؟"}], ensure_ascii=False),
            "description_en": "Meta went down.", "hashtags": json.dumps(["#ميتا", "#tech"]),
            "sources": json.dumps(["https://www.skynewsarabia.com/a"]), "category": "tech"}
    base.update(over)
    return base


def test_post_text_has_arabic_first_lines_series_seo_tags_and_variant_b(env):
    cfg, _, _ = env
    text = post_text(_ctx(), cfg)
    assert text.title == "عطل مفاجئ يضرب ميتا" and text.title_alt == "لماذا توقف فيسبوك؟"
    assert text.series == "عالم التقنية" and text.hook_ar == "هل توقف فيسبوك اليوم؟"
    assert text.caption.split("\n\n")[:2] == ["عطل مفاجئ يضرب ميتا", "هل توقف فيسبوك اليوم؟"]
    assert text.caption_alt.split("\n\n")[:2] == ["لماذا توقف فيسبوك؟", "هل توقف فيسبوك اليوم؟"]
    assert text.hashtags[:2] == ["#ميتا", "#tech"]                        # script tags first …
    assert "#تقنية" in text.hashtags and "#رائج" in text.hashtags          # … then the niche set + defaults
    assert len(text.hashtags) == len(set(t.lower() for t in text.hashtags)) <= 15
    assert "المصادر: skynewsarabia.com" in text.caption_alt


def test_post_text_without_config_or_alt_keeps_the_old_shape():
    text = post_text(_ctx(script_notes=json.dumps({"hook_title": "عنوان"}), notes="{}"))
    assert text.hashtags == ["#ميتا", "#tech"] and text.title_alt is None and text.caption_alt is None
    assert text.series is None


def test_merge_tags_dedupes_case_insensitively():
    assert merge_tags(["#AI", "#tech"], ["#ai", "#Tech", "#Gulf"], limit=3) == ["#AI", "#tech", "#Gulf"]


def test_youtube_title_adds_series_within_the_limit():
    t = PostText("عطل مفاجئ يضرب ميتا", "c", [], series="عالم التقنية")
    assert youtube.title_for(t) == "عطل مفاجئ يضرب ميتا | عالم التقنية"
    assert youtube.title_for(PostText("x" * 95, "c", [], series="عالم التقنية")) == "x" * 95
    assert youtube.title_for(PostText("x" * 120, "c", [])) == "x" * 99 + "…"
    assert youtube.title_for(PostText("عنوان | عالم التقنية", "c", [], series="عالم التقنية")) == "عنوان | عالم التقنية"


def _yt_setup(tmp, scope="https://www.googleapis.com/auth/youtube"):
    (tmp / "client_secret.json").write_text(json.dumps({"installed": {
        "client_id": "cid", "client_secret": "csec", "token_uri": "https://oauth2.googleapis.com/token"}}))
    (tmp / "data").mkdir(exist_ok=True)
    (tmp / "data/youtube.token.json").write_text(json.dumps({"refresh_token": "rt", "scope": scope}))


def test_playlist_needs_the_manage_scope(env):
    cfg, _, tmp = env
    _yt_setup(tmp, scope="https://www.googleapis.com/auth/youtube.upload")
    assert not youtube.has_playlist_scope(cfg)
    boom = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError("no call"))))
    assert youtube.add_to_playlist(cfg, boom, {}, "vid", "عالم التقنية") is False
    _yt_setup(tmp)
    assert youtube.has_playlist_scope(cfg)


def test_upload_then_playlist_created_and_reused(env, monkeypatch):
    cfg, conn, tmp = env
    _yt_setup(tmp)
    monkeypatch.setattr(youtube, "_playlists", {})
    calls = []

    def handler(req):
        path = req.url.path
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        if path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if path.endswith("/playlistItems") and req.method == "GET":
            return httpx.Response(200, json={"items": []})
        if path.endswith("/upload/youtube/v3/videos"):
            assert json.loads(req.content)["snippet"]["title"] == "عطل مفاجئ يضرب ميتا | عالم التقنية"
            return httpx.Response(200, headers={"Location": "https://upload.example/s"})
        if req.url.host == "upload.example":
            return httpx.Response(200, json={"id": "vid1"})
        if path.endswith("/playlists") and req.method == "GET":
            calls.append("list")
            return httpx.Response(200, json={"items": [{"id": "PLother", "snippet": {"title": "هل تعلم؟"}}]})
        if path.endswith("/playlists") and req.method == "POST":
            calls.append("create")
            assert json.loads(req.content)["snippet"]["title"] == "عالم التقنية"
            return httpx.Response(200, json={"id": "PLtech"})
        if path.endswith("/playlistItems") and req.method == "POST":
            body = json.loads(req.content)["snippet"]
            calls.append(("insert", body["playlistId"], body["resourceId"]["videoId"]))
            return httpx.Response(200, json={"id": "pi1"})
        raise AssertionError(f"{req.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    video = tmp / "v.mp4"
    video.write_bytes(b"x" * 10)
    text = post_text(_ctx(), cfg)
    assert youtube.publish(cfg, client, video, text, 1).external_id == "vid1"
    assert calls == ["list", "create", ("insert", "PLtech", "vid1")]
    assert youtube.publish(cfg, client, video, text, 2).external_id == "vid1"      # duplicate-check path aside,
    assert calls[-1] == ("insert", "PLtech", "vid1") and calls.count("list") == 1  # the id is cached


def test_playlist_failure_never_fails_the_upload(env, monkeypatch, caplog):
    cfg, conn, tmp = env
    _yt_setup(tmp)
    monkeypatch.setattr(youtube, "_playlists", {})

    def handler(req):
        path = req.url.path
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        if path.endswith("/channels"):
            return httpx.Response(500)
        if path.endswith("/upload/youtube/v3/videos"):
            return httpx.Response(200, headers={"Location": "https://upload.example/s"})
        if req.url.host == "upload.example":
            return httpx.Response(200, json={"id": "vid1"})
        return httpx.Response(403, json={"error": {"message": "insufficient"}})

    video = tmp / "v.mp4"
    video.write_bytes(b"x" * 10)
    posted = youtube.publish(cfg, httpx.Client(transport=httpx.MockTransport(handler)), video, post_text(_ctx(), cfg), 1)
    assert posted.external_id == "vid1"
    assert any("Couldn't add vid1 to playlist" in r.message for r in caplog.records)


def test_instagram_gets_variant_b(env, monkeypatch):
    cfg, _, tmp = env
    for k, v in (("META_PAGE_ID", "pg"), ("META_IG_USER_ID", "ig1"), ("META_PAGE_ACCESS_TOKEN", "tok")):
        monkeypatch.setenv(k, v)
    seen = {}

    def handler(req):
        path = req.url.path
        if req.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        if path.endswith("/ig1/media"):
            seen["caption"] = dict(x.split("=", 1) for x in req.content.decode().split("&"))["caption"]
            return httpx.Response(200, json={"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/c1"})
        if path.endswith("/c1"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "m9"})
        return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/XYZ/"})

    video = tmp / "v.mp4"
    video.write_bytes(b"x" * 10)
    from urllib.parse import unquote_plus
    meta.publish_instagram(cfg, httpx.Client(transport=httpx.MockTransport(handler)), video, post_text(_ctx(), cfg), 1,
                           sleep=lambda s: None)
    assert unquote_plus(seen["caption"]).startswith("لماذا توقف فيسبوك؟")


# --- script: A/B titles, CTA rotation --------------------------------------------------

def _draft(n=100):
    from tests.test_script import _draft as base
    return base(n=n)


def test_hook_title_alt_is_kept_unless_it_copies_a(env):
    d = write.validate({**_draft(), "hook_title": "عطل يضرب ميتا", "hook_title_alt": "لماذا توقف فيسبوك؟"}, 85, 115)
    assert (d.hook_title, d.hook_title_alt) == ("عطل يضرب ميتا", "لماذا توقف فيسبوك؟")
    d = write.validate({**_draft(), "hook_title": "عطل يضرب ميتا", "hook_title_alt": "عطل يضرب ميتا."}, 85, 115)
    assert d.hook_title_alt is None
    d = write.validate({**_draft(), "hook_title": "عطل يضرب ميتا", "hook_title_alt": "هل سمعت الخبر"}, 85, 115)
    assert d.hook_title_alt is None                                    # equals the spoken hook
    row = write._row(d, None, "passed", 1, {})
    assert "hook_title_alt" not in row["notes"] and row["notes"]["hook_title"] == "عطل يضرب ميتا"


def test_cta_rotates_per_series_and_falls_back(env):
    cfg, _, _ = env
    b = cfg.brands[0]
    tech = b["cta"]["عالم التقنية"]
    assert write.cta_line(b, "عالم التقنية", 0) == tech[0] and write.cta_line(b, "عالم التقنية", 1) == tech[1]
    assert write.cta_line(b, "عالم التقنية", len(tech)) == tech[0]
    assert write.cta_line(b, "رياضة في دقيقة", 2) == b["cta"]["default"][2]        # no list for the series
    assert write.cta_line({"id": "x"}, None, 5) == "تابعنا للمزيد"
    assert write.cta_line(b, "أداة اليوم", 0) == "الرابط في الوصف"


def test_script_prompt_lists_series_with_closing_lines_and_asks_for_alt(env):
    cfg, _, _ = env
    story = {"id": 3, "hook": "h", "key_facts": "[]", "claims": "[]", "why_trending": "w"}
    prompt = write.build_prompt(cfg, story, cfg.brands[0])
    assert '"عالم التقنية" — closing line like: "' in prompt and "hook_title_alt" in prompt
    assert "Gulf colloquial touch" in prompt and "Gulf-friendly" in prompt


def test_endcard_draws_the_rotated_cta(env, tmp_path):
    cfg, _, _ = env
    look = cfg.brands[0]
    short = brand.endcard(look, tmp_path / "a.png", "أداة اليوم", "الرابط في الوصف")
    long = brand.endcard(look, tmp_path / "b.png", "هل تعلم؟", "شاركها مع صديق يهمه الموضوع")
    assert short.exists() and long.exists() and short.stat().st_size != long.stat().st_size


# --- config sanity -------------------------------------------------------------------

def test_config_locks_the_niche_and_gulf_market(env):
    cfg, _, _ = env
    assert cfg.get("ranking.categories") == ["tech", "money", "wow-facts", "life-hack", "tools"]
    assert "US" not in cfg.get("discovery.trends.geos") and "SA" in cfg.get("discovery.trends.geos")
    assert cfg.brands[0]["voice"]["name"] == "ar-SA-HamedNeural"
    series = cfg.brands[0]["series"]
    assert {"money", "tools"} <= set(series) and set(cfg.brands[0]["cta"]) >= {"default", series["tools"]}
    assert all(cat in youtube.CATEGORY_IDS for cat in cfg.get("ranking.categories"))
    feeds = cfg.get("discovery.rss.feeds")
    assert sum(1 for f in feeds if f["region"] == "SA") >= 5 and len(feeds) == len({f["url"] for f in feeds})


def test_post_now_skips_the_window_and_keeps_the_slot_free(env):
    """Owner's ask (2026-09-24): a rushed video goes out with no window open, and it doesn't use up the
    scheduled slot — the next window still takes the oldest waiting video."""
    cfg, conn, _ = env
    cfg.brands[0]["platforms"] = ["youtube"]
    for vid in (1, 2, 3):
        _approved(cfg, conn, vid)
    yt = Fake()
    plats = {"youtube": (yt.missing, yt)}
    quiet = type("B", (), {"send_message": lambda self, chat, text, **kw: None})()
    _set_windows(cfg, ["00:00"], minutes=1)
    if windows.current(cfg):
        _set_windows(cfg, ["12:00"], minutes=1)
    assert runner.rush(conn, [3, 99]) == [3]                     # 99 doesn't exist
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet) == 0
    assert yt.calls == [3]                                       # no window open, only the rushed one went
    assert dict(conn.execute("SELECT id, status FROM videos")) == {1: "approved", 2: "approved", 3: "published"}
    _open_now(cfg)
    assert windows.used(conn, windows.current(cfg)) == 0         # the rushed video isn't the window's video
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet) == 0
    assert yt.calls == [3, 1]
    # `publish --now` rushes everything that's left.
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet, now=True) == 0
    assert yt.calls == [3, 1, 2]


def test_per_window_capacity(env):
    cfg, conn, _ = env
    cfg.brands[0]["platforms"] = ["youtube"]
    for vid in (1, 2, 3):
        _approved(cfg, conn, vid)
    yt = Fake()
    plats = {"youtube": (yt.missing, yt)}
    quiet = type("B", (), {"send_message": lambda self, chat, text, **kw: None})()
    _open_now(cfg)
    cfg.data["publish"]["windows"]["per_window"] = 2
    assert windows.per_window(cfg) == 2
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet) == 0
    assert yt.calls == [1, 2]
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=quiet) == 0
    assert yt.calls == [1, 2]                                    # window full
    assert windows.taken(conn, windows.current(cfg), 2) and not windows.taken(conn, windows.current(cfg), 3)
