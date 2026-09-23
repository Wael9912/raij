import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src import db
from src.config import load_config
from src.discover.common import Candidate, upsert_candidates
from src.publish import meta, runner, youtube
from src.publish.common import Posted, PublishError, post_text

KEYS = ("META_PAGE_ID", "META_IG_USER_ID", "META_PAGE_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


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


def _video(cfg, conn, vid=1, decisions=("approved",), status="approved"):
    """A rendered video row with a real file and the given review decisions (for that video id)."""
    if not conn.execute("SELECT 1 FROM candidates").fetchone():
        upsert_candidates(conn, [Candidate(source="rss", external_id="e1", canonical_url="https://x.example/1")])
        conn.execute("UPDATE candidates SET category = 'tech'")
        conn.execute("INSERT INTO stories (candidate_id, sources) VALUES (1, ?)",
                     (json.dumps(["https://www.skynewsarabia.com/a", "https://bbc.com/b"]),))
        conn.execute("INSERT INTO scripts (story_id, brand_id, body_ar, beats, description_en, hashtags, status, notes) "
                     "VALUES (1, 'raij', 'نص', ?, 'Meta went down.', ?, 'passed', ?)",
                     (json.dumps([{"role": "hook", "text": "هل توقف فيسبوك؟"}], ensure_ascii=False),
                      json.dumps(["#ميتا", "#tech"], ensure_ascii=False),
                      json.dumps({"hook_title": "عطل مفاجئ يضرب ميتا"}, ensure_ascii=False)))
    out = cfg.root / "assets/generated/video"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{vid}.mp4").write_bytes(b"x" * 1000)
    conn.execute("INSERT INTO videos (id, script_id, video_path, status, notes) VALUES (?, 1, ?, ?, ?)",
                 (vid, f"assets/generated/video/{vid}.mp4", status,
                  json.dumps({"credits": ["Photo: Jane / CC BY 4.0 via Wikimedia Commons"]})))
    for d in decisions:
        conn.execute("INSERT INTO approvals (video_id, decision, decided_by) VALUES (?, ?, 'owner')", (vid, d))
    conn.commit()


class Fake:
    """A platform publisher stub that records calls and can fail."""

    def __init__(self, fail=0, missing=None):
        self.calls, self.fail, self.why = [], fail, missing

    def missing(self, cfg):
        return self.why

    def __call__(self, cfg, client, path, text, video_id):
        self.calls.append((path.name, text.title, video_id))
        if self.fail:
            self.fail -= 1
            raise PublishError("boom")
        return Posted(f"ext{video_id}", f"https://p.example/{video_id}")


def _platforms(**fakes):
    return {name: (f.missing, f) for name, f in fakes.items()}


def _brand_platforms(cfg, names):
    cfg.brands[0]["platforms"] = list(names)


# --- the approval gate -------------------------------------------------------

def test_publishes_only_videos_approved_by_their_own_row(env):
    cfg, conn, _ = env
    _video(cfg, conn, 1)                                         # approved ✔
    _video(cfg, conn, 2, decisions=())                           # status approved but no approval row
    _video(cfg, conn, 3, decisions=("approved", "rejected"))     # later rejected
    _video(cfg, conn, 4, decisions=("approved",), status="superseded")
    _video(cfg, conn, 5, decisions=("rejected", "approved"))     # re-approved ✔
    conn.execute("INSERT INTO approvals (video_id, decision) VALUES (1, 'new_broll')")   # not a verdict
    conn.execute("INSERT INTO videos (id, script_id, video_path, status) VALUES (6, 1, 'x.mp4', 'approved')")
    conn.commit()
    assert [v["id"] for v in runner.eligible(conn)] == [1, 5]


def test_approval_older_than_max_age_is_not_published(env):
    cfg, conn, _ = env
    _video(cfg, conn, 1)
    conn.execute("UPDATE approvals SET decided_at = datetime('now', '-4 days')")
    conn.commit()
    assert runner.eligible(conn, max_age_hours=72) == [] and len(runner.eligible(conn)) == 1


def test_pause_publishes_nothing(env):
    cfg, conn, _ = env
    _video(cfg, conn)
    db.set_flag(conn, "publishing_paused", "1")
    yt = Fake()
    assert runner.publish(cfg, conn, platforms=_platforms(youtube=yt), client=httpx.Client()) == 0
    assert yt.calls == [] and conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 0


def test_pause_notice_is_sent_once_until_resume(env):
    """A9: every 10-min tick while paused must not repeat the '⏸ waiting' message."""
    cfg, conn, _ = env
    _video(cfg, conn)
    db.set_flag(conn, "publishing_paused", "1")
    sent = []
    bot = type("B", (), {"send_message": lambda self, chat, text, **kw: sent.append(text)})()
    for _ in range(3):
        runner.publish(cfg, conn, platforms=_platforms(youtube=Fake()), client=httpx.Client(), bot=bot)
    assert len(sent) == 1 and "paused" in sent[0]
    db.set_flag(conn, "paused_notice_sent", "0")                  # what /resume and `resume` do
    runner.publish(cfg, conn, platforms=_platforms(youtube=Fake()), client=httpx.Client(), bot=bot)
    assert len(sent) == 2


# --- closing approved videos (A3) -------------------------------------------

def _age(conn, hours):
    conn.execute("UPDATE approvals SET decided_at = datetime('now', ?)", (f"-{hours} hours",))
    conn.commit()


def test_stale_approved_video_with_a_post_is_closed_as_published(env):
    cfg, conn, _ = env
    _video(cfg, conn)
    _brand_platforms(cfg, ["youtube", "instagram"])
    yt, ig = Fake(), Fake(missing="no Meta keys")
    plats = _platforms(youtube=yt, instagram=ig)
    sent = []
    bot = type("B", (), {"send_message": lambda self, chat, text, **kw: sent.append(text)})()
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=bot) == 0
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == "approved"     # still owes Instagram
    _age(conn, 73)
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=bot) == 0
    row = conn.execute("SELECT status, notes FROM videos").fetchone()
    assert row["status"] == "published"
    notes = json.loads(row["notes"])["publish"]
    assert notes["done"] == ["youtube"] and notes["skipped"] == {"instagram": "no Meta keys"}
    assert notes["closed_at"] and notes and json.loads(row["notes"])["credits"]      # older notes kept
    assert any("#1 closed as published" in t for t in sent)
    assert len(yt.calls) == 1 and ig.calls == []
    # Idempotent: a closed video is never touched again.
    runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=bot)
    assert len(sent) == 2


