"""Ra'ij CLI. Usage: python -m src.main <command> [--dry-run]"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src import db, textshape
from src.config import load_config

log = logging.getLogger("raij")

# Pipeline stages in run-daily order. Each is implemented in its own phase.
STAGES = {
    "discover": "Pull trending candidates from YouTube, Reddit, Trends, RSS",
    "rank": "Score candidates and keep the top retellable ones",
    "extract": "Get transcripts and distill story cards (no media persists)",
    "script": "Write original Arabic scripts + similarity gate",
    "voice": "Generate Arabic voiceover with edge-tts",
    "assemble": "Render 1080x1920 video from stock b-roll + Arabic subtitles",
    "review": "Send drafts to Telegram for approval",
    "publish": "Publish approved videos (IG/FB Reels, YT Shorts, TikTok export)",
    "report": "Collect metrics and send the weekly report",
}


def cmd_discover(cfg, conn, args) -> int:
    from src.discover.runner import discover
    return discover(cfg, conn, only=getattr(args, "source", None), dry_run=args.dry_run)


def cmd_rank(cfg, conn, args) -> int:
    from src.rank.runner import rank
    return rank(cfg, conn, dry_run=args.dry_run)


def cmd_extract(cfg, conn, args) -> int:
    from src.extract.runner import extract
    return extract(cfg, conn, dry_run=args.dry_run)


def cmd_script(cfg, conn, args) -> int:
    from src.script.runner import script
    return script(cfg, conn, dry_run=args.dry_run)


def cmd_voice(cfg, conn, args) -> int:
    from src.voice.runner import voice
    return voice(cfg, conn, dry_run=args.dry_run)


def cmd_assemble(cfg, conn, args) -> int:
    from src.assemble.runner import assemble
    return assemble(cfg, conn, dry_run=args.dry_run)


def cmd_review(cfg, conn, args) -> int:
    from src.review.runner import review
    return review(cfg, conn, dry_run=args.dry_run)


def cmd_bot(cfg, conn, args) -> int:
    """Long-running: handle review buttons and commands from Telegram; between polls start queued production
    jobs (src/jobs.py) and send the 48/72 h reminders once an hour."""
    import time
    from src.review.bot import ensure_commands, ensure_keyboard, poll
    from src.review.runner import make_bot, remind
    from src.review.telegram import TelegramError
    if args.dry_run:
        log.info("[dry run] would long-poll Telegram for review decisions (chat %s)",
                 "set" if cfg.secret("TELEGRAM_CHAT_ID") else "NOT set")
        return 0
    try:
        bot, chat = make_bot(cfg)
    except TelegramError as exc:
        log.error("Telegram not configured: %s (see SETUP.md §4)", exc)
        return 1
    ensure_commands(conn, bot)
    ensure_keyboard(conn, bot, chat)
    last = {"remind": 0.0}

    def maintenance() -> None:
        if time.time() - last["remind"] > 3600:
            last["remind"] = time.time()
            remind(cfg, conn, bot, chat)

    log.info("Review bot listening — Ctrl+C to stop")
    try:
        poll(cfg, conn, bot, chat, maintenance=maintenance)
    except KeyboardInterrupt:
        log.info("Review bot stopped")
    return 0


def cmd_publish(cfg, conn, args) -> int:
    from src.publish.runner import publish
    return publish(cfg, conn, dry_run=args.dry_run, now=bool(getattr(args, "now", False)))


def cmd_youtube_auth(cfg, conn, args) -> int:
    """One-time browser consent for YouTube uploads; stores a refresh token in data/."""
    from src.discover.common import make_client
    from src.publish import youtube
    if args.dry_run:
        log.info("[dry run] would open Google consent using %s", youtube.secret_file(cfg).name)
        return 0
    if not youtube.secret_file(cfg).exists():
        log.error("No OAuth client file at %s — see SETUP.md §6", youtube.secret_file(cfg))
        return 1
    with make_client() as client:
        path = youtube.authorize(cfg, client)
    log.info("YouTube authorized — token saved to %s", path.relative_to(cfg.root))
    return 0


def cmd_tiktok_auth(cfg, conn, args) -> int:
    """One-time TikTok consent (Desktop Login Kit, loopback redirect); stores tokens in data/tiktok.token.json."""
    from src.discover.common import make_client
    from src.publish import tiktok_api
    why = tiktok_api.missing(cfg)
    if why and "not set" in why:
        log.error("%s", why)
        return 1
    web = tiktok_api.web_redirect(cfg)
    if args.dry_run:
        log.info("[dry run] would open TikTok consent for scopes %s with redirect %s", ",".join(tiktok_api.scopes(cfg)),
                 web or tiktok_api.redirect_uri(cfg))
        return 0
    code = getattr(args, "code", None)
    if web and not code:
        tiktok_api.start_web(cfg)
        log.info("Approve Ra'ij in the browser; the page that follows shows the `tiktok-auth --code … --state …` "
                 "command to run here (valid 15 min).")
        return 0
    with make_client() as client:
        if web:
            path, who = tiktok_api.finish_web(cfg, client, code, getattr(args, "state", None))
        else:
            path, who = tiktok_api.authorize(cfg, client)
    log.info("TikTok authorized%s — token saved to %s (mode: %s)", f" as {who}" if who else "",
             path.relative_to(cfg.root), tiktok_api.mode(cfg))
    return 0


def cmd_report(cfg, conn, args) -> int:
    from src.analytics.runner import report
    return report(cfg, conn, dry_run=args.dry_run, weekly=True if getattr(args, "weekly", False) else None)


def cmd_install_services(cfg, conn, args) -> int:
    from src import service
    if args.dry_run:
        if service.linux():
            for name, body in service.units(cfg).items():
                log.info("[dry run] %s:\n%s", name, body)
        else:
            for label, spec in service.plists(cfg).items():
                log.info("[dry run] %s: %s", label, " ".join(spec["ProgramArguments"][-1:]))
        return 0
    for path in service.install(cfg):
        log.info("Installed %s", path)
    log.info("Logs: data/logs/ — status: `uv run python -m src.main services`")
    return 0


def cmd_uninstall_services(cfg, conn, args) -> int:
    from src import service
    log.info("Removed: %s", ", ".join(service.uninstall()) or "nothing was installed")
    return 0


def cmd_services(cfg, conn, args) -> int:
    from src import service
    for label, state in service.status().items():
        print(f"{label:18} {state}")
    return 0


def daily_due(cfg, conn, now=None) -> str | None:
    """Local date string if today's run-daily hasn't started and it's past schedule.run_daily_at."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = now or datetime.now(ZoneInfo(cfg.get("schedule.timezone", "Africa/Cairo")))
    today = now.strftime("%Y-%m-%d")
    if now.strftime("%H:%M") >= str(cfg.get("schedule.run_daily_at", "07:00")) and \
            db.get_flag(conn, "last_daily_run") != today:
        return today
    return None


