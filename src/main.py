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
    """Long-running: handle review buttons and /pause /resume /status from Telegram."""
    from src.review.bot import poll
    from src.review.runner import make_bot
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
    log.info("Review bot listening — Ctrl+C to stop")
    try:
        poll(cfg, conn, bot, chat)
    except KeyboardInterrupt:
        log.info("Review bot stopped")
    return 0


def cmd_publish(cfg, conn, args) -> int:
    from src.publish.runner import publish
    return publish(cfg, conn, dry_run=args.dry_run)


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
    from src.review.bot import ensure_commands, poll
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
                while poll(cfg, conn, bot, chat, once=True):  # drain every queued tap
                    pass
                remind(cfg, conn, bot, chat)                   # 48 h / 72 h nudges for waiting cards (U5)
            except Exception as exc:
                log.error("Telegram pass failed: %s", exc)
                code = 1
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


def cmd_run_daily(cfg, conn, args) -> int:
    """Chain all stages; one stage failing must not kill the rest. Failures are sent to Telegram."""
    from src.lock import Busy, single
    try:
        with single(cfg.root, "run-daily"):
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
        try:
            from src.review.runner import make_bot
            bot, chat = make_bot(cfg)
            bot.send_message(chat, f"⚠️ Daily run: {', '.join(failures)} had problems — {log_hint()}")
        except Exception as exc:
            log.warning("Couldn't send the failure notice: %s", exc)
    return 1 if failures else 0


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
        "bot": ("Listen for Telegram review decisions (long-running)", cmd_bot),
        "youtube-auth": ("One-time Google consent for YouTube uploads", cmd_youtube_auth),
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
        if name == "discover":
            p.add_argument("--source", action="append", choices=["youtube", "reddit", "trends", "rss"],
                           help="Only run this source (repeatable)")
        if name == "state":
            p.add_argument("action", choices=["pack", "unpack"])
            p.add_argument("file", help="Encrypted bundle path")
            p.add_argument("--db-only", action="store_true", help="Pack the database only (no media)")
        if name == "report":
            p.add_argument("--weekly", action="store_true", help="Send the weekly report to Telegram now")
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
