import json
import plistlib
import subprocess
from datetime import datetime, timezone

import httpx
import pytest

from src import db, lock, service
from src.analytics import collect, report as rep, runner
from src.config import load_config
from src.discover.common import Candidate, upsert_candidates
from src.rank.runner import pick


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET_FILE", "client_secret.json")
    for key in ("META_PAGE_ID", "META_IG_USER_ID", "META_PAGE_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(key, "")
    cfg = load_config()
    cfg.root = tmp_path
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


def _published(conn, vid, category, title, platform="youtube", ext=None, days_ago=1):
    n = conn.execute("SELECT count(*) FROM candidates").fetchone()[0] + 1
    upsert_candidates(conn, [Candidate(source="rss", external_id=f"e{n}", canonical_url=f"https://x.example/{n}")])
    cid = conn.execute("SELECT max(id) FROM candidates").fetchone()[0]
    conn.execute("UPDATE candidates SET category = ? WHERE id = ?", (category, cid))
    sid = conn.execute("INSERT INTO stories (candidate_id) VALUES (?)", (cid,)).lastrowid
    xid = conn.execute("INSERT INTO scripts (story_id, brand_id, body_ar, status, notes) VALUES (?, 'raij', 'نص', 'passed', ?)",
                       (sid, json.dumps({"hook_title": title, "series": "S-" + category}, ensure_ascii=False))).lastrowid
    conn.execute("INSERT INTO videos (id, script_id, status) VALUES (?, ?, 'published')", (vid, xid))
    aid = conn.execute("INSERT INTO approvals (video_id, decision) VALUES (?, 'approved')", (vid,)).lastrowid
    pid = conn.execute("INSERT INTO posts (video_id, approval_id, platform, external_id, status, published_at) "
                       "VALUES (?, ?, ?, ?, 'published', datetime('now', ?))",
                       (vid, aid, platform, ext or f"yt{vid}", f"-{days_ago} days")).lastrowid
    conn.commit()
    return pid


def _metric(conn, pid, views, likes=0, retention=None):
    conn.execute("INSERT INTO metrics (post_id, views, likes, retention_pct) VALUES (?, ?, ?, ?)",
                 (pid, views, likes, retention))
    conn.commit()


def _yt_auth(tmp):
    (tmp / "client_secret.json").write_text(json.dumps({"installed": {"client_id": "c", "client_secret": "s"}}))
    (tmp / "data").mkdir(exist_ok=True)
    (tmp / "data/youtube.token.json").write_text(json.dumps({"refresh_token": "rt"}))


# --- collection --------------------------------------------------------------

def test_youtube_metrics_combine_stats_and_analytics(env):
    cfg, conn, tmp = env
    _yt_auth(tmp)
    p1, p2 = _published(conn, 1, "tech", "أ"), _published(conn, 2, "sports", "ب")

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        if req.url.host == "youtubeanalytics.googleapis.com":
            assert req.url.params["ids"] == "channel==MINE" and req.url.params["filters"] == "video==yt1,yt2"
            return httpx.Response(200, json={"columnHeaders": [{"name": n} for n in
                                  ("video", "averageViewDuration", "averageViewPercentage", "shares")],
                                  "rows": [["yt1", 31.5, 64.2, 7]]})
        assert req.url.params["id"] == "yt1,yt2"
        return httpx.Response(200, json={"items": [
            {"id": "yt1", "statistics": {"viewCount": "1200", "likeCount": "80", "commentCount": "5"}},
            {"id": "yt2", "statistics": {"viewCount": "300"}}]})

    posts = [dict(r) for r in conn.execute("SELECT * FROM posts")]
    got = collect.youtube(cfg, httpx.Client(transport=httpx.MockTransport(handler)), posts)
    assert got[p1] == collect.Metric(1200, 80, 5, 7, 31.5, 64.2)
    assert got[p2] == collect.Metric(views=300)


def test_youtube_analytics_outage_keeps_counts(env):
    cfg, conn, tmp = env
    _yt_auth(tmp)
    pid = _published(conn, 1, "tech", "أ")

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        if req.url.host == "youtubeanalytics.googleapis.com":
            return httpx.Response(403, json={"error": {"message": "API not enabled"}})
        return httpx.Response(200, json={"items": [{"id": "yt1", "statistics": {"viewCount": "9"}}]})

    got = collect.youtube(cfg, httpx.Client(transport=httpx.MockTransport(handler)),
                          [dict(r) for r in conn.execute("SELECT * FROM posts")])
    assert got[pid].views == 9 and got[pid].retention_pct is None


def test_instagram_insights_parsed(env, monkeypatch):
    cfg, conn, _ = env
    for k, v in (("META_IG_USER_ID", "ig"), ("META_PAGE_ACCESS_TOKEN", "tok")):
        monkeypatch.setenv(k, v)
    pid = _published(conn, 1, "tech", "أ", platform="instagram", ext="m1")
    body = {"data": [{"name": "views", "values": [{"value": 500}]}, {"name": "likes", "total_value": {"value": 40}},
                     {"name": "ig_reels_avg_watch_time", "values": [{"value": 12500}]}]}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    got = collect.instagram(cfg, client, [dict(r) for r in conn.execute("SELECT * FROM posts")])
    assert got[pid] == collect.Metric(views=500, likes=40, avg_watch_s=12.5)


def test_report_stage_saves_one_row_per_post_per_day(env):
    cfg, conn, _ = env
    pid = _published(conn, 1, "tech", "أ")
    fake = {"youtube": lambda cfg, client, posts: {pid: collect.Metric(views=10)}}
    import src.analytics.collect as c
    orig = c.COLLECTORS
    c.COLLECTORS = fake
    try:
        for _ in range(2):
            assert runner.report(cfg, conn, client=httpx.Client(), weekly=False) == 0
    finally:
        c.COLLECTORS = orig
    assert conn.execute("SELECT count(*), max(views) FROM metrics").fetchone()[:] == (1, 10)


def test_one_platform_failing_is_partial(env):
    cfg, conn, _ = env
    _published(conn, 1, "tech", "أ")
    ig = _published(conn, 2, "tech", "ب", platform="instagram", ext="m2")
    import src.analytics.collect as c
    orig = c.COLLECTORS

    def boom(*a):
        raise RuntimeError("down")
    c.COLLECTORS = {"youtube": boom, "instagram": lambda cfg, client, posts: {ig: collect.Metric(views=3)}}
    try:
        assert runner.report(cfg, conn, client=httpx.Client(), weekly=False) == 0
    finally:
        c.COLLECTORS = orig
    notes = json.loads(conn.execute("SELECT notes FROM runs").fetchone()[0])
    assert notes["collected"] == {"instagram": 1} and "down" in notes["errors"]["youtube"]
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "partial"


# --- report, winners, feedback ------------------------------------------------

def test_weekly_report_totals_and_ranking(env):
    cfg, conn, _ = env
    _metric(conn, _published(conn, 1, "tech", "عطل ميتا"), 1200, 80, 64.0)
    _metric(conn, _published(conn, 2, "sports", "الفيفا"), 3000, 150, 71.0)
    _metric(conn, _published(conn, 3, "culture", "قديم", days_ago=12), 9999)          # outside the week
    text = rep.weekly_text(conn)
    assert "2 video(s) · 4,200 views · 230 likes" in text and "Avg watched on YouTube: 68%" in text
    # RTL-safe: the Arabic title stands alone on its line, the numbers follow on the next (U: report lines)
    assert text.index("1. الفيفا\n   3,000 views (3,000 youtube) · 71% watched") < text.index("2. عطل ميتا\n")
    assert "قديم" not in text and "Next picks lean toward: sports, tech" in text


def test_winners_feed_rank_and_script(env):
    cfg, conn, _ = env
    _metric(conn, _published(conn, 1, "sports", "الفيفا"), 3000)
    _published(conn, 2, "tech", "بدون مشاهدات")
    assert rep.winner_boost(conn, 0.15) == {"sports": 1.15} and rep.winner_boost(conn, 0) == {}
    assert '"الفيفا" (sports, 3,000 views)' in rep.winners_prompt(conn)
    rows = [{"id": 1, "score": 0.60, "retellable": 1, "status": "ranked", "category": "tech", "topic": "a"},
            {"id": 2, "score": 0.55, "retellable": 1, "status": "ranked", "category": "sports", "topic": "b"}]
    assert [r["id"] for r in pick(rows, 1, {"tech", "sports"}, 2)] == [1]
    assert [r["id"] for r in pick(rows, 1, {"tech", "sports"}, 2, boost={"sports": 1.15})] == [2]
    from src.script.write import build_prompt
    prompt = build_prompt(cfg, {"hook": "h", "key_facts": "[]", "claims": "[]"}, cfg.brands[0],
                          winners=rep.winners_prompt(conn))
    assert "performed best" in prompt and "الفيفا" in prompt
    assert "performed best" not in build_prompt(cfg, {"hook": "h", "key_facts": "[]", "claims": "[]"}, cfg.brands[0])


def test_weekly_report_sent_once_on_report_day(env):
    cfg, conn, _ = env
    sent = []
    bot = type("B", (), {"send_message": lambda self, chat, text, **kw: sent.append(text)})()
    monday = datetime(2026, 9, 28, 7, tzinfo=timezone.utc)
    tuesday = datetime(2026, 9, 29, 7, tzinfo=timezone.utc)
    runner.report(cfg, conn, client=httpx.Client(), bot=bot, now=tuesday)
    assert sent == []
    runner.report(cfg, conn, client=httpx.Client(), bot=bot, now=monday)
    runner.report(cfg, conn, client=httpx.Client(), bot=bot, now=monday)
    assert len(sent) == 1 and "weekly report" in sent[0]
    runner.report(cfg, conn, client=httpx.Client(), bot=bot, now=tuesday, weekly=True)   # /report forces it
    assert len(sent) == 2


def test_report_dry_run(env, caplog):
    cfg, conn, _ = env
    _published(conn, 1, "tech", "أ")
    caplog.set_level("INFO")
    assert runner.report(cfg, conn, dry_run=True, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(AssertionError("network"))))) == 0
    assert "1 youtube" in caplog.text and conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


