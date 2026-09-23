import json
import subprocess
from pathlib import Path

import httpx
import pytest

from src import db
from src.assemble.render import GuardrailError
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


def _cb(data, chat=CHAT, cid="cb1", sender=None):
    # A private chat: the owner's user id is the chat id (S4 — the bot checks the sender, not just the chat).
    return {"callback_query": {"id": cid, "data": data, "from": {"id": int(sender or chat)},
                               "message": {"message_id": 101, "chat": {"id": int(chat)}}}}


def _msg(text, chat=CHAT, sender=None, reply_to=None):
    msg = {"message_id": 5, "text": text, "from": {"id": int(sender or chat)}, "chat": {"id": int(chat)}}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"message": msg}


def _prompt_id(tg):
    """The message id of the last ✏️ ForceReply prompt the fake Telegram handed out."""
    return tg.next_id


# --- cards ---------------------------------------------------------------------

def test_caption_has_hook_sources_credits_and_fits(env):
    cfg, conn, _ = env
    _rendered(cfg, conn)
    conn.execute("UPDATE candidates SET rank_reason = 'big fan reaction', category = 'sports'")
    conn.commit()
    ctx = cards.context(conn, 1)
    cap = cards.caption(ctx)
    assert cap.startswith("🎬 #1 · 48s\n\nهل سمعت\n\n🔎 Why: trending on news — big fan reaction")
    assert "script v1" not in cap and "similarity" not in cap            # jargon moved to the script message (U8)
    assert "Sources: skynewsarabia.com" in cap and "Photo: Jane / CC BY 4.0" in cap and len(cap) <= 1024
    assert cards.script_text(ctx).endswith("🔧 script v1 · similarity n/a (non-Arabic source) · voice ar-EG-ShakirNeural")


def test_caption_uses_the_arabic_hook_title_and_series(env):
    """U3: the caption shows the title burned into the video (also the YouTube title), not the English hook."""
    cfg, conn, _ = env
    _rendered(cfg, conn)
    conn.execute("UPDATE scripts SET notes = ?", (json.dumps({"hook_title": "إصلاحات الفيفا", "series": "رياضة في دقيقة"},
                                                             ensure_ascii=False),))
    conn.commit()
    cap = cards.caption(cards.context(conn, 1))
    assert cap.startswith("🎬 #1 · 48s · رياضة في دقيقة\n\nإصلاحات الفيفا\n") and "FIFA may reform" not in cap


def test_regenerated_card_names_its_parent(env):
    """U4: a replacement card says which card it replaces and why."""
    cfg, conn, _ = env
    _rendered(cfg, conn, status="superseded")
    conn.execute("INSERT INTO approvals (video_id, decision, note) VALUES (1, 'edit', 'shorter hook')")
    conn.execute("INSERT INTO videos (script_id, video_path, duration_s, status, parent_id) "
                 "VALUES (1, 'assets/generated/video/1.mp4', 47, 'rendered', 1)")
    conn.commit()
    cap = cards.caption(cards.context(conn, 2))
    assert "↩️ Replaces #1 (script edited)\n✏️ shorter hook" in cap


def test_keyboard_roundtrip():
    datas = [b["callback_data"] for row in cards.keyboard(9)["inline_keyboard"] for b in row]
    assert [cards.parse_callback(d) for d in datas] == [("ap", 9), ("rj", 9), ("ed", 9), ("nb", 9), ("rv", 9)]
    assert cards.parse_callback("zz:9") is None and cards.parse_callback("ap:x") is None
    assert cards.parse_callback("rt:9") == ("rt", 9) and cards.parse_callback("ba:9") == ("ba", 9)


# --- review stage --------------------------------------------------------------

def test_review_sends_video_and_script_once(env):
    cfg, conn, _ = env
    _rendered(cfg, conn)
    tg = FakeTelegram()
    assert runner.review(cfg, conn, bot=tg.bot()) == 0
    assert tg.methods() == ["sendMessage", "sendVideo", "sendMessage"]          # digest, card, script
    assert tg.calls[0][1]["text"].startswith("📬 1 new video to review\n#1\nهل سمعت")
    assert "إنفانتينو" in tg.calls[2][1]["text"] and tg.calls[2][1]["reply_to_message_id"] == 102
    row = conn.execute("SELECT status, review_msg_id FROM videos").fetchone()
    assert (row["status"], row["review_msg_id"]) == ("in_review", 102)
    assert runner.review(cfg, conn, bot=tg.bot()) == 0 and len(tg.calls) == 3      # idempotent, no digest either