def test_stale_approved_video_with_nothing_out_expires(env):
    cfg, conn, _ = env
    _video(cfg, conn)
    _brand_platforms(cfg, ["youtube"])
    yt = Fake(missing="not authorized")
    _age(conn, 100)
    runner.publish(cfg, conn, platforms=_platforms(youtube=yt), client=httpx.Client())
    row = conn.execute("SELECT status, notes FROM videos").fetchone()
    assert row["status"] == "expired" and json.loads(row["notes"])["publish"]["skipped"] == {"youtube": "not authorized"}


def test_finalize_now_closes_only_what_cannot_progress(env):
    """`finalize` (max_age None): a video waiting on missing keys or dead retries closes; one with a
    configured platform still to try is left alone."""
    cfg, conn, _ = env
    _video(cfg, conn, 1)
    _video(cfg, conn, 2)
    _brand_platforms(cfg, ["youtube", "facebook"])
    conn.execute("INSERT INTO posts (video_id, approval_id, platform, status) VALUES (1, 1, 'youtube', 'published')")
    conn.execute("INSERT INTO posts (video_id, approval_id, platform, status, attempts, error) "
                 "VALUES (2, 2, 'youtube', 'failed', 3, 'boom')")
    conn.commit()
    plats = _platforms(youtube=Fake(), facebook=Fake(missing="no Meta keys"))
    assert runner.finalize(cfg, conn, None, dry_run=True, platforms=plats) == [(1, "published"), (2, "expired")]
    assert {r[0] for r in conn.execute("SELECT status FROM videos")} == {"approved"}          # dry run
    plats = _platforms(youtube=Fake(), facebook=Fake())                                      # FB now configured
    assert runner.finalize(cfg, conn, None, platforms=plats) == []                           # both can still post
    plats = _platforms(youtube=Fake(), facebook=Fake(missing="no Meta keys"))
    assert runner.finalize(cfg, conn, None, platforms=plats) == [(1, "published"), (2, "expired")]
    assert json.loads(conn.execute("SELECT notes FROM videos WHERE id = 2").fetchone()[0])["publish"]["skipped"] == \
        {"youtube": "boom", "facebook": "no Meta keys"}
    assert runner.eligible(conn) == []