# --- runner: locks + launchd ---------------------------------------------------

def test_lock_blocks_a_second_holder(tmp_path):
    with lock.single(tmp_path, "publish"):
        with pytest.raises(lock.Busy):
            with lock.single(tmp_path, "publish"):
                pass
    with lock.single(tmp_path, "publish"):                          # released afterwards
        pass


def test_publish_skips_while_another_publish_runs(env):
    cfg, conn, _ = env
    from src.publish import runner as pub
    with lock.single(cfg.root, "publish"):
        assert pub.publish(cfg, conn, client=httpx.Client()) == 0
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_service_plists_and_install(env, tmp_path, monkeypatch):
    cfg, _, _ = env
    monkeypatch.setattr(service, "linux", lambda: False)          # the launchd path, even when CI runs on Linux
    specs = service.plists(cfg)
    assert specs["com.raij.bot"]["KeepAlive"] is True and specs["com.raij.bot"]["ProgramArguments"][-1] == "bot"
    assert specs["com.raij.daily"]["StartCalendarInterval"] == {"Hour": 10, "Minute": 30}   # after Gemini's quota reset
    assert specs["com.raij.publish"]["StartInterval"] == 1800
    assert specs["com.raij.publish"]["ProgramArguments"][-2:] == ["publish", "--catch-up"]   # finishes leftovers
    assert all(s["WorkingDirectory"] == str(cfg.root) and "/opt/homebrew/bin" in s["EnvironmentVariables"]["PATH"]
               for s in specs.values())
    cmds = []

    def run(cmd):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    agents = tmp_path / "LaunchAgents"
    paths = service.install(cfg, run=run, agents=agents)
    assert len(paths) == 3 and plistlib.loads(paths[0].read_bytes())["Label"] == "com.raij.bot"
    assert [c[1] for c in cmds] == ["bootout", "bootstrap"] * 3
    assert service.uninstall(run=run, agents=agents) == list(service.LABELS) and not list(agents.iterdir())