def test_digest_lists_flagged_and_stage_trouble(env):
    cfg, conn, _ = env
    _rendered(cfg, conn)
    upsert_candidates(conn, [Candidate(source="rss", external_id="e2", canonical_url="https://x.example/2",
                                       title="Election riots")])
    conn.execute("UPDATE candidates SET status = 'flagged' WHERE id = 2")
    db.finish_run(conn, db.start_run(conn, "extract"), "partial", {})
    db.finish_run(conn, db.start_run(conn, "voice"), "ok", {})
    text = cards.digest_text(conn, [1])
    assert "🚩 1 flagged (political, not scheduled)\n• Election riots" in text
    assert "⚠️ 1 stage run with problems\n• extract partial" in text and "voice" not in text


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


def test_unauthorized_sender_in_the_right_chat_is_ignored(env):
    """S4: the chat alone isn't authorization — a tap or text from another user id does nothing."""
    cfg, conn, _ = env
    _rendered(cfg, conn, status="in_review")
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_cb("ap:1", sender=999))
    h.handle(_msg("/pause", sender=999))
    assert tg.calls == [] and conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM videos WHERE id = 1").fetchone()[0] == "in_review"
    assert not db.publishing_paused(conn)


def test_owner_id_override(env, monkeypatch):
    cfg, conn, _ = env
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "555")
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_msg("/pause"))                       # sender == chat id, but the owner is someone else now
    assert not db.publishing_paused(conn)
    h.handle(_msg("/pause", sender=555))
    assert db.publishing_paused(conn)


def test_edit_note_must_reply_to_the_prompt(env, monkeypatch):
    """S4: a stray text while an ✏️ prompt is open nudges instead of rewriting; the reply itself still works."""
    cfg, conn, _ = env
    _in_review(cfg, conn)
    fb = FakeBuild(monkeypatch, cfg)
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg, botmod.Deps(stock_client=httpx.Client()))
    h.handle(_cb("ed:1"))
    prompt = _prompt_id(tg)
    assert "editMessageReplyMarkup" not in tg.methods()            # the card keeps its buttons while asking (U2)
    h.handle(_msg("just chatting"))
    assert "edit_note" not in fb.calls and "1" in json.loads(db.get_flag(conn, "pending_edits"))
    assert "To edit #1, reply to its ✏️ prompt" in tg.calls[-1][1]["text"]
    h.handle(_msg("the real note", reply_to=prompt))
    assert fb.calls["edit_note"] == "the real note" and db.get_flag(conn, "pending_edits") == "{}"


def test_two_edit_prompts_are_kept_apart(env, monkeypatch):
    """U2: ✏️ on two cards → two open prompts; each reply rewrites its own video, the other stays open."""
    cfg, conn, _ = env
    _in_review(cfg, conn)
    conn.execute("INSERT INTO videos (script_id, video_path, duration_s, status, review_msg_id) "
                 "VALUES (1, 'assets/generated/video/1.mp4', 47, 'in_review', 151)")
    conn.commit()
    fb = FakeBuild(monkeypatch, cfg)
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg, botmod.Deps(stock_client=httpx.Client()))
    h.handle(_cb("ed:1"))
    prompt1 = _prompt_id(tg)
    h.handle(_cb("ed:2"))
    prompt2 = _prompt_id(tg)
    assert set(json.loads(db.get_flag(conn, "pending_edits"))) == {"1", "2"}
    h.handle(_msg("note for two", reply_to=prompt2))
    assert fb.calls["edit_note"] == "note for two"
    assert conn.execute("SELECT status FROM videos WHERE id = 2").fetchone()[0] == "superseded"
    assert conn.execute("SELECT status FROM videos WHERE id = 1").fetchone()[0] == "in_review"
    assert list(json.loads(db.get_flag(conn, "pending_edits"))) == ["1"]
    h.handle(_cb("ap:1"))                                          # approving cancels its open prompt
    assert db.get_flag(conn, "pending_edits") == "{}"
    h.handle(_msg("late note", reply_to=prompt1))
    assert conn.execute("SELECT count(*) FROM approvals WHERE decision = 'edit'").fetchone()[0] == 1


