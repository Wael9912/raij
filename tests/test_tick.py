"""`tick` (GitHub Actions pass): the state-changed marker, the tap drain loop, dry-run, and the finalize command."""
import argparse
import json

import httpx
import pytest

from src import db, main
from src.config import load_config
from src.publish import runner as pub
from src.review import runner as review_runner
from tests.test_review import CHAT, TOKEN, FakeTelegram, _msg


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    cfg = load_config()
    cfg.root = tmp_path
    (tmp_path / "data").mkdir()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


def _args(dry_run=False):
    return argparse.Namespace(dry_run=dry_run)


def _changed(tmp):
    return (tmp / "data" / ".changed").exists()


def _wire(monkeypatch, tg, publish=None, daily=None):
    monkeypatch.setattr(review_runner, "make_bot", lambda cfg: (tg.bot(), CHAT))
    monkeypatch.setattr(pub, "publish", publish or (lambda cfg, conn, dry_run=False: 0))
    monkeypatch.setattr(main, "cmd_run_daily", daily or (lambda cfg, conn, args: 0))
    monkeypatch.setattr(main, "daily_due", lambda cfg, conn, now=None: None)


def test_idle_tick_writes_nothing(env, monkeypatch):
    cfg, conn, tmp = env
    tg = FakeTelegram()
    _wire(monkeypatch, tg)
    assert main.cmd_tick(cfg, conn, _args()) == 0
    assert _changed(tmp) and "setMyCommands" in tg.methods()      # first tick after a deploy: slash menu once (U8)
    (tmp / "data" / ".changed").unlink()
    assert main.cmd_tick(cfg, conn, _args()) == 0
    assert not _changed(tmp) and tg.methods().count("setMyCommands") == 1


def test_taps_are_drained_and_marked(env, monkeypatch):
    cfg, conn, tmp = env
    tg = FakeTelegram(updates=[{"update_id": 1, **_msg("/pause")}])
    _wire(monkeypatch, tg)
    assert main.cmd_tick(cfg, conn, _args()) == 0
    assert db.publishing_paused(conn) and _changed(tmp)
    assert tg.methods().count("getUpdates") == 2           # one batch, then the empty one that ends the drain


def test_changed_marker_survives_a_crash(env, monkeypatch):
    """A1: the daily run is marked started, then publish blows up — the DB changed, so the caller must save it."""
    cfg, conn, tmp = env

    def boom(cfg, conn, dry_run=False):
        raise RuntimeError("upload exploded")
    _wire(monkeypatch, FakeTelegram(), publish=boom)
    monkeypatch.setattr(main, "daily_due", lambda cfg, conn, now=None: "2026-09-23")
    with pytest.raises(RuntimeError):
        main.cmd_tick(cfg, conn, _args())
    assert db.get_flag(conn, "last_daily_run") == "2026-09-23" and _changed(tmp)


def test_telegram_outage_still_publishes(env, monkeypatch):
    cfg, conn, tmp = env
    calls = []
    _wire(monkeypatch, FakeTelegram(), publish=lambda cfg, conn, dry_run=False: calls.append(1) or 0)
    monkeypatch.setattr(review_runner, "make_bot", lambda cfg: (_ for _ in ()).throw(RuntimeError("down")))
    assert main.cmd_tick(cfg, conn, _args()) == 1
    assert calls == [1] and not _changed(tmp)


def test_dry_run_touches_nothing(env, monkeypatch, caplog):
    cfg, conn, tmp = env
    caplog.set_level("INFO")
    _wire(monkeypatch, FakeTelegram())
    monkeypatch.setattr(main, "daily_due", lambda cfg, conn, now=None: "2026-09-23")
    assert main.cmd_tick(cfg, conn, _args(dry_run=True)) == 0
    assert "daily run is due" in caplog.text
    assert db.get_flag(conn, "last_daily_run") is None and not _changed(tmp)


def test_resume_clears_the_pause_notice(env):
    cfg, conn, _ = env
    db.set_flag(conn, "paused_notice_sent", "1")
    assert main.cmd_pause(cfg, conn, _args()) == 0 and db.get_flag(conn, "paused_notice_sent") == "1"
    assert main.cmd_resume(cfg, conn, _args()) == 0
    assert db.get_flag(conn, "paused_notice_sent") == "0" and not db.publishing_paused(conn)


def test_finalize_command_dry_run_then_real(env, monkeypatch, caplog):
    cfg, conn, _ = env
    caplog.set_level("INFO")
    monkeypatch.setattr(pub, "finalize", lambda cfg, conn, max_age_hours, dry_run=False, platforms=None:
                        [(20, "published")] if max_age_hours is None else [])
    assert main.cmd_finalize(cfg, conn, _args(dry_run=True)) == 0
    assert "[dry run] video 20 → published" in caplog.text
    assert main.cmd_finalize(cfg, conn, _args()) == 0
    assert "1 approved video(s) closed" in caplog.text


def test_any_writing_command_sets_the_changed_marker(tmp_path, monkeypatch):
    """The Actions job saves state only when data/.changed exists — `finalize`/`pause` must set it, not only tick."""
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "data" / "t.db"))
    monkeypatch.setattr(main, "load_config", lambda path=None: _rooted(tmp_path))
    assert main.main(["pause"]) == 0
    assert (tmp_path / "data" / ".changed").exists()
    (tmp_path / "data" / ".changed").unlink()
    assert main.main(["publish", "--dry-run"]) == 0
    assert not (tmp_path / "data" / ".changed").exists()


def _rooted(root):
    cfg = load_config()
    cfg.root = root
    return cfg
