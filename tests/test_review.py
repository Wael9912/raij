import json
import subprocess
from pathlib import Path

import httpx
import pytest

from src import db
from src.config import load_config
from src.discover.common import Candidate, upsert_candidates
from src.review import bot as botmod
from src.review import cards, runner, telegram

CHAT = "4242"
TOKEN = "123:SECRET-TOKEN"
BEATS = [{"role": "hook", "text": "هل سمعت", "broll_keywords": ["stadium"]},
         {"role": "body", "text": "إنفانتينو يعد بإصلاحات", "broll_keywords": ["pen"], "person": "Gianni Infantino"},
         {"role": "cta", "text": "اكتب رأيك", "broll_keywords": ["phone"]}]


class FakeTelegram:
    """Records Bot API calls; answers like Telegram does."""

    def __init__(self, updates=None, fail=None):
        self.calls, self.updates, self.fail, self.next_id = [], list(updates or []), fail, 100

    def handler(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        if self.fail and method == self.fail:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
        if request.headers.get("content-type", "").startswith("multipart"):
            body = {"multipart": True}
        else:
            body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": out})
        self.next_id += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})

    def bot(self):
        return telegram.Bot(TOKEN, httpx.Client(transport=httpx.MockTransport(self.handler)))

    def methods(self):
        return [m for m, _ in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    cfg = load_config()
    cfg.root = tmp_path
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


def _rendered(cfg, conn, status="rendered", size=1000):
    upsert_candidates(conn, [Candidate(source="rss", external_id="e1", canonical_url="https://x.example/1",
                                       title="Infantino reforms")])
    conn.execute("INSERT INTO stories (candidate_id, hook, key_facts, claims, why_trending, transcript, sources) "
                 "VALUES (1, 'FIFA may reform', '[\"211 associations\"]', '[]', 'fans care', 'src', "
                 "'[\"https://www.skynewsarabia.com/a\"]')")
    conn.execute("INSERT INTO scripts (story_id, brand_id, version, body_ar, beats, description_en, hashtags, status) "
                 "VALUES (1, 'raij', 1, 'x', ?, 'FIFA news.', '[\"#FIFA\"]', 'passed')",
                 (json.dumps(BEATS, ensure_ascii=False),))
    out = cfg.root / "assets/generated/video"
    out.mkdir(parents=True, exist_ok=True)
    (out / "1.mp4").write_bytes(b"0" * size)
    notes = {"voice": "ar-EG-ShakirNeural", "rate": "+10%", "credits": ["Photo: Jane / CC BY 4.0 via Wikimedia Commons"]}
    manifest = [{"provider": "pexels", "id": "11"}, {"provider": "wikimedia", "id": "Face.jpg"}]
    conn.execute("INSERT INTO videos (script_id, voice_path, video_path, duration_s, status, notes, broll_manifest) "
                 "VALUES (1, 'assets/generated/voice/1.wav', 'assets/generated/video/1.mp4', 48.0, ?, ?, ?)",
                 (status, json.dumps(notes), json.dumps(manifest)))
    conn.commit()


def _cb(data, chat=CHAT, cid="cb1"):
    return {"callback_query": {"id": cid, "data": data, "from": {"id": 7},
                               "message": {"message_id": 101, "chat": {"id": int(chat)}}}}


def _msg(text, chat=CHAT):
    return {"message": {"message_id": 5, "text": text, "from": {"id": 7}, "chat": {"id": int(chat)}}}


# --- cards ---------------------------------------------------------------------

def test_caption_has_hook_sources_credits_and_fits(env):
    cfg, conn, _ = env
    _rendered(cfg, conn)
    cap = cards.caption(cards.context(conn, 1))
    assert cap.startswith("🎬 #1 · 48s · script v1") and "FIFA may reform" in cap
    assert "Sources: skynewsarabia.com" in cap and "Photo: Jane / CC BY 4.0" in cap and len(cap) <= 1024


def test_keyboard_roundtrip():
    datas = [b["callback_data"] for row in cards.keyboard(9)["inline_keyboard"] for b in row]
    assert [cards.parse_callback(d) for d in datas] == [("ap", 9), ("rj", 9), ("ed", 9), ("nb", 9), ("rv", 9)]
    assert cards.parse_callback("zz:9") is None and cards.parse_callback("ap:x") is None


# --- review stage --------------------------------------------------------------

def test_review_sends_video_and_script_once(env):
    cfg, conn, _ = env
    _rendered(cfg, conn)
    tg = FakeTelegram()
    assert runner.review(cfg, conn, bot=tg.bot()) == 0
    assert tg.methods() == ["sendVideo", "sendMessage"]
    assert "إنفانتينو" in tg.calls[1][1]["text"] and tg.calls[1][1]["reply_to_message_id"] == 101
    row = conn.execute("SELECT status, review_msg_id FROM videos").fetchone()
    assert (row["status"], row["review_msg_id"]) == ("in_review", 101)
    assert runner.review(cfg, conn, bot=tg.bot()) == 0 and len(tg.calls) == 2      # idempotent


def test_review_without_telegram_config_changes_nothing(env, monkeypatch):
    cfg, conn, _ = env
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    _rendered(cfg, conn)
    assert runner.review(cfg, conn) == 1
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == "rendered"


def test_big_video_goes_out_as_preview(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setattr(runner, "MAX_VIDEO_BYTES", 500)
    _rendered(cfg, conn, size=1000)
    cmds = []

    def fake_ffmpeg(cmd):
        cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"small")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    path = runner.preview_for(cfg, tmp / "assets/generated/video/1.mp4", 48.0, run=fake_ffmpeg)
    assert path.name == "1.preview.mp4" and "-maxrate" in cmds[0]


def test_errors_never_leak_the_token(env):
    tg = FakeTelegram(fail="sendMessage")
    with pytest.raises(telegram.TelegramError) as exc:
        tg.bot().send_message(CHAT, "hi")
    assert "chat not found" in str(exc.value) and "SECRET" not in str(exc.value)


# --- bot -------------------------------------------------------------------------

def _handler(cfg, conn, tg, deps=None):
    return botmod.Handler(cfg, conn, tg.bot(), CHAT, deps)


def test_unauthorized_chat_is_ignored(env):
    cfg, conn, _ = env
    _rendered(cfg, conn, status="in_review")
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_cb("ap:1", chat="999"))
    h.handle(_msg("/pause", chat="999"))
    assert tg.calls == [] and conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    assert not db.publishing_paused(conn)