def test_guardrail_refuses_file_outside_generated(env):
    cfg, conn, tmp = env
    _video(cfg, conn)
    (tmp / "data").mkdir(exist_ok=True)
    conn.execute("UPDATE videos SET video_path = 'data/source.mp4'")
    conn.commit()
    from src.assemble.render import GuardrailError
    with pytest.raises(GuardrailError):
        runner.publish(cfg, conn, platforms=_platforms(youtube=Fake()), client=httpx.Client())


# --- runner ------------------------------------------------------------------

def test_publish_all_platforms_then_idempotent(env):
    cfg, conn, _ = env
    _video(cfg, conn)
    _brand_platforms(cfg, ["youtube", "instagram"])
    yt, ig = Fake(), Fake()
    sent = []
    bot = type("B", (), {"send_message": lambda self, chat, text, **kw: sent.append(text)})()
    assert runner.publish(cfg, conn, platforms=_platforms(youtube=yt, instagram=ig), client=httpx.Client(), bot=bot) == 0
    rows = conn.execute("SELECT platform, status, external_id, approval_id, attempts FROM posts ORDER BY platform").fetchall()
    assert [tuple(r) for r in rows] == [("instagram", "published", "ext1", 1, 1), ("youtube", "published", "ext1", 1, 1)]
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == "published"
    assert yt.calls == [("1.mp4", "عطل مفاجئ يضرب ميتا", 1)]
    assert "✅ #1 «عطل مفاجئ يضرب ميتا»\nYouTube: https://p.example/1" in sent[0]      # title + link (C)
    assert runner.publish(cfg, conn, platforms=_platforms(youtube=yt, instagram=ig), client=httpx.Client(), bot=bot) == 0
    assert len(yt.calls) == 1 and len(ig.calls) == 1              # never redone


def test_unconfigured_platform_is_skipped_without_a_row(env):
    cfg, conn, _ = env
    _video(cfg, conn)
    _brand_platforms(cfg, ["youtube", "tiktok_export"])
    yt, tk = Fake(missing="no keys"), Fake()
    assert runner.publish(cfg, conn, platforms=_platforms(youtube=yt, tiktok_export=tk), client=httpx.Client()) == 0
    assert [r[0] for r in conn.execute("SELECT platform FROM posts")] == ["tiktok_export"] and yt.calls == []
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == "approved"    # still owes YouTube
    notes = json.loads(conn.execute("SELECT notes FROM runs").fetchone()[0])
    assert notes["skipped"] == {"youtube": "no keys"}


def test_partial_failure_retries_then_alerts_once(env):
    cfg, conn, _ = env
    _video(cfg, conn)
    _brand_platforms(cfg, ["youtube", "facebook"])
    yt, fb = Fake(), Fake(fail=5)
    sent, markups = [], []
    bot = type("B", (), {"send_message": lambda self, chat, text, **kw: (sent.append(text),
                                                                          markups.append(kw.get("reply_markup")))})()
    plats = _platforms(youtube=yt, facebook=fb)
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=bot) == 0     # partial
    for _ in range(4):
        _rewind(conn, 24)                                            # past every backoff step (A8)
        runner.publish(cfg, conn, platforms=plats, client=httpx.Client(), bot=bot)
    post = dict(conn.execute("SELECT * FROM posts WHERE platform = 'facebook'").fetchone())
    assert post["status"] == "failed" and post["attempts"] == 3 and "boom" in post["error"]
    assert len(fb.calls) == 3 and len(yt.calls) == 1
    alerts = [(t, m) for t, m in zip(sent, markups) if "giving up" in t]
    assert len(alerts) == 1 and "Facebook" in alerts[0][0] and "«عطل مفاجئ يضرب ميتا»" in alerts[0][0]
    assert alerts[0][1]["inline_keyboard"][0][0]["callback_data"] == "rt:1"           # 🔁 Retry button (U9)


