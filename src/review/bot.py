"""bot: long-polling Telegram handler for the review buttons and commands.

Only updates from TELEGRAM_CHAT_ID *sent by the owner* are acted on (the sender's user id must equal
the chat id — a private chat — or TELEGRAM_OWNER_ID when set); everything else is ignored (S4).
Every state lives in the DB (videos.status, approvals, control flags), so the bot can be restarted
at any time.

  ✅ Approve / ❌ Reject  → approvals row; video approved / rejected
  ✏️ Edit script          → asks for a note (the card keeps its buttons); the reply regenerates
                            script → voice → video. One open prompt per video, each expiring on its own (U2)
  🔁 New b-roll           → same voice, re-assembled without the previous clips
  🎙 Re-voice             → the brand's alternate voice, re-assembled
  🔁 Retry                → on a final publish failure: re-approve and queue the failed platforms again (U9)
  /queue /status /approve_all /skip /pause /resume /report /help
A regenerated video is a new row (parent_id = old) sent for review; the old one is 'superseded'.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from src import db
from src.assemble import render
from src.assemble.runner import VIDEO_SELECT, assemble_video
from src.config import Config
from src.discover.common import make_client
from src.review import cards
from src.review.runner import RunCmd, run_cmd, send_card
from src.review.telegram import Bot, TelegramError
from src.script.runner import _save
from src.script.write import write_script
from src.voice import tts
from src.voice.runner import voice_script

log = logging.getLogger("raij.review")

EDIT_NOTE_TTL = 6 * 3600     # an unanswered "✏️ Edit" prompt expires: a stray text days later mustn't rewrite (S4)


@dataclass
class Deps:
    """External effects, injectable for tests."""
    llm_client: httpx.Client | None = None
    stock_client: httpx.Client | None = None
    synth: Callable = tts.synthesize
    voice_run: Callable = tts.run_cmd
    render_run: Callable = render.run_cmd
    preview_run: RunCmd = run_cmd
    extra: dict = field(default_factory=dict)


def ensure_commands(conn: sqlite3.Connection, bot: Bot) -> bool:
    """Register the slash menu once per command-list version (U8); a Telegram failure just retries next time."""
    digest = hashlib.sha256(json.dumps(cards.COMMANDS).encode()).hexdigest()[:12]
    if db.get_flag(conn, "commands_version") == digest:
        return False
    try:
        bot.set_commands(cards.COMMANDS)
    except TelegramError as exc:
        log.info("setMyCommands failed (will retry): %s", exc)
        return False
    db.set_flag(conn, "commands_version", digest)
    return True


class Handler:
    def __init__(self, cfg: Config, conn: sqlite3.Connection, bot: Bot, chat: str, deps: Deps | None = None):
        self.cfg, self.conn, self.bot, self.chat = cfg, conn, bot, str(chat)
        self.owner = str(cfg.secret("TELEGRAM_OWNER_ID") or self.chat)
        self.deps = deps or Deps()

    # -- dispatch ----------------------------------------------------------------
    def _authorized(self, kind: str, chat: dict | None, sender: dict | None) -> bool:
        """The owner's chat *and* the owner as sender: a bot in a group, or a forwarded tap, gets nothing."""
        if str((chat or {}).get("id")) != self.chat:
            log.warning("Ignoring %s from unauthorized chat", kind)
            return False
        if str((sender or {}).get("id")) != self.owner:
            log.warning("Ignoring %s from unauthorized sender", kind)
            return False
        return True

    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            cb = update["callback_query"]
            if self._authorized("callback", (cb.get("message") or {}).get("chat"), cb.get("from")):
                self.on_button(cb)
        elif "message" in update:
            msg = update["message"]
            if self._authorized("message", msg.get("chat"), msg.get("from")):
                self.on_message(msg)

    def _soft(self, fn: Callable, *args: Any) -> None:
        """Telegram cosmetics (the tap's toast, removing buttons) must never block the decision itself:
        a callback answered after a long regeneration is 'too old' and Telegram rejects it."""
        try:
            fn(*args)
        except TelegramError as exc:
            log.info("Ignoring cosmetic Telegram failure: %s", exc)

    def on_button(self, cb: dict[str, Any]) -> None:
        parsed = cards.parse_callback(cb.get("data", ""))
        if not parsed:
            self._soft(self.bot.answer, cb["id"], "Unknown action")
            return
        act, vid = parsed
        user = str((cb.get("from") or {}).get("id", ""))
        msg_id = (cb.get("message") or {}).get("message_id")
        if act in cards.EXTRA:
            self._soft(self.bot.answer, cb["id"], "⏳ Working on it…")
            if act == "rt":
                self._retry(vid, user, msg_id)
            elif act == "bx":
                self._soft(self.bot.edit_markup, self.chat, msg_id, None)
                self.bot.send_message(self.chat, "↩️ Cancelled — nothing changed.")
            else:
                self._soft(self.bot.edit_markup, self.chat, msg_id, None)
                self._bulk("approved" if act == "ba" else "rejected", vid, user)
            return
        row = self.conn.execute("SELECT status, review_msg_id FROM videos WHERE id = ?", (vid,)).fetchone()
        if row is None or row["status"] != "in_review":
            self._soft(self.bot.answer, cb["id"], "Already handled")
            return
        toast = {"ap": "✅ Approved", "rj": "❌ Rejected", "ed": "Send your edit note"}.get(act, "⏳ Working on it…")
        self._soft(self.bot.answer, cb["id"], toast)
        if act == "ed":
            # The card keeps its buttons: the owner may still approve or reject while the prompt is open (U2).
            prompt = self.bot.send_message(self.chat, f"✏️ What should change in #{vid}? Reply to this message "
                                                      f"with your note.",
                                           reply_markup={"force_reply": True, "selective": True},
                                           reply_to_message_id=row["review_msg_id"])
            pending = self._pending()
            pending[str(vid)] = {"prompt": prompt["message_id"], "user": user, "at": time.time()}
            self._save_pending(pending)
            return
        # Buttons come off first so a double tap can't act twice (the status check above also guards it).
        self._soft(self.bot.edit_markup, self.chat, row["review_msg_id"], None)
        self._drop_pending(vid)
        if act in ("ap", "rj"):
            self._decide(vid, cards.ACTIONS[act], user, row["review_msg_id"])
            self.bot.send_message(self.chat, f"{toast} #{vid}\n{self._title(vid)}",
                                  reply_to_message_id=row["review_msg_id"])
        else:
            self._approval(vid, cards.ACTIONS[act], user, row["review_msg_id"])
            self._regenerate(vid, new_broll=act == "nb", revoice=act == "rv")

    def on_message(self, msg: dict[str, Any]) -> None:
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            self.on_command(text.split()[0].split("@")[0].lower())
            return
        if not text:
            return
        self._on_note(msg, text)

    def on_command(self, cmd: str) -> None:
        if cmd == "/pause":
            db.set_flag(self.conn, "publishing_paused", "1")
            self.bot.send_message(self.chat, "⏸ Publishing paused. /resume to continue.")
        elif cmd == "/resume":
            db.set_flag(self.conn, "publishing_paused", "0")
            db.set_flag(self.conn, "paused_notice_sent", "0")
            self.bot.send_message(self.chat, "▶️ Publishing resumed.")
        elif cmd == "/status":
            self.bot.send_message(self.chat, self.status())
        elif cmd == "/queue":
            self.bot.send_message(self.chat, self.queue(), disable_web_page_preview=True)
        elif cmd in ("/help", "/start"):
            self.bot.send_message(self.chat, cards.HELP)
        elif cmd in ("/approve_all", "/skip"):
            self._ask_bulk("ba" if cmd == "/approve_all" else "bs")
        elif cmd == "/report":
            from src.analytics.runner import send_weekly
            self._soft(send_weekly, self.cfg, self.conn, self.bot, False)     # no backup on demand (U7)
        else:
            self.bot.send_message(self.chat, f"Unknown command {cmd}. /help lists what I understand.")

    def status(self) -> str:
        return cards.status_text(self.conn, db.publishing_paused(self.conn))

    def queue(self) -> str:
        from src.publish.runner import PLATFORMS
        missing = {name: check(self.cfg) for name, (check, _) in PLATFORMS.items()}
        return cards.queue_text(self.conn, self.cfg, missing)

    # -- edit prompts (one per video, each with its own expiry — U2) ----------------------
    def _pending(self) -> dict[str, dict[str, Any]]:
        raw = json.loads(db.get_flag(self.conn, "pending_edits") or "{}") or {}
        legacy = json.loads(db.get_flag(self.conn, "pending_edit") or "null")      # single-prompt flag (pre-11)
        if legacy and str(legacy.get("video_id")) not in raw:
            raw[str(legacy["video_id"])] = {k: legacy[k] for k in ("prompt", "user", "at") if k in legacy}
        return raw

    def _save_pending(self, pending: dict[str, dict[str, Any]]) -> None:
        db.set_flag(self.conn, "pending_edits", json.dumps(pending))
        db.set_flag(self.conn, "pending_edit", "null")

    def _drop_pending(self, vid: int) -> None:
        pending = self._pending()
        if pending.pop(str(vid), None) is not None:
            self._save_pending(pending)

    def _on_note(self, msg: dict[str, Any], text: str) -> None:
        pending = self._pending()
        if not pending:
            return
        now = time.time()
        expired = [k for k, p in pending.items() if now - float(p.get("at") or 0) > EDIT_NOTE_TTL]
        for k in expired:
            pending.pop(k)
            log.info("Edit prompt for #%s expired", k)
        if expired:
            self._save_pending(pending)
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        hit = next((k for k, p in pending.items() if p.get("prompt") == reply_to), None)
        if hit is None:
            # Only the reply to a ForceReply prompt is a note (S4): a stray text must not rewrite a video.
            if expired and not pending:
                self.bot.send_message(self.chat, "⌛ The ✏️ prompt for #" + ", #".join(expired)
                                      + " expired — tap ✏️ Edit script again.")
            elif pending:
                self.bot.send_message(self.chat, "↩️ To edit #" + ", #".join(pending)
                                      + ", reply to its ✏️ prompt (or tap ✏️ again).")
            return
        vid = int(hit)
        pending.pop(hit)
        self._save_pending(pending)
        row = self.conn.execute("SELECT status, review_msg_id FROM videos WHERE id = ?", (vid,)).fetchone()
        if row is None or row["status"] != "in_review":
            self.bot.send_message(self.chat, f"#{vid} is no longer in review ({row['status'] if row else 'gone'}) "
                                             f"— note ignored.")
            return
        self._soft(self.bot.edit_markup, self.chat, row["review_msg_id"], None)
        self._approval(vid, "edit", str((msg.get("from") or {}).get("id", "")), row["review_msg_id"], note=text)
        self.bot.send_message(self.chat, f"⏳ Rewriting #{vid} with your note…")
        self._regenerate(vid, edit_note=text)

    # -- bulk (U: /approve_all, /skip — always behind a confirm button) -------------------
    def _ask_bulk(self, act: str) -> None:
        review = cards.in_review(self.conn)
        if not review:
            self.bot.send_message(self.chat, "📭 Nothing in review.")
            return
        verb = "Approve" if act == "ba" else "Reject"
        lines = [f"{verb} all {len(review)} card{'s' if len(review) != 1 else ''} in review?"]
        for v in review:
            lines += [f"#{v['id']}", cards.title_of(v)]
        self.bot.send_message(self.chat, "\n".join(lines), reply_markup=cards.confirm_keyboard(act, review[-1]["id"]))

    def _bulk(self, decision: str, upto: int, user: str) -> None:
        review = [v for v in cards.in_review(self.conn) if v["id"] <= upto]
        if not review:
            self.bot.send_message(self.chat, "📭 Nothing left in review — no change.")
            return
        for v in review:
            msg_id = self.conn.execute("SELECT review_msg_id FROM videos WHERE id = ?", (v["id"],)).fetchone()[0]
            self._soft(self.bot.edit_markup, self.chat, msg_id, None)
            self._drop_pending(v["id"])
            self._decide(v["id"], decision, user, msg_id)
        mark = "✅ Approved" if decision == "approved" else "❌ Rejected"
        self.bot.send_message(self.chat, f"{mark} {len(review)} card{'s' if len(review) != 1 else ''}: "
                                         + ", ".join(f"#{v['id']}" for v in review))

    # -- retry a final publish failure (U9) ----------------------------------------------
    def _retry(self, vid: int, user: str, msg_id: int | None) -> None:
        row = self.conn.execute("SELECT status FROM videos WHERE id = ?", (vid,)).fetchone()
        if row is None or row["status"] != "approved":
            self.bot.send_message(self.chat, f"#{vid} is {row['status'] if row else 'gone'} — nothing to retry.")
            return
        failed = [r["platform"] for r in self.conn.execute(
            "SELECT platform FROM posts WHERE video_id = ? AND status = 'failed' ORDER BY id", (vid,))]
        if not failed:
            self.bot.send_message(self.chat, f"#{vid} has no failed platform — nothing to retry.")
            return
        self._soft(self.bot.edit_markup, self.chat, msg_id, None)
        # A fresh approval row renews the publish window (max_age_hours) — the owner just asked for it.
        self._approval(vid, "approved", user, msg_id, note="retry")
        self.conn.execute("UPDATE posts SET attempts = 0, status = 'queued', error = NULL, last_attempt_at = NULL "
                          "WHERE video_id = ? AND status = 'failed'", (vid,))
        self.conn.commit()
        names = ", ".join(cards.PLATFORM.get(p, p) for p in failed)
        self.bot.send_message(self.chat, f"🔁 #{vid} queued again for {names} — next pass tries it.")

    # -- effects -------------------------------------------------------------------
    def _title(self, vid: int) -> str:
        try:
            return cards.title_of(cards.context(self.conn, vid))
        except KeyError:
            return ""

    def _decide(self, vid: int, decision: str, user: str, msg_id: int | None) -> None:
        self._approval(vid, decision, user, msg_id)
        self.conn.execute("UPDATE videos SET status = ? WHERE id = ?", (decision, vid))
        self.conn.commit()

    def _approval(self, vid: int, decision: str, user: str, msg_id: int | None, note: str | None = None) -> None:
        self.conn.execute("INSERT INTO approvals (video_id, decision, note, decided_by, telegram_msg_id) "
                          "VALUES (?, ?, ?, ?, ?)", (vid, decision, note, user, msg_id))
        self.conn.commit()

    def _regenerate(self, vid: int, edit_note: str = "", new_broll: bool = False, revoice: bool = False) -> None:
        """Build a replacement video and send it; on failure, report and put the old one back up."""
        ctx = cards.context(self.conn, vid)
        # The replacement row exists before any work starts (A2): a failure half-way must leave it 'failed',
        # never 'voiced'/'pending' where tomorrow's assemble would pick it up and review would send a duplicate.
        new_id = self.conn.execute("INSERT INTO videos (script_id, parent_id, status) VALUES (?, ?, 'pending')",
                                   (ctx["script_id"], vid)).lastrowid
        self.conn.commit()
        try:
            self._build(ctx, new_id, edit_note, new_broll, revoice)
        except Exception as exc:                               # tell the reviewer, keep the old video
            log.exception("Regenerating video %d failed", vid)
            self.conn.execute("UPDATE videos SET status = 'failed', notes = ? WHERE id = ?",
                              (json.dumps({"failed": f"regeneration: {str(exc)[:300]}"}, ensure_ascii=False), new_id))
            self.conn.commit()
            self.bot.send_message(self.chat, f"⚠️ Couldn't regenerate #{vid}: {str(exc)[:300]}\n"
                                             f"The original is back up for review.")
            self._soft(self.bot.edit_markup, self.chat, ctx["review_msg_id"], cards.keyboard(vid))
            return
        self.conn.execute("UPDATE videos SET status = 'superseded' WHERE id = ?", (vid,))
        self.conn.commit()
        send_card(self.cfg, self.conn, self.bot, self.chat, new_id, run=self.deps.preview_run)

    def _build(self, ctx: dict[str, Any], new_id: int, edit_note: str, new_broll: bool, revoice: bool) -> None:
        cfg, conn, d = self.cfg, self.conn, self.deps
        script_id, voice_path, duration, notes = ctx["script_id"], ctx["voice_path"], ctx["duration_s"], {}
        brand = next((b for b in cfg.brands if b["id"] == ctx["brand_id"]), {"id": ctx["brand_id"]})

        if edit_note:
            story = {k: ctx[k] for k in ("hook", "key_facts", "claims", "why_trending", "transcript")}
            story["id"] = ctx["story_id"]
            outcome = write_script(cfg, story, brand, edit_note=edit_note, client=d.llm_client)
            final = outcome.final
            if final["status"] != "passed":
                raise RuntimeError(f"the rewrite was rejected — {final['notes'].get('reason', 'unknown reason')}")
            base = conn.execute("SELECT max(version) FROM scripts WHERE story_id = ? AND brand_id = ?",
                                (ctx["story_id"], ctx["brand_id"])).fetchone()[0] or 0
            for v in outcome.versions:
                v["version"] += base
            script_id = _save(conn, ctx["story_id"], ctx["brand_id"], outcome.versions)
            conn.execute("UPDATE scripts SET edit_note = ? WHERE id = ?", (edit_note, script_id))
            conn.execute("UPDATE scripts SET status = 'superseded' WHERE id = ?", (ctx["script_id"],))
            conn.execute("UPDATE videos SET script_id = ? WHERE id = ?", (script_id, new_id))
            conn.commit()

        if edit_note or revoice:
            script = dict(conn.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone())
            voice_name = None
            if revoice:
                v = brand.get("voice") or {}
                current = json.loads(ctx.get("notes") or "{}").get("voice")
                voice_name = v.get("name") if current == v.get("alt") else v.get("alt", "ar-SA-HamedNeural")
            row = voice_script(cfg, script, cfg.root / "assets" / "generated" / "voice", synth=d.synth,
                               run=d.voice_run, voice_name=voice_name, stem=f"{script_id}_v{new_id}")
            voice_path, duration, notes = row["voice_path"], row["duration_s"], row["notes"]
        else:
            notes = {k: v for k, v in json.loads(ctx.get("notes") or "{}").items()
                     if k in ("voice", "rate", "lufs", "true_peak", "words", "script_words")}
        conn.execute("UPDATE videos SET voice_path = ?, duration_s = ?, status = 'voiced', notes = ? WHERE id = ?",
                     (voice_path, duration, json.dumps(notes, ensure_ascii=False), new_id))
        conn.commit()

        video = dict(conn.execute(f"{VIDEO_SELECT} WHERE v.id = ?", (new_id,)).fetchone())
        exclude = set()
        if new_broll:
            exclude = {f"{m['provider']}:{m['id']}" for m in json.loads(ctx.get("broll_manifest") or "[]")
                       if m.get("provider") != "wikimedia"}
        client = d.stock_client or make_client()
        try:
            out = assemble_video(cfg, video, client, run=d.render_run, exclude=exclude)
        finally:
            if d.stock_client is None:
                client.close()
        conn.execute("UPDATE videos SET video_path = ?, subtitle_path = ?, broll_manifest = ?, duration_s = ?, "
                     "status = 'rendered', notes = ? WHERE id = ?",
                     (out["video_path"], out["subtitle_path"], json.dumps(out["manifest"], ensure_ascii=False),
                      out["duration_s"], json.dumps({**notes, **out["notes"]}, ensure_ascii=False), new_id))
        conn.commit()


def poll(cfg: Config, conn: sqlite3.Connection, bot: Bot, chat: str, deps: Deps | None = None,
         once: bool = False, timeout: int = 30) -> int:
    """Process updates until interrupted (or one batch with once=True). The offset is persisted so a
    restart never replays a button press."""
    handler = Handler(cfg, conn, bot, chat, deps)
    handled = 0
    while True:
        offset = db.get_flag(conn, "telegram_offset")
        try:
            updates = bot.updates(int(offset) if offset else None, timeout=0 if once else timeout)
        except TelegramError as exc:
            log.error("getUpdates failed: %s", exc)
            if once:
                return handled
            time.sleep(10)
            continue
        for u in updates:
            db.set_flag(conn, "telegram_offset", str(u["update_id"] + 1))    # before handling: no replays
            try:
                handler.handle(u)
            except Exception:
                log.exception("Update %s failed", u.get("update_id"))
            handled += 1
        if once:
            return handled