@pytest.mark.parametrize("act, decision", [("ap", "approved"), ("rj", "rejected")])
def test_approve_and_reject(env, act, decision):
    cfg, conn, _ = env
    _rendered(cfg, conn, status="in_review")
    conn.execute("UPDATE videos SET review_msg_id = 101")
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_cb(f"{act}:1"))
    appr = conn.execute("SELECT decision, decided_by, telegram_msg_id FROM approvals").fetchone()
    assert tuple(appr) == (decision, "7", 101)
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == decision
    assert tg.methods()[0] == "editMessageReplyMarkup"                  # buttons removed first
    h.handle(_cb(f"{act}:1", cid="cb2"))                                 # double tap
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1
    assert tg.calls[-1] == ("answerCallbackQuery", {"callback_query_id": "cb2", "text": "Already handled"})


def test_pause_resume_status(env):
    cfg, conn, _ = env
    _rendered(cfg, conn, status="in_review")
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_msg("/pause"))
    assert db.publishing_paused(conn)
    h.handle(_msg("/resume"))
    assert not db.publishing_paused(conn)
    h.handle(_msg("/status"))
    assert "in_review: 1" in tg.calls[-1][1]["text"]


class FakeBuild:
    """Stands in for the script/voice/assemble stages; records how regeneration called them."""

    def __init__(self, monkeypatch, cfg, passed=True):
        self.calls = {}
        self.cfg = cfg

        def write_script(cfg, story, brand, edit_note="", client=None):
            self.calls["edit_note"] = edit_note
            status = "passed" if passed else "rejected"
            v = {"version": 1, "body_ar": "جديد", "beats": BEATS, "description_en": "d", "hashtags": [],
                 "similarity": None, "status": status, "notes": {"reason": "too similar"} if not passed else {}}
            return type("O", (), {"versions": [v], "final": v})()

        def voice_script(cfg, script, out_dir, synth=None, run=None, voice_name=None, stem=None):
            self.calls["voice"] = (voice_name, stem, script["id"])
            return {"voice_path": f"assets/generated/voice/{stem}.wav", "duration_s": 47.0,
                    "notes": {"voice": voice_name or "ar-EG-ShakirNeural", "rate": "+10%"}}

        def assemble_video(cfg, video, client, run=None, recent=None, exclude=None):
            self.calls["exclude"] = exclude
            path = cfg.root / f"assets/generated/video/{video['id']}.mp4"
            path.write_bytes(b"mp4")
            return {"video_path": str(path.relative_to(cfg.root)), "subtitle_path": "x.srt", "duration_s": 49.0,
                    "manifest": [], "notes": {"clips": 3, "credits": []}}

        monkeypatch.setattr(botmod, "write_script", write_script)
        monkeypatch.setattr(botmod, "voice_script", voice_script)
        monkeypatch.setattr(botmod, "assemble_video", assemble_video)


