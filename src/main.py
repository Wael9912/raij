"""Ra'ij CLI. Usage: python -m src.main <command> [--dry-run]"""
from __future__ import annotations

import argparse
import logging
import sys

from src import db
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


def _not_implemented(name: str):
    def run(cfg, conn, args) -> int:
        log.warning("'%s' is not implemented yet%s", name, " (dry run)" if args.dry_run else "")
        return 0
    return run


def cmd_discover(cfg, conn, args) -> int:
    from src.discover.runner import discover
    return discover(cfg, conn, only=getattr(args, "source", None), dry_run=args.dry_run)


def cmd_rank(cfg, conn, args) -> int:
    from src.rank.runner import rank
    return rank(cfg, conn, dry_run=args.dry_run)


def cmd_extract(cfg, conn, args) -> int:
    from src.extract.runner import extract
    return extract(cfg, conn, dry_run=args.dry_run)


HANDLERS = {name: _not_implemented(name) for name in STAGES}
HANDLERS["discover"] = cmd_discover
HANDLERS["rank"] = cmd_rank
HANDLERS["extract"] = cmd_extract


def cmd_init_db(cfg, conn, args) -> int:
    if args.dry_run:
        log.info("Would initialize schema at %s", cfg.db_path)
        return 0
    db.init_db(conn)
    log.info("Database ready at %s", cfg.db_path)
    return 0


def cmd_run_daily(cfg, conn, args) -> int:
    """Chain all stages; one stage failing must not kill the rest."""
    failures = []
    for name in STAGES:
        if name == "publish" and db.publishing_paused(conn):
            log.warning("Publishing is paused (kill switch) — skipping publish")
            continue
        try:
            HANDLERS[name](cfg, conn, args)
        except Exception:
            log.exception("Stage '%s' failed; continuing", name)
            failures.append(name)
    return 1 if failures else 0


def cmd_pause(cfg, conn, args) -> int:
    db.set_flag(conn, "publishing_paused", "1")
    log.info("Publishing paused")
    return 0


def cmd_resume(cfg, conn, args) -> int:
    db.set_flag(conn, "publishing_paused", "0")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO, and those carry API keys in the query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)
    try:
        db.init_db(conn)
        return args.func(cfg, conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
