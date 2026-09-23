"""bot: long-polling Telegram handler for the review buttons and commands.

Only updates from TELEGRAM_CHAT_ID are acted on; everything else is ignored. Every state lives in
the DB (videos.status, approvals, control flags), so the bot can be restarted at any time.

  ✅ Approve / ❌ Reject  → approvals row; video approved / rejected
  ✏️ Edit script          → asks for a note; the reply regenerates script → voice → video
  🔁 New b-roll           → same voice, re-assembled without the previous clips
  🎙 Re-voice             → the brand's alternate voice, re-assembled
  /pause /resume /status /report
A regenerated video is a new row (parent_id = old) sent for review; the old one is 'superseded'.
"""
from __future__ import annotations

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


class Handler:
    def __init__(self, cfg: Config, conn: sqlite3.Connection, bot: Bot, chat: str, deps: Deps | None = None):
        self.cfg, self.conn, self.bot, self.chat = cfg, conn, bot, str(chat)
        self.deps = deps or Deps()

    # -- dispatch ----------------------------------------------------------------
    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            cb = update["callback_query"]
            if str(((cb.get("message") or {}).get("chat") or {}).get("id")) != self.chat:
                log.warning("Ignoring callback from unauthorized chat")
                return
            self.on_button(cb)
        elif "message" in update:
            msg = update["message"]
            if str((msg.get("chat") or {}).get("id")) != self.chat:
                log.warning("Ignoring message from unauthorized chat")
                return
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
        row = self.conn.execute("SELECT status, review_msg_id FROM videos WHERE id = ?", (vid,)).fetchone()
        if row is None or row["status"] != "in_review":
            self._soft(self.bot.answer, cb["id"], "Already handled")
            return
        user = str((cb.get("from") or {}).get("id", ""))
        toast = {"ap": "✅ Approved", "rj": "❌ Rejected", "ed": "Send your edit note"}.get(act, "⏳ Working on it…")
        self._soft(self.bot.answer, cb["id"], toast)
        # Buttons come off first so a double tap can't act twice (the status check above also guards it).
        self._soft(self.bot.edit_markup, self.chat, row["review_msg_id"], None)
        if act in ("ap", "rj"):
            decision = cards.ACTIONS[act]
            self._approval(vid, decision, user, row["review_msg_id"])
            self.conn.execute("UPDATE videos SET status = ? WHERE id = ?", (decision, vid))
            self.conn.commit()
            self.bot.send_message(self.chat, f"{toast} #{vid}", reply_to_message_id=row["review_msg_id"])
        elif act == "ed":
            prompt = self.bot.send_message(self.chat, f"✏️ What should change in #{vid}? Reply with your note.",
                                           reply_markup={"force_reply": True, "selective": True},
                                           reply_to_message_id=row["review_msg_id"])
            db.set_flag(self.conn, "pending_edit", json.dumps({"video_id": vid, "prompt": prompt["message_id"],
                                                               "user": user}))
        else:
            self._approval(vid, cards.ACTIONS[act], user, row["review_msg_id"])
            self._regenerate(vid, new_broll=act == "nb", revoice=act == "rv")

    def on_message(self, msg: dict[str, Any]) -> None:
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            cmd = text.split()[0].split("@")[0].lower()
            if cmd == "/pause":
                db.set_flag(self.conn, "publishing_paused", "1")
                self.bot.send_message(self.chat, "⏸ Publishing paused.")
            elif cmd == "/resume":
                db.set_flag(self.conn, "publishing_paused", "0")
                db.set_flag(self.conn, "paused_notice_sent", "0")
                self.bot.send_message(self.chat, "▶️ Publishing resumed.")
            elif cmd in ("/status", "/start"):
                self.bot.send_message(self.chat, self.status())
            elif cmd == "/report":
                from src.analytics.runner import send_weekly
                self._soft(send_weekly, self.cfg, self.conn, self.bot)
            return
        pending = json.loads(db.get_flag(self.conn, "pending_edit") or "null")
        if pending and text:
            db.set_flag(self.conn, "pending_edit", "null")
            vid = pending["video_id"]
            self._approval(vid, "edit", str((msg.get("from") or {}).get("id", "")), None, note=text)
            self.bot.send_message(self.chat, f"⏳ Rewriting #{vid} with your note…")
            self._regenerate(vid, edit_note=text)

    def status(self) -> str:
        counts = dict(self.conn.execute("SELECT status, count(*) FROM videos GROUP BY status").fetchall())
        paused = db.publishing_paused(self.conn)
        lines = ["📊 Ra'ij status", f"Publishing: {'⏸ paused' if paused else '▶️ on'}"]
        lines += [f"{k}: {v}" for k, v in sorted(counts.items())]
        return "\n".join(lines)

    # -- effects -------------------------------------------------------------------
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