def test_edit_prompt_expires(env, monkeypatch):
    cfg, conn, _ = env
    _in_review(cfg, conn)
    fb = FakeBuild(monkeypatch, cfg)
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg, botmod.Deps(stock_client=httpx.Client()))
    h.handle(_cb("ed:1"))
    prompt = _prompt_id(tg)
    pending = json.loads(db.get_flag(conn, "pending_edits"))
    pending["1"]["at"] -= botmod.EDIT_NOTE_TTL + 1
    db.set_flag(conn, "pending_edits", json.dumps(pending))
    h.handle(_msg("too late", reply_to=prompt))
    assert "edit_note" not in fb.calls and db.get_flag(conn, "pending_edits") == "{}"
    assert "expired" in tg.calls[-1][1]["text"]
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


def test_legacy_single_pending_edit_flag_is_honoured(env, monkeypatch):
    """A prompt opened by the pre-11 bot (control.pending_edit) still takes its reply after the upgrade."""
    cfg, conn, _ = env
    _in_review(cfg, conn)
    fb = FakeBuild(monkeypatch, cfg)
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg, botmod.Deps(stock_client=httpx.Client()))
    import time
    db.set_flag(conn, "pending_edit", json.dumps({"video_id": 1, "prompt": 77, "user": CHAT, "at": time.time()}))
    h.handle(_msg("old-style note", reply_to=77))
    assert fb.calls["edit_note"] == "old-style note" and db.get_flag(conn, "pending_edit") == "null"


def test_callback_rejects_non_ascii_digits():
    assert cards.parse_callback("ap:1") == ("ap", 1)
    assert cards.parse_callback("ap:١") is None            # Arabic-Indic digit passes str.isdigit()
    assert cards.parse_callback("ap:") is None and cards.parse_callback("zz:1") is None


def test_send_card_refuses_video_outside_generated_assets(env):
    """S7: the review upload goes through the same guardrail as publish."""
    cfg, conn, tmp = env
    _rendered(cfg, conn)
    (tmp / "data").mkdir(exist_ok=True)
    (tmp / "data" / "source.mp4").write_bytes(b"0")
    conn.execute("UPDATE videos SET video_path = 'data/source.mp4' WHERE id = 1")
    conn.commit()
    tg = FakeTelegram()
    with pytest.raises(GuardrailError):
        runner.send_card(cfg, conn, tg.bot(), CHAT, 1, run=lambda cmd: subprocess.CompletedProcess(cmd, 0))
    assert "sendVideo" not in tg.methods()
    assert conn.execute("SELECT status FROM videos WHERE id = 1").fetchone()[0] == "rendered"


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
    assert tuple(appr) == (decision, CHAT, 101)
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == decision
    assert tg.methods()[:2] == ["answerCallbackQuery", "editMessageReplyMarkup"]   # ack, then buttons off
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
    assert "1 in review" in tg.calls[-1][1]["text"] and "▶️ on" in tg.calls[-1][1]["text"]


def test_queue_lists_review_and_publishing_state(env):
    cfg, conn, _ = env
    _rendered(cfg, conn, status="in_review")
    conn.execute("INSERT INTO videos (script_id, video_path, duration_s, status) "
                 "VALUES (1, 'assets/generated/video/1.mp4', 47, 'approved')")
    conn.execute("INSERT INTO approvals (video_id, decision, decided_at) VALUES (2, 'approved', '2026-09-20 10:00:00')")
    conn.execute("INSERT INTO posts (video_id, approval_id, platform, status, url) VALUES (2, 1, 'youtube', 'published', 'u')")
    conn.execute("INSERT INTO posts (video_id, approval_id, platform, status, attempts) VALUES (2, 1, 'facebook', 'failed', 2)")
    conn.commit()
    cfg.brands[0]["platforms"] = ["youtube", "instagram", "facebook", "tiktok_export"]
    from datetime import datetime, timezone
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    text = cards.queue_text(conn, cfg, {"instagram": "no keys"}, now=now)
    assert text.startswith("📥 In review: 1\n#1 · ")
    assert "📤 Approved, publishing: 1\n#2 · approved 26 h ago\nهل سمعت\n" in text
    assert "YouTube ✅ · Instagram 🔑 no keys · Facebook ⚠️ 2× · TikTok ⏳" in text
    conn.execute("UPDATE videos SET status = 'published'")
    conn.commit()
    assert cards.queue_text(conn, cfg).startswith("📭 Queue is empty")


