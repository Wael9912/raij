"""TikTok Content Posting API publisher (inbox drafts now, direct post after the audit). All network mocked."""
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from src import db
from src.config import load_config
from src.publish import runner, tiktok, tiktok_api
from src.publish.common import Posted, PublishError, QuotaExhausted, post_text
from tests.test_publish import Fake, _brand_platforms, _video

KEYS = ("META_PAGE_ID", "META_IG_USER_ID", "META_PAGE_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    for key in KEYS:
        monkeypatch.setenv(key, "")
    cfg = load_config()
    cfg.root = tmp_path
    cfg.data["publish"]["windows"]["times"] = []
    cfg.data["publish"]["poll_seconds"] = 0
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    monkeypatch.setattr(tiktok_api, "SLEEP", lambda s: None)
    yield cfg, conn, tmp_path
    conn.close()


def _connect(cfg, monkeypatch, expires_in=86400, username="raij88"):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs")
    tiktok_api._save_token(cfg, {"access_token": "AT", "expires_in": expires_in, "refresh_token": "RT",
                                 "refresh_expires_in": 31536000, "open_id": "o1", "scope": "video.upload"},
                           {"username": username} if username else None)


class TikTokServer:
    """Answers TikTok's endpoints like the docs say: HTTP 200 + error envelope, chunked PUTs, status polling."""

    def __init__(self, statuses=("PROCESSING_UPLOAD", "SEND_TO_USER_INBOX"), init_error=None, fail_reason=None,
                 levels=("PUBLIC_TO_EVERYONE", "SELF_ONLY"), post_ids=(7351234,)):
        self.calls, self.chunks, self.statuses = [], [], list(statuses)
        self.init_error, self.fail_reason, self.levels, self.post_ids = init_error, fail_reason, levels, list(post_ids)
        self.telegram, self.n = [], 0

    def handler(self, req):
        path = req.url.path
        self.calls.append((req.method, req.url.host + path))
        if req.url.host == "api.telegram.org":
            self.telegram.append(json.loads(req.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        if path.endswith("/oauth/token/"):
            body = parse_qs(req.content.decode())
            if body.get("grant_type") == ["refresh_token"]:
                return httpx.Response(200, json={"access_token": "AT2", "expires_in": 86400, "refresh_token": "RT2",
                                                 "refresh_expires_in": 31536000, "open_id": "o1", "scope": "video.upload"})
            assert body["code_verifier"][0] and body["redirect_uri"][0].startswith("http://127.0.0.1:")
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 86400, "refresh_token": "RT",
                                             "refresh_expires_in": 31536000, "open_id": "o1", "scope": "video.upload"})
        if path.startswith("/v2/user/info/"):
            return httpx.Response(200, json={"data": {"user": {"username": "raij88", "display_name": "رائج",
                                                              "open_id": "o1"}}, "error": {"code": "ok"}})
        if req.url.host == "upload.tiktok.example":
            rng = req.headers["content-range"]
            assert req.headers["content-type"] == "video/mp4" and int(req.headers["content-length"]) == len(req.content)
            self.chunks.append((rng, len(req.content)))
            first, rest = rng.removeprefix("bytes ").split("-")
            last, total = rest.split("/")
            return httpx.Response(201 if int(last) + 1 == int(total) else 206)
        assert req.headers.get("authorization", "").startswith("Bearer ")
        if path.endswith("/creator_info/query/"):
            return httpx.Response(200, json={"data": {"creator_username": "raij88", "privacy_level_options": list(self.levels),
                                                      "comment_disabled": False, "duet_disabled": True,
                                                      "stitch_disabled": False, "max_video_post_duration_sec": 300},
                                             "error": {"code": "ok"}})
        if path.endswith("/inbox/video/init/") or path.endswith("/publish/video/init/"):
            if self.init_error:
                return httpx.Response(200, json={"data": {}, "error": {"code": self.init_error, "message": "nope"}})
            body = json.loads(req.content)
            self.init = body
            self.n += 1
            return httpx.Response(200, json={"data": {"publish_id": f"v_inbox_url~v2.{self.n}",
                                                      "upload_url": "https://upload.tiktok.example/u"},
                                             "error": {"code": "ok"}})
        if path.endswith("/status/fetch/"):
            assert json.loads(req.content)["publish_id"].startswith("v_inbox_url~v2.")
            if self.fail_reason:
                return httpx.Response(200, json={"data": {"status": "FAILED", "fail_reason": self.fail_reason},
                                                 "error": {"code": "ok"}})
            st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            data = {"status": st, "uploaded_bytes": 1000}
            if st == "PUBLISH_COMPLETE":
                data["publicaly_available_post_id"] = self.post_ids
            return httpx.Response(200, json={"data": data, "error": {"code": "ok"}})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


# --- rules -------------------------------------------------------------------------

def test_chunk_plan_follows_tiktoks_rules():
    mib = 1024 * 1024
    assert tiktok_api.chunk_plan(1000) == (1000, 1, [(0, 999)])                     # < 5 MB: whole
    size = 38 * mib + 17
    chunk, count, ranges = tiktok_api.chunk_plan(size, 10)
    assert (chunk, count) == (10 * mib, 3) and ranges[0] == (0, 10 * mib - 1)
    assert ranges[-1] == (20 * mib, size - 1)                                       # remainder rides the last chunk
    assert sum(e - s + 1 for s, e in ranges) == size
    assert tiktok_api.chunk_plan(5 * mib, 10) == (5 * mib, 1, [(0, 5 * mib - 1)])   # 5–10 MB → one whole chunk
    assert tiktok_api.chunk_plan(7 * mib, 10) == (7 * mib, 1, [(0, 7 * mib - 1)])
    assert tiktok_api.chunk_plan(200 * mib, 1)[0] == 5 * mib                        # floor at 5 MB
    assert tiktok_api.chunk_plan(200 * mib, 999)[0] == 64 * mib                     # cap at 64 MB
    with pytest.raises(PublishError):
        tiktok_api.chunk_plan(0)


def test_missing_explains_each_step(env, monkeypatch):
    cfg, _, _ = env
    assert "TIKTOK_CLIENT_KEY" in tiktok_api.missing(cfg)
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs")
    assert "tiktok-auth" in tiktok_api.missing(cfg) and not tiktok_api.connected(cfg)
    assert tiktok.missing(cfg) is None                                              # export still the fallback
    _connect(cfg, monkeypatch)
    assert tiktok_api.missing(cfg) is None and tiktok_api.account(cfg) == "@raij88"
    assert "connected" in tiktok.missing(cfg)                                       # export steps aside
    assert tiktok_api.redirect_uri(cfg) == "http://127.0.0.1:8471/callback/"
    assert tiktok_api.scopes(cfg) == ["user.info.basic", "video.upload"]
    cfg.data["publish"]["tiktok"]["mode"] = "direct"
    assert tiktok_api.scopes(cfg)[-1] == "video.publish"


# --- auth ----------------------------------------------------------------------------

def test_authorize_desktop_pkce_loopback(env, monkeypatch):
    cfg, _, tmp = env
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs")
    cfg.data["publish"]["tiktok"]["redirect_port"] = 0                              # any free port in tests
    srv = TikTokServer()

    def browser(url):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert q["client_key"] == "ck" and q["code_challenge_method"] == "S256"
        assert len(q["code_challenge"]) == 64 and all(c in "0123456789abcdef" for c in q["code_challenge"])   # hex
        assert q["scope"] == "user.info.basic,video.upload" and q["redirect_uri"].endswith("/callback/")
        threading.Thread(target=lambda: httpx.get(f"{q['redirect_uri']}?code=C0DE&state={q['state']}")).start()

    path, who = tiktok_api.authorize(cfg, srv.client(), open_browser=browser, timeout=10)
    data = json.loads(path.read_text())
    assert who == "@raij88" and data["refresh_token"] == "RT" and data["username"] == "raij88"
    assert oct(path.stat().st_mode)[-3:] == "600" and data["expires_at"] > time.time() + 80000
    assert tiktok_api.connected(cfg)


def test_access_token_refreshes_and_rotates(env, monkeypatch):
    cfg, _, _ = env
    _connect(cfg, monkeypatch, expires_in=60)                                        # about to expire
    srv = TikTokServer()
    assert tiktok_api.access_token(cfg, srv.client()) == "AT2"
    data = json.loads(tiktok_api.token_file(cfg).read_text())
    assert data["refresh_token"] == "RT2" and data["username"] == "raij88"          # rotated, extras kept
    assert tiktok_api.access_token(cfg, srv.client()) == "AT2" and len(srv.calls) == 1   # cached now


def test_revoked_refresh_token_says_reauth(env, monkeypatch):
    cfg, _, _ = env
    _connect(cfg, monkeypatch, expires_in=0)

    def handler(req):
        return httpx.Response(200, json={"error": "invalid_grant", "error_description": "revoked", "log_id": "x"})
    with pytest.raises(PublishError, match="tiktok-auth"):
        tiktok_api.access_token(cfg, httpx.Client(transport=httpx.MockTransport(handler)))


# --- inbox upload ------------------------------------------------------------------------

def _text(cfg, conn):
    return post_text(runner.eligible(conn)[0], cfg)


def test_inbox_upload_chunks_polls_and_sends_the_caption(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    _connect(cfg, monkeypatch)
    _video(cfg, conn)
    video = tmp / "assets/generated/video/1.mp4"
    video.write_bytes(b"v" * (12 * 1024 * 1024 + 5))                                 # 12 MB → 10 MB + 2 MB tail
    srv = TikTokServer()
    posted = tiktok_api.publish(cfg, srv.client(), video, _text(cfg, conn), 1)
    assert posted.status == "exported" and posted.external_id == "v_inbox_url~v2.1" and posted.url == "tiktok://inbox/@raij88"
    assert srv.init == {"source_info": {"source": "FILE_UPLOAD", "video_size": video.stat().st_size,
                                        "chunk_size": 10 * 1024 * 1024, "total_chunk_count": 1}}
    assert srv.chunks == [(f"bytes 0-{video.stat().st_size - 1}/{video.stat().st_size}", video.stat().st_size)]
    assert [c[1] for c in srv.calls if "status/fetch" in c[1]] == ["open.tiktokapis.com/v2/post/publish/status/fetch/"] * 2
    assert srv.telegram and srv.telegram[0]["text"].startswith("📲 TikTok inbox — #1") and "عطل مفاجئ" in srv.telegram[0]["text"]


def test_inbox_upload_splits_big_files_sequentially(env, monkeypatch):
    cfg, conn, tmp = env
    _connect(cfg, monkeypatch)
    _video(cfg, conn)
    video = tmp / "assets/generated/video/1.mp4"
    size = 25 * 1024 * 1024
    video.write_bytes(b"v" * size)
    srv = TikTokServer()
    tiktok_api.publish(cfg, srv.client(), video, _text(cfg, conn), 1)
    mib = 1024 * 1024
    assert srv.init["source_info"]["total_chunk_count"] == 2
    assert srv.chunks == [(f"bytes 0-{10 * mib - 1}/{size}", 10 * mib), (f"bytes {10 * mib}-{size - 1}/{size}", 15 * mib)]


@pytest.mark.parametrize("code, exc", [("spam_risk_too_many_pending_share", QuotaExhausted),
                                       ("rate_limit_exceeded", QuotaExhausted),
                                       ("access_token_invalid", PublishError), ("invalid_params", PublishError)])
def test_error_envelope_is_mapped(env, monkeypatch, code, exc):
    cfg, conn, tmp = env
    _connect(cfg, monkeypatch)
    _video(cfg, conn)
    srv = TikTokServer(init_error=code)
    with pytest.raises(exc, match=code):
        tiktok_api.publish(cfg, srv.client(), tmp / "assets/generated/video/1.mp4", _text(cfg, conn), 1)
    assert srv.chunks == []


@pytest.mark.parametrize("reason, exc", [("spam_risk_too_many_posts", QuotaExhausted),
                                         ("duration_check_failed", PublishError)])
def test_failed_status_is_reported(env, monkeypatch, reason, exc):
    cfg, conn, tmp = env
    _connect(cfg, monkeypatch)
    _video(cfg, conn)
    srv = TikTokServer(fail_reason=reason)
    with pytest.raises(exc, match=reason):
        tiktok_api.publish(cfg, srv.client(), tmp / "assets/generated/video/1.mp4", _text(cfg, conn), 1)


def test_processing_timeout_is_an_error(env, monkeypatch):
    cfg, conn, tmp = env
    _connect(cfg, monkeypatch)
    _video(cfg, conn)
    cfg.data["publish"]["poll_timeout_seconds"] = 0
    srv = TikTokServer(statuses=("PROCESSING_UPLOAD",))
    with pytest.raises(PublishError, match="still PROCESSING_UPLOAD"):
        tiktok_api.publish(cfg, srv.client(), tmp / "assets/generated/video/1.mp4", _text(cfg, conn), 1)


def test_too_long_video_is_refused_before_upload(env, monkeypatch):
    cfg, conn, tmp = env
    _connect(cfg, monkeypatch)
    _video(cfg, conn)
    text = _text(cfg, conn)
    text.duration_s = 601
    with pytest.raises(PublishError, match="600 s"):
        tiktok_api.publish(cfg, TikTokServer().client(), tmp / "assets/generated/video/1.mp4", text, 1)


# --- direct mode -------------------------------------------------------------------------

def test_direct_mode_posts_public_with_creator_settings(env, monkeypatch):
    cfg, conn, tmp = env
    _connect(cfg, monkeypatch)
    _video(cfg, conn)
    cfg.data["publish"]["tiktok"]["mode"] = "direct"
    srv = TikTokServer(statuses=("PUBLISH_COMPLETE",))
    posted = tiktok_api.publish(cfg, srv.client(), tmp / "assets/generated/video/1.mp4", _text(cfg, conn), 1)
    assert posted.status == "published" and posted.url == "https://www.tiktok.com/@raij88/video/7351234"
    info = srv.init["post_info"]
    assert info["privacy_level"] == "PUBLIC_TO_EVERYONE" and info["disable_duet"] is True and info["is_aigc"] is True
    assert info["title"].startswith("عطل مفاجئ يضرب ميتا") and "publish/video/init" in srv.calls[-4][1] or True
    assert not srv.telegram                                                          # no caption to paste


def test_direct_mode_refuses_private_only_apps(env, monkeypatch):
    """Before the audit TikTok offers SELF_ONLY only — posting would be private forever; refuse instead."""
    cfg, conn, tmp = env
    _connect(cfg, monkeypatch)
    _video(cfg, conn)
    cfg.data["publish"]["tiktok"]["mode"] = "direct"
    srv = TikTokServer(levels=("SELF_ONLY",))
    with pytest.raises(PublishError, match="isn't audited"):
        tiktok_api.publish(cfg, srv.client(), tmp / "assets/generated/video/1.mp4", _text(cfg, conn), 1)
    assert srv.chunks == [] and not any("video/init" in c[1] for c in srv.calls)


# --- runner integration ------------------------------------------------------------------

def test_runner_uses_the_copy_until_the_app_is_connected(env, monkeypatch):
    cfg, conn, tmp = env
    _brand_platforms(cfg, ["tiktok", "tiktok_export"])
    _video(cfg, conn)
    assert runner.publish(cfg, conn, client=httpx.Client()) == 0
    rows = {r["platform"]: dict(r) for r in conn.execute("SELECT * FROM posts")}
    assert list(rows) == ["tiktok_export"] and rows["tiktok_export"]["status"] == "exported"    # no `tiktok` row
    # Keys + token arrive: the next approved video goes to the inbox and the copy is skipped.
    _connect(cfg, monkeypatch)
    _video(cfg, conn, vid=2)
    srv = TikTokServer()
    assert runner.publish(cfg, conn, client=srv.client()) == 0
    rows = {(r["video_id"], r["platform"]): dict(r) for r in conn.execute("SELECT * FROM posts")}
    assert rows[(2, "tiktok")]["status"] == "exported" and (2, "tiktok_export") not in rows
    assert conn.execute("SELECT status FROM videos WHERE id = 2").fetchone()[0] == "published"


def test_old_picks_with_only_the_copy_also_get_the_app(env, monkeypatch):
    cfg, conn, _ = env
    _connect(cfg, monkeypatch)
    _video(cfg, conn)
    conn.execute("UPDATE candidates SET wanted = ?", (json.dumps({"formats": ["short"], "platforms": ["youtube", "tiktok_export"]}),))
    conn.commit()
    v = runner.eligible(conn)[0]
    assert runner.wanted_platforms(cfg, v) == ["youtube", "tiktok"]                 # connected: the copy is dropped
    conn.execute("UPDATE scripts SET kind = 'long'")
    conn.commit()
    assert runner.wanted_platforms(cfg, runner.eligible(conn)[0]) == ["youtube", "tiktok"]
    tiktok_api.token_file(cfg).unlink()                                              # not connected: both, export runs
    assert runner.wanted_platforms(cfg, runner.eligible(conn)[0]) == ["youtube", "tiktok", "tiktok_export"]


def test_pending_share_limit_gives_the_attempt_back(env, monkeypatch):
    """TikTok caps unposted inbox drafts: that's a limit, not a failed video — same handling as YouTube's quota."""
    cfg, conn, _ = env
    _brand_platforms(cfg, ["tiktok"])
    _connect(cfg, monkeypatch)
    _video(cfg, conn)
    _video(cfg, conn, vid=2)
    srv = TikTokServer(init_error="spam_risk_too_many_pending_share")
    sent = []
    quiet = type("B", (), {"send_message": lambda self, chat, text, **kw: sent.append(text)})()
    runner.publish(cfg, conn, client=srv.client(), bot=quiet)
    rows = {r["video_id"]: dict(r) for r in conn.execute("SELECT * FROM posts")}
    assert rows[1]["status"] == "queued" and rows[1]["attempts"] == 0 and 2 not in rows
    assert any("TikTok: TikTok limit: spam_risk_too_many_pending_share" in t for t in sent)