def cmd_tick(cfg, conn, args) -> int:
    """One short pass for hosts without always-on processes (GitHub Actions, every ~10 min): start the daily
    run when due, handle queued Telegram taps, publish approvals. Touches data/.changed when the state changed,
    so the caller knows whether to save it."""
    from src.publish.runner import publish
    from src.review.bot import ensure_commands, ensure_keyboard, poll
    from src.review.runner import make_bot, remind
    before = conn.total_changes
    code = 0
    try:
        today = daily_due(cfg, conn)
        if today and not args.dry_run:
            db.set_flag(conn, "last_daily_run", today)      # set first: a crash mustn't loop the whole day
            code |= cmd_run_daily(cfg, conn, args)
        elif today:
            log.info("[dry run] daily run is due (%s)", today)
        if not args.dry_run:
            try:
                bot, chat = make_bot(cfg)
                ensure_commands(conn, bot)                     # slash menu, once per version (U8)
                ensure_keyboard(conn, bot, chat)               # button bar, once per version
                while poll(cfg, conn, bot, chat, once=True):  # drain every queued tap
                    pass
                remind(cfg, conn, bot, chat)                   # 48 h / 72 h nudges for waiting cards (U5)
            except Exception as exc:
                log.error("Telegram pass failed: %s", exc)
                code = 1
            # No long-lived bot here: jobs the owner asked for (/trending, /topic, /run) run inside this tick.
            from src import jobs
            done = jobs.run_queued_inline(cfg, conn, lambda name: JOBS[name](cfg, conn, args))
            if done:
                log.info("Ran queued job(s): %s", ", ".join(done))
        code |= publish(cfg, conn, dry_run=args.dry_run)
    finally:
        # Whatever happened (A1): if the DB changed, the caller must save it, or the next tick replays the day
        # (daily pipeline again, cards re-sent, quota burned).
        changed = conn.total_changes != before
        if changed and not args.dry_run:
            (cfg.root / "data" / ".changed").touch()
        log.info("Tick done (%s)", "state changed" if changed else "no changes")
    return code