def test_help_and_unknown_command(env):
    cfg, conn, _ = env
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_msg("/help"))
    assert tg.calls[-1][1]["text"].startswith("🤖 Ra'ij review bot")
    h.handle(_msg("/frobnicate"))
    assert "Unknown command /frobnicate" in tg.calls[-1][1]["text"]


def test_report_command_sends_no_backup(env, monkeypatch):
    """U7: /report is the numbers only; the encrypted DB copy stays weekly."""
    cfg, conn, _ = env
    monkeypatch.setenv("RAIJ_STATE_KEY", "k")
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_msg("/report"))
    assert tg.methods() == ["sendMessage"] and "weekly report" in tg.calls[-1][1]["text"]


def test_slash_menu_registered_once_per_version(env):
    cfg, conn, _ = env
    tg = FakeTelegram()
    assert botmod.ensure_commands(conn, tg.bot()) is True
    assert tg.methods() == ["setMyCommands"]
    assert [c["command"] for c in tg.calls[0][1]["commands"]][:2] == ["queue", "status"]
    assert botmod.ensure_commands(conn, tg.bot()) is False and len(tg.calls) == 1


@pytest.mark.parametrize("cmd, act, decision", [("/approve_all", "ba", "approved"), ("/skip", "bs", "rejected")])
def test_bulk_commands_need_a_confirm_tap(env, cmd, act, decision):
    cfg, conn, _ = env
    _in_review(cfg, conn)
    conn.execute("INSERT INTO videos (script_id, video_path, duration_s, status, review_msg_id) "
                 "VALUES (1, 'assets/generated/video/1.mp4', 47, 'in_review', 151)")
    conn.commit()
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_msg(cmd))
    ask = tg.calls[-1][1]
    assert ask["text"].startswith(("Approve" if act == "ba" else "Reject") + " all 2 cards in review?\n#1\n")
    assert ask["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"{act}:2"
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0        # nothing until confirmed
    h.handle(_cb("bx:2"))
    assert "Cancelled" in tg.calls[-1][1]["text"] and conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    conn.execute("INSERT INTO videos (script_id, video_path, duration_s, status, review_msg_id) "
                 "VALUES (1, 'assets/generated/video/1.mp4', 47, 'in_review', 161)")   # arrives after the ask
    conn.commit()
    h.handle(_cb(f"{act}:2"))
    rows = conn.execute("SELECT id, status FROM videos ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, decision), (2, decision), (3, "in_review")]
    assert conn.execute("SELECT count(*) FROM approvals WHERE decision = ?", (decision,)).fetchone()[0] == 2
    assert f"2 cards: #1, #2" in tg.calls[-1][1]["text"]
    h.handle(_msg(cmd))
    h.handle(_cb(f"{act}:3"))
    assert conn.execute("SELECT status FROM videos WHERE id = 3").fetchone()[0] == decision


def test_retry_button_requeues_failed_platforms(env):
    """U9: 🔁 on a final failure re-approves the video and resets its failed posts."""
    cfg, conn, _ = env
    _rendered(cfg, conn, status="approved")
    conn.execute("INSERT INTO approvals (video_id, decision, decided_at) VALUES (1, 'approved', '2026-09-01 00:00:00')")
    conn.execute("INSERT INTO posts (video_id, approval_id, platform, status, url) VALUES (1, 1, 'youtube', 'published', 'u')")
    conn.execute("INSERT INTO posts (video_id, approval_id, platform, status, attempts, error, last_attempt_at) "
                 "VALUES (1, 1, 'facebook', 'failed', 3, 'boom', '2026-09-02 00:00:00')")
    conn.commit()
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_cb("rt:1"))
    fb = dict(conn.execute("SELECT * FROM posts WHERE platform = 'facebook'").fetchone())
    assert (fb["status"], fb["attempts"], fb["error"], fb["last_attempt_at"]) == ("queued", 0, None, None)
    assert conn.execute("SELECT status FROM posts WHERE platform = 'youtube'").fetchone()[0] == "published"
    latest = conn.execute("SELECT decision, note FROM approvals ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(latest) == ("approved", "retry")
    assert "queued again for Facebook" in tg.calls[-1][1]["text"]
    h.handle(_cb("rt:1", cid="cb2"))
    assert "nothing to retry" in tg.calls[-1][1]["text"]


def test_reminders_warn_at_48_and_72_hours_without_deciding(env):
    """U5 (owner's call: warn only). One nudge per level, caption gets a ⌛ line, buttons stay, status unchanged."""
    from datetime import datetime, timedelta, timezone
    cfg, conn, _ = env
    _in_review(cfg, conn)
    conn.execute("UPDATE videos SET created_at = '2026-09-20 00:00:00'")
    conn.commit()
    tg = FakeTelegram()
    bot = tg.bot()
    at = lambda h: datetime(2026, 9, 20, tzinfo=timezone.utc) + timedelta(hours=h)      # noqa: E731
    assert runner.remind(cfg, conn, bot, CHAT, now=at(47)) == []
    assert runner.remind(cfg, conn, bot, CHAT, now=at(49)) == [1]
    assert tg.methods() == ["sendMessage", "editMessageCaption"]
    assert tg.calls[0][1]["text"].startswith("⌛ 1 card in review for over 48 h — decide soon")
    assert tg.calls[1][1]["caption"].startswith("⌛ In review for 2 d 1 h\n\n🎬 #1")
    assert tg.calls[1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ap:1"
    assert runner.remind(cfg, conn, bot, CHAT, now=at(60)) == [] and len(tg.calls) == 2      # not again at 48
    assert runner.remind(cfg, conn, bot, CHAT, now=datetime(2026, 9, 23, 1, 0, tzinfo=timezone.utc)) == [1]
    assert "over 72 h — the trend is going stale" in tg.calls[2][1]["text"]
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == "in_review"
    assert json.loads(conn.execute("SELECT notes FROM videos").fetchone()[0])["reminded_h"] == 72


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
    h.handle(_msg("Make the hook about the 2027 election", reply_to=_prompt_id(tg)))
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
    h.handle(_msg("rewrite it", reply_to=_prompt_id(tg)))
    assert conn.execute("SELECT status FROM videos WHERE id = 1").fetchone()[0] == "in_review"
    texts = [b.get("text", "") for m, b in tg.calls if m == "sendMessage"]
    assert any("Couldn't regenerate #1" in t and "too similar" in t for t in texts)
    restored = [b for m, b in tg.calls if m == "editMessageReplyMarkup"][-1]
    assert restored["reply_markup"] == cards.keyboard(1)


def test_failure_after_the_row_exists_leaves_no_orphan(env, monkeypatch):
    """A2: voicing fails after the replacement row was created — it must end 'failed', not sit as
    'pending'/'voiced' for the next assemble/review pass to pick up as a duplicate."""
    cfg, conn, _ = env
    _in_review(cfg, conn)
    FakeBuild(monkeypatch, cfg)

    def voice_script(*args, **kwargs):
        raise RuntimeError("edge-tts 403")
    monkeypatch.setattr(botmod, "voice_script", voice_script)
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg, botmod.Deps(stock_client=httpx.Client()))
    h.handle(_cb("rv:1"))
    rows = conn.execute("SELECT id, status, parent_id, notes FROM videos ORDER BY id").fetchall()
    assert [(r["id"], r["status"], r["parent_id"]) for r in rows] == [(1, "in_review", None), (2, "failed", 1)]
    assert "edge-tts 403" in json.loads(rows[1]["notes"])["failed"]
    assert any("Couldn't regenerate #1" in b.get("text", "") for m, b in tg.calls if m == "sendMessage")


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


def test_stale_tap_still_counts(env):
    """A tap handled after a long regeneration can't be acknowledged ('query is too old'),
    but the decision must still be recorded."""
    cfg, conn, _ = env
    _in_review(cfg, conn)
    tg = FakeTelegram(fail="answerCallbackQuery")
    _handler(cfg, conn, tg).handle(_cb("ap:1"))
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == "approved"
    assert conn.execute("SELECT decision FROM approvals").fetchone()[0] == "approved"