def test_failure_then_success_on_retry(env):
    cfg, conn, _ = env
    _video(cfg, conn)
    _brand_platforms(cfg, ["youtube"])
    yt = Fake(fail=1)
    assert runner.publish(cfg, conn, platforms=_platforms(youtube=yt), client=httpx.Client()) == 1
    _rewind(conn, 1)                                                  # first backoff step (A8)
    assert runner.publish(cfg, conn, platforms=_platforms(youtube=yt), client=httpx.Client()) == 0
    post = dict(conn.execute("SELECT * FROM posts").fetchone())
    assert post["status"] == "published" and post["attempts"] == 2 and post["error"] is None


def test_dry_run_touches_nothing(env, caplog):
    cfg, conn, _ = env
    _video(cfg, conn)
    caplog.set_level("INFO")
    yt = Fake(missing="no keys")
    boom = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError("network"))))
    assert runner.publish(cfg, conn, dry_run=True, platforms=_platforms(youtube=yt), client=boom) == 0
    assert "skipped — no keys" in caplog.text and "video 1 (approval 1)" in caplog.text
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0 and yt.calls == []


def test_tiktok_export_copies_video_and_caption(env):
    cfg, conn, tmp = env
    _video(cfg, conn)
    _brand_platforms(cfg, ["tiktok_export"])
    assert runner.publish(cfg, conn, client=httpx.Client()) == 0
    post = dict(conn.execute("SELECT * FROM posts").fetchone())
    assert post["status"] == "exported"
    exported = tmp / post["url"]
    assert exported.read_bytes() == b"x" * 1000
    caption = exported.with_suffix(".txt").read_text(encoding="utf-8")
    assert caption.startswith("عطل مفاجئ يضرب ميتا") and "📷 Photo: Jane" in caption


def test_caption_has_headline_tags_sources_and_credits(env):
    cfg, conn, _ = env
    _video(cfg, conn)
    text = post_text(runner.eligible(conn)[0])
    assert text.title == "عطل مفاجئ يضرب ميتا" and text.category == "tech"
    assert "#ميتا #tech" in text.caption and "المصادر: bbc.com، skynewsarabia.com" in text.caption
    assert text.caption.rstrip().endswith("📷 Photo: Jane / CC BY 4.0 via Wikimedia Commons")


# --- YouTube -----------------------------------------------------------------

def _yt_setup(tmp):
    (tmp / "client_secret.json").write_text(json.dumps({"installed": {
        "client_id": "cid", "client_secret": "csec", "token_uri": "https://oauth2.googleapis.com/token"}}))
    (tmp / "data").mkdir(exist_ok=True)
    (tmp / "data/youtube.token.json").write_text(json.dumps({"refresh_token": "rt"}))


def test_youtube_missing_explains_next_step(env):
    cfg, _, tmp = env
    assert "SETUP.md §6" in youtube.missing(cfg)
    (tmp / "client_secret.json").write_text("{}")
    assert "youtube-auth" in youtube.missing(cfg)