def cmd_finalize(cfg, conn, args) -> int:
    """Close approved videos now: 'published' when every configured platform is done (owner's call not to wait
    for missing keys), 'expired' when nothing went out. `publish` does the same by itself after max_age_hours."""
    from src.publish.runner import finalize
    closed = finalize(cfg, conn, max_age_hours=None, dry_run=args.dry_run)
    for vid, status in closed:
        log.info("%svideo %d → %s", "[dry run] " if args.dry_run else "", vid, status)
    log.info("%d approved video(s) closed", len(closed))
    return 0


def cmd_state(cfg, conn, args) -> int:
    """pack/unpack the encrypted state bundle (GitHub Actions persistence)."""
    from src import state
    path = Path(args.file)
    if args.action == "pack":
        state.pack(cfg, path, with_media=not args.db_only)
        log.info("State packed → %s (%.1f MB)", path, path.stat().st_size / 1e6)
    else:
        names = state.unpack(cfg, path)
        log.info("State restored: %d file(s)", len(names))
    return 0


HANDLERS = {"discover": cmd_discover, "rank": cmd_rank, "extract": cmd_extract, "script": cmd_script,
            "voice": cmd_voice, "assemble": cmd_assemble, "review": cmd_review, "publish": cmd_publish,
            "report": cmd_report}
assert set(HANDLERS) == set(STAGES)


def cmd_init_db(cfg, conn, args) -> int:
    if args.dry_run:
        log.info("Would initialize schema at %s", cfg.db_path)
        return 0
    db.init_db(conn)
    log.info("Database ready at %s", cfg.db_path)
    return 0


PRODUCE_STAGES = ("extract", "script", "voice", "assemble", "review")


def _notify_failures(cfg, failures: list[str], what: str) -> None:
    try:
        from src.review.runner import make_bot
        bot, chat = make_bot(cfg)
        bot.send_message(chat, f"⚠️ {what}: {', '.join(failures)} had problems — {log_hint()}")
    except Exception as exc:
        log.warning("Couldn't send the failure notice: %s", exc)


def cmd_produce(cfg, conn, args) -> int:
    """Make videos for everything already selected (the owner's picks, topics and scripts from the bot):
    extract → script → voice → assemble → review. Loops while new selected work appears (a pick made during the
    run), at most 3 rounds. Shares the `pipeline` lock with run-daily."""
    from src.lock import Busy, single
    failures: list[str] = []
    try:
        with single(cfg.root, "pipeline"):
            seen: set[int] = set()
            for _round in range(3):
                selected = {r[0] for r in conn.execute("SELECT id FROM candidates WHERE status = 'selected'")}
                pending = len(selected) + conn.execute("SELECT count(*) FROM candidates WHERE status = 'extracted'"
                                                       ).fetchone()[0]
                work = pending or conn.execute(
                    "SELECT count(*) FROM scripts x WHERE x.status = 'passed' AND NOT EXISTS "
                    "(SELECT 1 FROM videos v WHERE v.script_id = x.id)").fetchone()[0] or conn.execute(
                    "SELECT count(*) FROM videos WHERE status IN ('voiced', 'rendered')").fetchone()[0]
                if not work or (_round and not (selected - seen)):   # another round only for picks made meanwhile
                    if not _round:
                        log.info("Produce: nothing selected — nothing to make")
                    break
                seen |= selected
                failures = []
                for name in PRODUCE_STAGES:
                    try:
                        code = HANDLERS[name](cfg, conn, args)
                    except Exception:
                        log.exception("Stage '%s' failed; continuing", name)
                        code = 1
                    if code:
                        failures.append(name)
                if args.dry_run:
                    break
    except Busy as exc:
        log.warning("produce skipped: %s (the running pipeline will pick the work up)", exc)
        return 0
    if failures and not args.dry_run:
        _notify_failures(cfg, failures, "Producing your picks")
    return 1 if failures else 0