def test_systemd_units_for_linux_server(env):
    cfg, _, _ = env
    u = service.units(cfg, user="ubuntu")
    assert set(u) == set(service.units_names())
    assert "Restart=always" in u["raij-bot.service"] and "User=ubuntu" in u["raij-bot.service"]
    assert "run python -m src.main bot" in u["raij-bot.service"] and f"WorkingDirectory={cfg.root}" in u["raij-bot.service"]
    assert "OnCalendar=*-*-* 10:30:00 Africa/Cairo" in u["raij-daily.timer"] and "Persistent=true" in u["raij-daily.timer"]
    assert "OnUnitInactiveSec=30min" in u["raij-publish.timer"] and "Type=oneshot" in u["raij-publish.service"]
    assert "src.main publish --catch-up" in u["raij-publish.service"]
    cmds = []

    def run(cmd):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    service._install_systemd(cfg, run, cfg.root / "units")
    assert sum(c[:2] == ["sudo", "install"] for c in cmds) == 5
    assert ["sudo", "systemctl", "enable", "--now", "raij-bot.service", "raij-daily.timer", "raij-publish.timer"] in cmds


# --- GitHub Actions mode -------------------------------------------------------

def test_state_bundle_roundtrip_keeps_live_media_only(env, monkeypatch, tmp_path):
    from src import state
    cfg, conn, root = env
    monkeypatch.setenv("RAIJ_STATE_KEY", "k" * 40)
    _published(conn, 1, "tech", "أ")
    conn.execute("UPDATE videos SET status = 'published'")
    vid = root / "assets/generated/video"
    vid.mkdir(parents=True)
    for n in (1, 2):
        (vid / f"{n}.mp4").write_bytes(b"v" * 10)
    (root / "assets/generated/voice").mkdir(parents=True)
    (root / "assets/generated/voice/9.wav").write_bytes(b"w")
    (root / "assets/generated/voice/9.words.json").write_text("{}")
    conn.execute("UPDATE videos SET video_path = 'assets/generated/video/1.mp4'")
    conn.execute("INSERT INTO videos (id, script_id, status, video_path, voice_path) VALUES "
                 "(2, 1, 'in_review', 'assets/generated/video/2.mp4', 'assets/generated/voice/9.wav')")
    conn.commit()
    bundle = state.pack(cfg, tmp_path / "s.enc")
    assert b"SQLite" not in bundle.read_bytes()[:64]                # encrypted
    other = tmp_path / "fresh"
    cfg.root = other
    cfg.db_path = other / "data/pipeline.db"
    names = state.unpack(cfg, bundle)
    assert sorted(names) == ["assets/generated/video/2.mp4", "assets/generated/voice/9.wav",
                             "assets/generated/voice/9.words.json", "data/pipeline.db"]      # published #1 dropped
    restored = db.connect(cfg.db_path)
    assert restored.execute("SELECT count(*) FROM videos").fetchone()[0] == 2
    monkeypatch.setenv("RAIJ_STATE_KEY", "x" * 40)
    with pytest.raises(state.StateError, match="wrong RAIJ_STATE_KEY"):
        state.unpack(cfg, bundle)