def test_youtube_resumable_upload_in_chunks(env, monkeypatch):
    cfg, conn, tmp = env
    _yt_setup(tmp)
    _video(cfg, conn)
    monkeypatch.setattr(youtube, "CHUNK", 400)
    seen = []

    def handler(req):
        seen.append((req.method, req.url.host, req.headers.get("content-range")))
        if req.url.host == "oauth2.googleapis.com":
            assert b"refresh_token=rt" in req.content and b"grant_type=refresh_token" in req.content
            return httpx.Response(200, json={"access_token": "at"})
        assert req.headers["authorization"] == "Bearer at"
        if req.method == "GET":                               # duplicate check: channel + recent uploads
            if "channels" in req.url.path:
                return httpx.Response(200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
            assert req.url.params["playlistId"] == "UU1"
            return httpx.Response(200, json={"items": [{"snippet": {"title": "other #Shorts", "publishedAt": "2099-01-01T00:00:00Z",
                                                                    "resourceId": {"videoId": "zzz"}}}]})
        if req.method == "POST":
            meta_ = json.loads(req.content)
            assert meta_["snippet"]["title"] == "عطل مفاجئ يضرب ميتا #Shorts"
            assert meta_["snippet"]["categoryId"] == "28" and "📷 Photo: Jane" in meta_["snippet"]["description"]
            assert meta_["status"]["selfDeclaredMadeForKids"] is False
            assert req.headers["x-upload-content-length"] == "1000"
            return httpx.Response(200, headers={"Location": "https://upload.example/session1"})
        end = int(req.headers["content-range"].split("-")[1].split("/")[0])
        if end < 999:
            return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})
        return httpx.Response(200, json={"id": "abc123"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    posted = youtube.publish(cfg, client, tmp / "assets/generated/video/1.mp4", post_text(runner.eligible(conn)[0]), 1)
    assert posted.url == "https://youtube.com/shorts/abc123"
    assert [s[2] for s in seen if s[0] == "PUT"] == ["bytes 0-399/1000", "bytes 400-799/1000", "bytes 800-999/1000"]


def test_youtube_adopts_a_recent_upload_with_the_same_title(env, monkeypatch):
    """A10: the state saved after an upload was lost (cache save failed / stale bootstrap) — the next attempt
    must find the video on the channel instead of uploading it twice."""
    cfg, conn, tmp = env
    _yt_setup(tmp)
    _video(cfg, conn)
    uploads = []
    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        if req.method == "GET" and "channels" in req.url.path:
            return httpx.Response(200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if req.method == "GET":
            return httpx.Response(200, json={"items": [
                {"snippet": {"title": "عطل مفاجئ يضرب ميتا #Shorts", "publishedAt": "2020-01-01T00:00:00Z",
                             "resourceId": {"videoId": "old"}}},
                {"snippet": {"title": "عطل مفاجئ يضرب ميتا #Shorts", "publishedAt": recent,
                             "resourceId": {"videoId": "dup1"}}}]})
        uploads.append(req.method)
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    posted = youtube.publish(cfg, client, tmp / "assets/generated/video/1.mp4", post_text(runner.eligible(conn)[0]), 1)
    assert posted.external_id == "dup1" and uploads == []


def test_youtube_uploads_when_the_duplicate_check_fails(env, monkeypatch):
    cfg, conn, tmp = env
    _yt_setup(tmp)
    _video(cfg, conn)

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        if req.method == "GET":
            return httpx.Response(403, json={"error": "quotaExceeded"})
        if req.method == "POST":
            return httpx.Response(200, headers={"Location": "https://upload.example/s"})
        return httpx.Response(200, json={"id": "new1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    posted = youtube.publish(cfg, client, tmp / "assets/generated/video/1.mp4", post_text(runner.eligible(conn)[0]), 1)
    assert posted.external_id == "new1"


def test_youtube_revoked_token_says_reauth(env):
    cfg, conn, tmp = env
    _yt_setup(tmp)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": "invalid_grant"})))
    with pytest.raises(PublishError, match="youtube-auth"):
        youtube.access_token(cfg, client)


def test_youtube_authorize_loopback_flow(env):
    cfg, _, tmp = env
    _yt_setup(tmp)
    (tmp / "data/youtube.token.json").unlink()
    import threading
    from urllib.parse import parse_qs, urlsplit

    def browser(url):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert q["code_challenge_method"] == "S256" and "youtube.upload" in q["scope"]
        threading.Thread(target=lambda: httpx.get(f"{q['redirect_uri']}/?code=C0DE&state={q['state']}")).start()

    def handler(req):
        body = parse_qs(req.content.decode())
        assert body["code"] == ["C0DE"] and body["code_verifier"][0]
        return httpx.Response(200, json={"refresh_token": "new-rt", "access_token": "at"})

    path = youtube.authorize(cfg, httpx.Client(transport=httpx.MockTransport(handler)), open_browser=browser, timeout=10)
    assert json.loads(path.read_text())["refresh_token"] == "new-rt" and oct(path.stat().st_mode)[-3:] == "600"


# --- Meta --------------------------------------------------------------------

def _meta_env(monkeypatch):
    for k, v in (("META_PAGE_ID", "page1"), ("META_IG_USER_ID", "ig1"), ("META_PAGE_ACCESS_TOKEN", "tok")):
        monkeypatch.setenv(k, v)


def test_instagram_reels_resumable_flow(env, monkeypatch):
    cfg, conn, tmp = env
    _meta_env(monkeypatch)
    _video(cfg, conn)
    statuses = iter(["IN_PROGRESS", "FINISHED"])
    calls = []

    def handler(req):
        path = req.url.path
        calls.append((req.method, req.url.host, path))
        if req.url.host == "rupload.facebook.com":
            assert req.headers["authorization"] == "OAuth tok" and req.headers["file_size"] == "1000"
            return httpx.Response(200, json={"success": True})
        if path.endswith("/ig1/media"):
            form = parse(req)
            assert form["media_type"] == "REELS" and form["upload_type"] == "resumable"
            assert form["caption"].startswith("عطل مفاجئ")
            return httpx.Response(200, json={"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/c1"})
        if path.endswith("/c1"):
            return httpx.Response(200, json={"status_code": next(statuses)})
        if path.endswith("/media_publish"):
            assert parse(req)["creation_id"] == "c1"
            return httpx.Response(200, json={"id": "m9"})
        if path.endswith("/m9"):
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/XYZ/"})
        raise AssertionError(path)

    posted = meta.publish_instagram(cfg, httpx.Client(transport=httpx.MockTransport(handler)),
                                    tmp / "assets/generated/video/1.mp4", post_text(runner.eligible(conn)[0]), 1,
                                    sleep=lambda s: None)
    assert (posted.external_id, posted.url) == ("m9", "https://www.instagram.com/reel/XYZ/")
    assert [c[2].rsplit("/", 1)[-1] for c in calls] == ["media", "c1", "c1", "c1", "media_publish", "m9"]


def test_instagram_processing_error_fails(env, monkeypatch):
    cfg, conn, tmp = env
    _meta_env(monkeypatch)
    _video(cfg, conn)

    def handler(req):
        if req.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "c1", "uri": "https://rupload.facebook.com/x/c1"})
        return httpx.Response(200, json={"status_code": "ERROR", "status": "Video too long"})

    with pytest.raises(PublishError, match="Video too long"):
        meta.publish_instagram(cfg, httpx.Client(transport=httpx.MockTransport(handler)),
                               tmp / "assets/generated/video/1.mp4", post_text(runner.eligible(conn)[0]), 1,
                               sleep=lambda s: None)


def test_facebook_reels_flow_and_publish_is_not_retried(env, monkeypatch):
    cfg, conn, tmp = env
    _meta_env(monkeypatch)
    _video(cfg, conn)
    finishes = []

    def handler(req):
        if req.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        form = parse(req)
        if form["upload_phase"] == "start":
            return httpx.Response(200, json={"video_id": "v5", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/v5"})
        finishes.append(form)
        return httpx.Response(200, json={"success": True}) if len(finishes) == 1 else httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    video, text = tmp / "assets/generated/video/1.mp4", post_text(runner.eligible(conn)[0])
    posted = meta.publish_facebook(cfg, client, video, text, 1)
    assert posted.url == "https://www.facebook.com/reel/v5" and finishes[0]["video_state"] == "PUBLISHED"
    from src.discover.common import FetchError
    with pytest.raises(FetchError):
        meta.publish_facebook(cfg, client, video, text, 1)
    assert len(finishes) == 2                                   # the 503 finish was not retried


def test_meta_errors_never_leak_the_token(env, monkeypatch):
    cfg, conn, tmp = env
    _meta_env(monkeypatch)
    _video(cfg, conn)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": {"message": "Invalid OAuth access token"}})))
    from src.discover.common import FetchError
    with pytest.raises(FetchError) as exc:
        meta.publish_instagram(cfg, client, tmp / "assets/generated/video/1.mp4", post_text(runner.eligible(conn)[0]), 1)
    assert "tok" not in str(exc.value).replace("token", "") and "Invalid OAuth" in str(exc.value)


def parse(req):
    from urllib.parse import parse_qs
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


# --- Phase 10b (A8, A12) -----------------------------------------------------

def test_retry_due_follows_the_backoff_schedule():
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    stamp = lambda h: (now - timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")     # noqa: E731
    assert runner.retry_due({"attempts": 0, "last_attempt_at": None}, [1, 6, 24], now)
    assert not runner.retry_due({"attempts": 1, "last_attempt_at": stamp(0.5)}, [1, 6, 24], now)
    assert runner.retry_due({"attempts": 1, "last_attempt_at": stamp(1)}, [1, 6, 24], now)
    assert not runner.retry_due({"attempts": 2, "last_attempt_at": stamp(5)}, [1, 6, 24], now)
    assert runner.retry_due({"attempts": 2, "last_attempt_at": stamp(6)}, [1, 6, 24], now)
    assert not runner.retry_due({"attempts": 5, "last_attempt_at": stamp(23)}, [1, 6, 24], now)   # last repeats
    assert runner.retry_due({"attempts": 1, "last_attempt_at": stamp(0)}, [], now)


def _rewind(conn, hours):
    conn.execute("UPDATE posts SET last_attempt_at = datetime(last_attempt_at, ?)", (f"-{hours} hours",))
    conn.commit()


def test_failed_post_waits_for_its_backoff(env):
    cfg, conn, _ = env
    _video(cfg, conn)
    _brand_platforms(cfg, ["youtube"])
    yt = Fake(fail=2)
    plats = _platforms(youtube=yt)
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client()) == 1
    for _ in range(3):                                            # ticks 10 minutes apart: no retry yet
        assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client()) == 0
    assert len(yt.calls) == 1 and conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    _rewind(conn, 1)                                              # an hour later: second attempt
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client()) == 1
    assert len(yt.calls) == 2
    runner.publish(cfg, conn, platforms=plats, client=httpx.Client())
    assert len(yt.calls) == 2                                     # 6 h backoff now
    _rewind(conn, 6)
    assert runner.publish(cfg, conn, platforms=plats, client=httpx.Client()) == 0
    post = dict(conn.execute("SELECT * FROM posts").fetchone())
    assert post["status"] == "published" and post["attempts"] == 3 and len(yt.calls) == 3


def test_youtube_upload_stalled_by_308s_gives_up(env, monkeypatch):
    cfg, conn, tmp = env
    _yt_setup(tmp)
    _video(cfg, conn)
    monkeypatch.setattr(youtube, "CHUNK", 400)
    puts = []

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        if req.method == "GET":
            return httpx.Response(200, json={"items": []})
        if req.method == "POST":
            return httpx.Response(200, headers={"Location": "https://upload.example/s"})
        puts.append(req.headers["content-range"])
        return httpx.Response(308)                                # never reports progress (A12)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(PublishError, match="stalled at byte 0"):
        youtube.publish(cfg, client, tmp / "assets/generated/video/1.mp4", post_text(runner.eligible(conn)[0]), 1)
    assert len(puts) == youtube.MAX_STALLS + 1

    (tmp / "assets/generated/video/1.mp4").write_bytes(b"")
    with pytest.raises(PublishError, match="empty"):
        youtube.publish(cfg, client, tmp / "assets/generated/video/1.mp4", post_text(runner.eligible(conn)[0]), 1)