def cmd_trending(cfg, conn, args) -> int:
    """Discover + screen (no automatic selection), then send the owner a pick list in Telegram (Phase 13)."""
    from src.lock import Busy, single
    from src.rank.runner import rank, shortlist
    from src.review import picks
    from src.review.runner import make_bot
    try:
        with single(cfg.root, "pipeline"):
            code = cmd_discover(cfg, conn, args)
            code |= rank(cfg, conn, dry_run=args.dry_run, select=False)
    except Busy as exc:
        log.warning("trending skipped: %s", exc)
        return 0
    items = shortlist(cfg, conn)
    if args.dry_run:
        log.info("[dry run] would offer %d item(s) to pick from", len(items))
        for r in items:
            log.info("[dry run] #%d %-9s %-10s fit %s  %s", r["id"], r["source"], r.get("category") or "?",
                     r.get("audience_fit") or "?", (r["title"] or "")[:60])
        return code
    bot, chat = make_bot(cfg)
    if not items:
        bot.send_message(chat, "😶 Nothing new to pick from right now — the screen found no fresh retellable items. "
                               "Try later, or /topic <something>.")
        return code
    flow = picks.new(cfg, "trend", items=items)
    from src.publish.runner import PLATFORMS
    missing = {name: check(cfg) for name, (check, _) in PLATFORMS.items()}
    msg = bot.send_message(chat, picks.text(flow, missing), reply_markup=picks.keyboard(flow),
                           disable_web_page_preview=True)
    flow["msg"] = msg["message_id"]
    picks.save(conn, flow)
    log.info("Pick list sent: %d items", len(items))
    return code


def cmd_topic(cfg, conn, args) -> int:
    """CLI twin of /topic: `topic "…" [--long] [--platforms youtube,tiktok_export]` then `produce`."""
    from src.discover import manual
    fmts = ["short", "long"] if getattr(args, "both", False) else (["long"] if getattr(args, "long", False) else ["short"])
    plats = [p for p in (getattr(args, "platforms", None) or "").split(",") if p] or \
        list((cfg.brands or [{}])[0].get("platforms") or [])
    if args.dry_run:
        log.info("[dry run] would add topic %r (%s) → %s", args.text, "+".join(fmts), ", ".join(plats))
        return 0
    cid = manual.add_topic(cfg, conn, args.text, fmts, plats)
    log.info("Topic queued as candidate #%d (%s) — run `produce` to make it", cid, "+".join(fmts))
    return 0


def cmd_script_file(cfg, conn, args) -> int:
    """CLI twin of /script: `from-script FILE [--platforms …]` — the file's text is voiced as written."""
    from src.discover import manual
    text = Path(args.file).read_text(encoding="utf-8")
    plats = [p for p in (getattr(args, "platforms", None) or "").split(",") if p] or \
        list((cfg.brands or [{}])[0].get("platforms") or [])
    if args.dry_run:
        log.info("[dry run] would add a %d-word script → %s", len(text.split()), ", ".join(plats))
        return 0
    cid, kind = manual.add_script(cfg, conn, text, plats)
    log.info("Script queued as candidate #%d (%s) — run `produce` to make it", cid, kind)
    return 0


def cmd_run_daily(cfg, conn, args) -> int:
    """Chain all stages; one stage failing must not kill the rest. Failures are sent to Telegram."""
    from src.lock import Busy, single
    try:
        with single(cfg.root, "pipeline"):
            failures = []
            for name in STAGES:
                if name == "publish" and db.publishing_paused(conn):
                    log.warning("Publishing is paused (kill switch) — skipping publish")
                    continue
                try:
                    code = HANDLERS[name](cfg, conn, args)
                except Exception:
                    log.exception("Stage '%s' failed; continuing", name)
                    code = 1
                if code:
                    failures.append(name)
    except Busy as exc:
        log.warning("run-daily skipped: %s", exc)
        return 0
    if failures and not args.dry_run:
        _notify_failures(cfg, failures, "Daily run")
    return 1 if failures else 0


# Jobs the bot can queue (src/jobs.py) — run inline by `tick` on GitHub Actions.
JOBS = {"trending": cmd_trending, "produce": cmd_produce, "run-daily": cmd_run_daily, "publish": cmd_publish}


def log_hint() -> str:
    """Where the owner can read the log for this run (U6): the Actions run when there is one, else the local file."""
    import os
    run, repo, server = os.getenv("GITHUB_RUN_ID"), os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_SERVER_URL")
    if run and repo:
        return f"log: {server or 'https://github.com'}/{repo}/actions/runs/{run}"
    return "see data/logs/daily.log"


def cmd_pause(cfg, conn, args) -> int:
    db.set_flag(conn, "publishing_paused", "1")
    log.info("Publishing paused")
    return 0