def test_daily_due_once_per_local_day(env):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.main import daily_due
    cfg, conn, _ = env
    cairo = ZoneInfo("Africa/Cairo")
    assert daily_due(cfg, conn, datetime(2026, 9, 23, 10, 29, tzinfo=cairo)) is None
    assert daily_due(cfg, conn, datetime(2026, 9, 23, 10, 35, tzinfo=cairo)) == "2026-09-23"
    db.set_flag(conn, "last_daily_run", "2026-09-23")
    assert daily_due(cfg, conn, datetime(2026, 9, 23, 22, 0, tzinfo=cairo)) is None
    assert daily_due(cfg, conn, datetime(2026, 9, 24, 11, 0, tzinfo=cairo)) == "2026-09-24"


def test_tiktok_goes_to_telegram_and_failure_fails_the_post(env, monkeypatch):
    from src.publish import tiktok
    from src.publish.common import PostText, PublishError
    from src.review import runner as review_runner
    cfg, _, root = env
    video = root / "v.mp4"
    video.write_bytes(b"x")
    sent = []

    class Bot:
        def send_video(self, chat, path, caption, **kw):
            sent.append(("video", caption))
            return {"message_id": 5}

        def send_message(self, chat, text, **kw):
            sent.append(("text", text, kw.get("reply_to_message_id")))

    monkeypatch.setattr(review_runner, "make_bot", lambda cfg: (Bot(), "chat"))
    posted = tiktok.export(cfg, None, video, PostText("t", "CAPTION", []), 7)
    assert posted.status == "exported" and sent[1] == ("text", "CAPTION", 5) and "TikTok — #7" in sent[0][1]

    class Down(Bot):
        def send_video(self, *a, **kw):
            from src.review.telegram import TelegramError
            raise TelegramError("sendVideo: HTTP 502")
    monkeypatch.setattr(review_runner, "make_bot", lambda cfg: (Down(), "chat"))
    with pytest.raises(PublishError, match="TikTok copy"):
        tiktok.export(cfg, None, video, PostText("t", "CAPTION", []), 7)