def _in_review(cfg, conn):
    _rendered(cfg, conn, status="in_review")
    conn.execute("UPDATE videos SET review_msg_id = 101")
    conn.commit()


def test_edit_flow_regenerates_and_resends(env, monkeypatch):
    cfg, conn, _ = env
    _in_review(cfg, conn)
    fb = FakeBuild(monkeypatch, cfg)
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg, botmod.Deps(stock_client=httpx.Client()))
    h.handle(_cb("ed:1"))
    assert tg.calls[-1][0] == "sendMessage" and tg.calls[-1][1]["reply_markup"]["force_reply"] is True
    h.handle(_msg("Make the hook about the 2027 election"))
    assert fb.calls["edit_note"] == "Make the hook about the 2027 election"
    appr = conn.execute("SELECT decision, note FROM approvals").fetchone()
    assert tuple(appr) == ("edit", "Make the hook about the 2027 election")
    scripts = conn.execute("SELECT id, version, status, edit_note FROM scripts ORDER BY id").fetchall()
    assert [(s["version"], s["status"]) for s in scripts] == [(1, "superseded"), (2, "passed")]
    assert scripts[1]["edit_note"] == "Make the hook about the 2027 election"
    vids = conn.execute("SELECT id, script_id, parent_id, status FROM videos ORDER BY id").fetchall()
    assert [(v["parent_id"], v["status"]) for v in vids] == [(None, "superseded"), (1, "in_review")]
    assert vids[1]["script_id"] == scripts[1]["id"] and fb.calls["voice"][1] == f"{scripts[1]['id']}_v2"
    assert "sendVideo" in tg.methods()
    assert db.get_flag(conn, "pending_edit") == "null"


def test_new_broll_excludes_previous_stock_but_keeps_voice(env, monkeypatch):
    cfg, conn, _ = env
    _in_review(cfg, conn)
    fb = FakeBuild(monkeypatch, cfg)
    h = _handler(cfg, conn, FakeTelegram(), botmod.Deps(stock_client=httpx.Client()))
    h.handle(_cb("nb:1"))
    assert fb.calls["exclude"] == {"pexels:11"} and "voice" not in fb.calls
    new = conn.execute("SELECT voice_path, status FROM videos WHERE id = 2").fetchone()
    assert tuple(new) == ("assets/generated/voice/1.wav", "in_review")


def test_revoice_switches_to_alternate_voice(env, monkeypatch):
    cfg, conn, _ = env
    _in_review(cfg, conn)
    fb = FakeBuild(monkeypatch, cfg)
    h = _handler(cfg, conn, FakeTelegram(), botmod.Deps(stock_client=httpx.Client()))
    h.handle(_cb("rv:1"))
    assert fb.calls["voice"][0] == "ar-SA-HamedNeural"


def test_failed_regeneration_puts_original_back(env, monkeypatch):
    cfg, conn, _ = env
    _in_review(cfg, conn)
    FakeBuild(monkeypatch, cfg, passed=False)
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg, botmod.Deps(stock_client=httpx.Client()))
    h.handle(_cb("ed:1"))
    h.handle(_msg("rewrite it"))
    assert conn.execute("SELECT status FROM videos WHERE id = 1").fetchone()[0] == "in_review"
    texts = [b.get("text", "") for m, b in tg.calls if m == "sendMessage"]
    assert any("Couldn't regenerate #1" in t and "too similar" in t for t in texts)
    restored = [b for m, b in tg.calls if m == "editMessageReplyMarkup"][-1]
    assert restored["reply_markup"] == cards.keyboard(1)


def test_poll_persists_offset(env):
    cfg, conn, _ = env
    _rendered(cfg, conn, status="in_review")
    tg = FakeTelegram(updates=[{"update_id": 50, **_msg("/pause")}])
    assert botmod.poll(cfg, conn, tg.bot(), CHAT, once=True) == 1
    assert db.get_flag(conn, "telegram_offset") == "51" and db.publishing_paused(conn)
    assert botmod.poll(cfg, conn, tg.bot(), CHAT, once=True) == 0
    assert tg.calls[-1] == ("getUpdates", {"offset": 51, "timeout": 0, "allowed_updates": ["message", "callback_query"]})


def test_dry_run(env, caplog):
    cfg, conn, _ = env
    _rendered(cfg, conn)
    caplog.set_level("INFO")
    assert runner.review(cfg, conn, dry_run=True) == 0
    assert "1 rendered video" in caplog.text and conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