def cmd_resume(cfg, conn, args) -> int:
    db.set_flag(conn, "publishing_paused", "0")
    db.set_flag(conn, "paused_notice_sent", "0")            # the next pause tells the owner again (A9)
    log.info("Publishing resumed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="raij", description="Ra'ij trend-to-Arabic-shorts engine")
    parser.add_argument("--config", help="Path to config.yaml (default: repo root)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    commands = {
        "init-db": ("Create/upgrade the SQLite schema", cmd_init_db),
        **{name: (help_, HANDLERS[name]) for name, help_ in STAGES.items()},
        "run-daily": ("Run every stage in order", cmd_run_daily),
        "produce": ("Make videos for everything selected (owner picks, topics, scripts): extract → review",
                    cmd_produce),
        "trending": ("Discover + screen now and send a Telegram pick list (no automatic selection)", cmd_trending),
        "topic": ("Queue a video about a topic (then `produce`)", cmd_topic),
        "from-script": ("Queue an owner-written script file to voice and render (then `produce`)", cmd_script_file),
        "bot": ("Listen for Telegram review decisions (long-running)", cmd_bot),
        "youtube-auth": ("One-time Google consent for YouTube uploads", cmd_youtube_auth),
        "tiktok-auth": ("One-time TikTok consent (inbox drafts now, direct posting after the audit)", cmd_tiktok_auth),
        "install-services": ("Run bot, daily pipeline and publishing in the background (launchd)",
                             cmd_install_services),
        "uninstall-services": ("Stop and remove the background services", cmd_uninstall_services),
        "services": ("Show background service status", cmd_services),
        "tick": ("One short pass: daily run if due, Telegram taps, publish (for GitHub Actions)", cmd_tick),
        "finalize": ("Close approved videos now instead of waiting publish.max_age_hours for missing platforms",
                     cmd_finalize),
        "state": ("Pack/unpack the encrypted state bundle", cmd_state),
        "pause": ("Kill switch: halt all publishing", cmd_pause),
        "resume": ("Re-enable publishing", cmd_resume),
    }
    for name, (help_, func) in commands.items():
        p = sub.add_parser(name, help=help_, description=help_)
        p.add_argument("--dry-run", action="store_true", help="Show what would happen; no side effects")
        p.set_defaults(func=func)
        if name == "tiktok-auth":
            p.add_argument("--code", help="The code from the callback page (or the whole callback URL)")
            p.add_argument("--state", help="The state from the callback page")
        if name == "publish":
            p.add_argument("--now", action="store_true",
                           help="Ignore the posting windows: post every approved video to its connected platforms now")
        if name == "discover":
            p.add_argument("--source", action="append", choices=["youtube", "reddit", "trends", "wiki", "rss"],
                           help="Only run this source (repeatable)")
        if name == "state":
            p.add_argument("action", choices=["pack", "unpack"])
            p.add_argument("file", help="Encrypted bundle path")
            p.add_argument("--db-only", action="store_true", help="Pack the database only (no media)")
        if name == "report":
            p.add_argument("--weekly", action="store_true", help="Send the weekly report to Telegram now")
        if name == "topic":
            p.add_argument("text", help="What the video should be about (any language)")
            p.add_argument("--long", action="store_true", help="Make a 2–5 min landscape video instead of a Short")
            p.add_argument("--both", action="store_true", help="Make both a Short and a long video")
            p.add_argument("--platforms", help="Comma-separated: youtube,instagram,facebook,tiktok_export")
        if name == "from-script":
            p.add_argument("file", help="UTF-8 text file with the script (≤115 words → Short, else Long)")
            p.add_argument("--platforms", help="Comma-separated: youtube,instagram,facebook,tiktok_export")
    return parser


def main(argv: list[str] | None = None) -> int:
    textshape.ensure()                      # Arabic shaping (raqm) for rendered text
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO, and those carry API keys in the query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    if args.command == "state":                  # must not hold the DB open while it's replaced
        return args.func(cfg, None, args)
    conn = db.connect(cfg.db_path)
    try:
        db.init_db(conn)
        before = conn.total_changes
        try:
            return args.func(cfg, conn, args)
        finally:
            # Any command that changed the DB (tick, finalize, pause, …) tells the Actions job to save the state.
            if conn.total_changes != before and not getattr(args, "dry_run", False):
                (cfg.root / "data" / ".changed").touch()
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
