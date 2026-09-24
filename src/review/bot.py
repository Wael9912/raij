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
  /trending /topic /script /run /jobs → production from the bot (Phase 13): the pick flow in review/picks.py,
                            heavy work in background jobs (src/jobs.py) so taps stay snappy
A regenerated video is a new row (parent_id = old) sent for review; the old one is 'superseded'.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from src import db, formats, jobs
from src.assemble import render
from src.assemble.runner import VIDEO_SELECT, assemble_video
from src.config import Config
from src.discover.common import make_client
from src.review import cards, picks
from src.review.runner import RunCmd, run_cmd, send_card
from src.review.telegram import Bot, TelegramError
from src.script.runner import _save
from src.script.write import segment_script, write_script
from src.voice import tts
from src.voice.runner import voice_script

log = logging.getLogger("raij.review")

EDIT_NOTE_TTL = 6 * 3600     # an unanswered "✏️ Edit" prompt expires: a stray text days later mustn't rewrite (S4)
INPUT_TTL = 30 * 60          # a "✍️ Topic" / "📝 Script" prompt waits this long for the text


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


def ensure_keyboard(conn: sqlite3.Connection, bot: Bot, chat: str) -> bool:
    """Show the permanent button bar once per keyboard version (a reply keyboard sticks to the chat once sent)."""
    digest = hashlib.sha256(json.dumps(cards.MAIN_KEYBOARD, sort_keys=True).encode()).hexdigest()[:12]
    if db.get_flag(conn, "keyboard_version") == digest:
        return False
    try:
        bot.send_message(chat, "🔘 Buttons are ready under the chat: 🔥 Trending · ✍️ Topic · 📝 Script · ▶️ Run daily "
                               "· ⚙️ Jobs · 📋 Queue · 📊 Status · ❓ Help", reply_markup=cards.MAIN_KEYBOARD)
    except TelegramError as exc:
        log.info("Keyboard not sent (will retry): %s", exc)
        return False
    db.set_flag(conn, "keyboard_version", digest)
    return True


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

    def _soft(self, fn: Callable, *args: Any, **kwargs: Any) -> None:
        """Telegram cosmetics (the tap's toast, removing buttons) must never block the decision itself:
        a callback answered after a long regeneration is 'too old' and Telegram rejects it."""
        try:
            fn(*args, **kwargs)
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
            if act.startswith("p"):
                self._pick_tap(act, vid, cb["id"], msg_id)
                return
            self._soft(self.bot.answer, cb["id"], "⏳ Working on it…")
            if act == "rt":
                self._retry(vid, user, msg_id)
            elif act == "bx":
                self._soft(self.bot.edit_markup, self.chat, msg_id, None)
                db.set_flag(self.conn, "post_now_ask", "null")
                self.bot.send_message(self.chat, "↩️ Cancelled — nothing changed.")
            elif act == "nw":
                self._soft(self.bot.edit_markup, self.chat, msg_id, None)
                self._post_now(vid, user)
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
        if text in cards.BUTTONS:                          # the button bar sends its label as a message
            cmd = cards.BUTTONS[text]
            if cmd in ("/topic", "/script"):
                self._ask_input(cmd)
            else:
                self.on_command(cmd)
            return
        if text.startswith("/"):
            head, _, rest = text.partition(" ")
            self.on_command(head.split("@")[0].lower(), rest.strip())
            return
        if not text:
            return
        if self._on_input(msg, text):
            return
        self._on_note(msg, text)

    # -- ✍️ Topic / 📝 Script buttons: ask for the text, then treat the reply as the command ----------
    def _ask_input(self, cmd: str) -> None:
        what = ("✍️ What should the video be about? Reply to this message with the topic (any language)."
                if cmd == "/topic" else
                "📝 Reply to this message with your full script. It is voiced exactly as written: up to 115 words "
                "→ Short, longer → Long (up to ~520 words ≈ 4.5 min).")
        prompt = self.bot.send_message(self.chat, what, reply_markup={"force_reply": True, "selective": True})
        db.set_flag(self.conn, "pending_input", json.dumps({"cmd": cmd, "prompt": prompt["message_id"],
                                                             "at": time.time()}))

    def _on_input(self, msg: dict[str, Any], text: str) -> bool:
        """True if the text answered an open ✍️/📝 prompt (a reply to it, or the only thing pending)."""
        try:
            pending = json.loads(db.get_flag(self.conn, "pending_input") or "null")
        except ValueError:
            pending = None
        if not pending:
            return False
        if time.time() - float(pending.get("at") or 0) > INPUT_TTL:
            db.set_flag(self.conn, "pending_input", "null")
            return False
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_to != pending.get("prompt") and (reply_to or self._pending()):
            return False                                    # it's an edit note, or a reply to something else
        db.set_flag(self.conn, "pending_input", "null")
        self.on_command(pending["cmd"], text)
        return True

    def on_command(self, cmd: str, arg: str = "") -> None:
        if cmd in ("/trending", "/run", "/topic", "/script", "/jobs", "/make"):
            self.on_produce(cmd, arg)
        elif cmd == "/pause":
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
        elif cmd == "/post_now":
            self._ask_post_now(arg)
        elif cmd == "/report":
            from src.analytics.runner import send_weekly
            self._soft(send_weekly, self.cfg, self.conn, self.bot, False)     # no backup on demand (U7)
        else:
            self.bot.send_message(self.chat, f"Unknown command {cmd}. /help lists what I understand.")

    def status(self) -> str:
        return cards.status_text(self.conn, db.publishing_paused(self.conn)) + "\n" + jobs.status_line(self.conn)

    # -- production from the bot (Phase 13) --------------------------------------------
    def on_produce(self, cmd: str, arg: str) -> None:
        if cmd == "/jobs":
            tail = jobs.log_tail(self.cfg)
            self.bot.send_message(self.chat, jobs.status_line(self.conn) + (f"\n\n📜 {tail}" if tail else ""),
                                  disable_web_page_preview=True)
            return
        if cmd == "/run":
            queued = jobs.request(self.conn, "run-daily")
            self.bot.send_message(self.chat, "▶️ Daily pipeline queued — discover → pick → write → voice → render; "
                                             "cards arrive here when ready." if queued else
                                             "⏳ The daily pipeline is already queued or running. /jobs for progress.")
            return
        if cmd == "/trending":
            queued = jobs.request(self.conn, "trending")
            self.bot.send_message(self.chat, "🔎 Looking for what's trending (Google Trends, news feeds, Wikipedia) "
                                             "— the list with pick buttons arrives in a minute or two." if queued else
                                             "⏳ A trending search is already queued or running.")
            return
        if cmd in ("/topic", "/make"):
            if len(arg.split()) < 1 or len(arg) < 3:
                self.bot.send_message(self.chat, "Usage: /topic <what the video should be about>\n"
                                                 "e.g. /topic لماذا ارتفع سعر الذهب هذا الأسبوع")
                return
            flow = picks.new(self.cfg, "topic", text=arg)
            self._pick_send(flow)
            return
        if cmd == "/script":
            words = len(arg.split())
            if words < 12:
                self.bot.send_message(self.chat, "Usage: /script <the full script text, at least a dozen words>\n"
                                                 "It is voiced exactly as written; ≤115 words → Short, longer → Long "
                                                 "(up to ~520 words ≈ 4.5 min).")
                return
            fmt = formats.kind_for_words(self.cfg, words)
            if words > formats.get(self.cfg, "long").max_words:
                self.bot.send_message(self.chat, f"That's {words} words — too long even for a 5-minute video "
                                                 f"(max {formats.get(self.cfg, 'long').max_words}). Trim it and resend.")
                return
            flow = picks.new(self.cfg, "script", text=arg, fmts=[fmt], words=words)
            self._pick_send(flow)

    def _missing(self) -> dict[str, str | None]:
        from src.publish.runner import PLATFORMS
        return {name: check(self.cfg) for name, (check, _) in PLATFORMS.items()}

    def _pick_send(self, flow: dict[str, Any]) -> None:
        old = picks.load(self.conn)
        if old and old.get("msg"):
            self._soft(self.bot.edit_markup, self.chat, old["msg"], None)
        msg = self.bot.send_message(self.chat, picks.text(flow, self._missing()), reply_markup=picks.keyboard(flow),
                                    disable_web_page_preview=True)
        flow["msg"] = msg["message_id"]
        picks.save(self.conn, flow)

    def _pick_tap(self, act: str, n: int, cb_id: str, msg_id: int | None) -> None:
        flow = picks.load(self.conn)
        if not flow or (msg_id and flow.get("msg") and flow["msg"] != msg_id):
            self._soft(self.bot.answer, cb_id, "This list is no longer active — /trending again")
            self._soft(self.bot.edit_markup, self.chat, msg_id, None)
            return
        if act == "px":
            picks.save(self.conn, None)
            self._soft(self.bot.answer, cb_id, "Cancelled")
            self._soft(self.bot.edit_text, self.chat, flow["msg"], "✖ Cancelled — nothing was made.", None)
            return
        if act == "pg":
            if flow["kind"] == "trend" and not flow["chosen"]:
                self._soft(self.bot.answer, cb_id, "Pick at least one topic first")
                return
            self._soft(self.bot.answer, cb_id, "🚀 On it")
            try:
                summary = picks.commit(self.cfg, self.conn, flow)
            except ValueError as exc:
                self.bot.send_message(self.chat, f"⚠️ {exc}")
                return
            picks.save(self.conn, None)
            jobs.request(self.conn, "produce")
            self._soft(self.bot.edit_text, self.chat, flow["msg"], picks.text(flow, self._missing()), None)
            self.bot.send_message(self.chat, picks.done_text(summary))
            return
        toast = picks.toggle(flow, act, n)
        picks.save(self.conn, flow)
        self._soft(self.bot.answer, cb_id, toast or "")
        self._soft(self.bot.edit_text, self.chat, flow["msg"], picks.text(flow, self._missing()), picks.keyboard(flow),
                   disable_web_page_preview=True)

    def maintenance(self) -> None:
        """Between polls: start queued jobs, report finished ones (called by `poll` every loop)."""
        try:
            event = jobs.tick(self.cfg, self.conn)
        except Exception as exc:                            # noqa: BLE001
            log.warning("Job maintenance failed: %s", exc)
            return
        if not event:
            return
        if "started" in event:
            log.info("Started job %s", event["started"])
        elif event.get("code") not in (None, 0):
            self.bot.send_message(self.chat, f"⚠️ Job {event['finished']} ended with errors (exit {event['code']}). "
                                             f"/jobs shows the last log lines.")
        else:
            log.info("Job %s finished in %.0f s", event["finished"], event.get("seconds") or 0)

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

    # -- post now (owner's ask 2026-09-24: approved videos sat for days behind the posting windows) ----
    def _post_now_candidates(self, ids: set[int] | None = None) -> list[dict[str, Any]]:
        """Approved videos with at least one connected platform still to post (rushed ones included: the owner
        may ask again if a pass hasn't run yet)."""
        from src.publish.runner import DONE, wanted_platforms
        missing = self._missing()
        out = []
        for v in cards._rows(self.conn, ("approved",)):
            if ids is not None and v["id"] not in ids:
                continue
            posts = {r["platform"]: dict(r) for r in
                     self.conn.execute("SELECT platform, status FROM posts WHERE video_id = ?", (v["id"],))}
            todo = [p for p in wanted_platforms(self.cfg, v) if not missing.get(p)
                    and posts.get(p, {}).get("status") not in DONE]
            if todo:
                out.append({**v, "todo": todo})
        return out

    def _ask_post_now(self, arg: str) -> None:
        ids = {int(t) for t in re.findall(r"\d+", arg or "")} or None
        if db.publishing_paused(self.conn):
            self.bot.send_message(self.chat, "⏸ Publishing is paused — /resume first, then /post_now.")
            return
        rows = self._post_now_candidates(ids)
        if not rows:
            what = "Those videos aren't" if ids else "No approved video is"
            self.bot.send_message(self.chat, f"📭 {what} waiting to post. /queue shows the state.")
            return
        names = sorted({cards.PLATFORM.get(p, p) for v in rows for p in v["todo"]})
        n = len(rows)
        lines = [f"🚀 Post {n} video{'s' if n != 1 else ''} now to {', '.join(names)}? "
                 f"This skips the posting windows."]
        for v in rows:
            lines += [f"#{v['id']} → " + ", ".join(cards.PLATFORM.get(p, p) for p in v["todo"]), cards.title_of(v)]
        off = [cards.PLATFORM.get(p, p) for p, why in self._missing().items() if why
               and any(p in (self._wanted(v)) for v in rows)]
        if off:
            lines.append(f"🔑 Not connected (skipped): {', '.join(sorted(set(off)))}")
        upto = rows[-1]["id"]
        db.set_flag(self.conn, "post_now_ask", json.dumps({"ids": [v["id"] for v in rows], "upto": upto,
                                                           "at": time.time()}))
        self.bot.send_message(self.chat, "\n".join(lines), reply_markup=cards.confirm_keyboard("nw", upto))

    def _wanted(self, v: dict[str, Any]) -> list[str]:
        from src.publish.runner import wanted_platforms
        return wanted_platforms(self.cfg, v)

    def _post_now(self, upto: int, user: str) -> None:
        from src.publish.runner import rush
        try:
            ask = json.loads(db.get_flag(self.conn, "post_now_ask") or "null") or {}
        except ValueError:
            ask = {}
        db.set_flag(self.conn, "post_now_ask", "null")
        ids = set(ask.get("ids") or []) if ask.get("upto") == upto else None
        rows = [v for v in self._post_now_candidates(ids) if v["id"] <= upto]
        marked = rush(self.conn, [v["id"] for v in rows], by=user)
        if not marked:
            self.bot.send_message(self.chat, "📭 Nothing left to post — no change.")
            return
        queued = jobs.request(self.conn, "publish")
        names = sorted({cards.PLATFORM.get(p, p) for v in rows for p in v["todo"]})
        self.bot.send_message(self.chat, f"🚀 Posting {', '.join(f'#{i}' for i in marked)} now → {', '.join(names)}."
                                         + ("" if queued else " A publish pass is already running; the next one takes them.")
                                         + "\nYou'll get a message per upload as it goes out.")

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
            kind = str(ctx.get("kind") or "short")
            own = formats.wanted(ctx)
            if own.get("kind") == "script" and own.get("text"):
                outcome = segment_script(cfg, own["text"], brand, kind, edit_note=edit_note, client=d.llm_client,
                                         seed=int(ctx["story_id"]))
            else:
                outcome = write_script(cfg, story, brand, edit_note=edit_note, client=d.llm_client, kind=kind)
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
         once: bool = False, timeout: int = 30, maintenance: Callable[[], None] | None = None) -> int:
    """Process updates until interrupted (or one batch with once=True). The offset is persisted so a
    restart never replays a button press. `maintenance` (the long-running bot) runs after every poll: job
    queue, reminders."""
    handler = Handler(cfg, conn, bot, chat, deps)
    handled = 0
    while True:
        if not once:
            handler.maintenance()
            if maintenance:
                try:
                    maintenance()
                except Exception as exc:                    # noqa: BLE001 — never kills the poller
                    log.warning("Maintenance failed: %s", exc)
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
