"""Performance audit 2026-09-24: a sleeping Mac, an unreachable service or a cut-off TTS stream must not cost
the day's picks — keep-awake, outage-aware retries, the truncated-voice guard, same-day catch-up, and stale
`running` rows."""
import argparse
import json
import subprocess
import time

import httpx
import pytest

from src import db, main, power
from src.config import load_config
from src.discover.common import Candidate, upsert_candidates
from src.extract import runner as extract_runner
from src.llm import LLMError
from src.voice import runner as voice_runner, tts
from tests.test_extract import ARTICLE_HTML, _no_cmd, _rss_cand, _select
from tests.test_voice import BEATS, WORDS, FakeAudio, _script


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    cfg = load_config()
    cfg.root = tmp_path
    (tmp_path / "data").mkdir()
    cfg.data["brands"][0]["voice"] = {"name": "ar-EG-ShakirNeural", "rate": "+0%", "pitch": "+0Hz"}
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


def _args(**kw):
    kw.setdefault("dry_run", False)
    return argparse.Namespace(**kw)


# --- LLMError.outage: a provider that is down or throttled is not the item's fault -------------------------

def test_outage_is_only_when_every_provider_was_unreachable():
    down = LLMError("x", ["gemini: POST …: ConnectError", "groq: GROQ_API_KEY not set", "ollama: POST …: ConnectError"])
    assert down.outage
    throttled = LLMError("x", ["gemini: POST …: HTTP 503 high demand", "groq: GROQ_API_KEY not set"])
    assert throttled.outage
    blocked = LLMError("x", ["gemini: Gemini flash returned no candidates (blocked or empty)", "groq: not set"])
    assert not blocked.outage
    garbled = LLMError("x", ["gemini: unparseable output: Expecting value"])
    assert not garbled.outage
    assert not LLMError("x").outage                                     # no detail → treat as the item's fault


def test_extract_outage_does_not_count_an_attempt(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    cfg.data["pipeline"] = {"max_attempts": 3, "max_age_days": 2}
    _select(conn, [_rss_cand(1)])

    def handler(request):
        if "generativelanguage" in request.url.host or request.url.host == "localhost":
            raise httpx.ConnectError("no route to host")               # the Mac's network was asleep; no Ollama
        return httpx.Response(200, text=ARTICLE_HTML)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    for _ in range(4):                                                  # more than max_attempts
        assert extract_runner.extract(cfg, conn, client=client, run=_no_cmd, out_dir=tmp) == 1
        row = conn.execute("SELECT status, attempts FROM candidates").fetchone()
        assert (row["status"], row["attempts"] or 0) == ("selected", 0)
    notes = json.loads(conn.execute("SELECT notes FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert notes["failed"] == [] and "ConnectError" in notes["retry"][0]["error"]


# --- voice: a cut-off stream is retried, never rendered; an unreachable service doesn't count ---------------

def test_truncated_voice_is_left_for_the_next_run(env):
    cfg, conn, tmp = env
    _script(conn)                                                       # 11 script words
    fake = FakeAudio([12.4])
    fake_words = WORDS[:3]                                              # 3 of 11 words came back (live: 22 of 99)
    fake.synth = lambda text, voice, rate, pitch, out: (out.write_bytes(b"mp3"), fake_words)[1]
    assert voice_runner.voice(cfg, conn, synth=fake.synth, run=fake.run) == 1
    assert conn.execute("SELECT count(*) FROM videos").fetchone()[0] == 0
    assert not list(tmp.glob("assets/generated/voice/*.wav"))
    notes = json.loads(conn.execute("SELECT notes FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert "Truncated" in notes["retry"][0]["error"] and notes["failed"] == []
    script_notes = json.loads(conn.execute("SELECT notes FROM scripts").fetchone()[0] or "{}")
    assert not script_notes.get("attempts")                             # not counted against the script
    fake2 = FakeAudio([48.2])
    assert voice_runner.voice(cfg, conn, synth=fake2.synth, run=fake2.run) == 0   # the next run voices it
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == "voiced"


def test_check_complete_tolerates_tokenizer_differences():
    tts.check_complete(WORDS, 11)                                       # 12 tokens for 11 words: fine
    tts.check_complete(WORDS[:10], 11)                                  # 0.9: fine
    tts.check_complete(WORDS[:2], 5)                                    # tiny scripts are never judged
    with pytest.raises(tts.Truncated):
        tts.check_complete(WORDS[:5], 11)


def test_unreachable_tts_is_classified(tmp_path, monkeypatch):
    class ClientConnectorDNSError(Exception):
        pass

    async def dead(*a):
        raise ClientConnectorDNSError("Cannot connect to host speech.platform.bing.com:443")

    monkeypatch.setattr(tts, "_synthesize", dead)
    with pytest.raises(tts.Unreachable):
        tts.synthesize("t", "v", "+0%", "+0Hz", tmp_path / "b.mp3", sleep=lambda s: None)

    async def refused(*a):
        raise ValueError("bad voice name")

    monkeypatch.setattr(tts, "_synthesize", refused)
    with pytest.raises(tts.VoiceError) as exc:
        tts.synthesize("t", "v", "+0%", "+0Hz", tmp_path / "c.mp3", sleep=lambda s: None)
    assert not isinstance(exc.value, tts.Unreachable)


def test_voice_unreachable_does_not_count_an_attempt(env):
    cfg, conn, _ = env
    _script(conn)

    def down(*a):
        raise tts.Unreachable("edge-tts failed after 4 attempts: ClientConnectorDNSError: …")

    for _ in range(4):
        assert voice_runner.voice(cfg, conn, synth=down, run=FakeAudio([]).run) == 1
    assert conn.execute("SELECT count(*) FROM videos").fetchone()[0] == 0
    assert not json.loads(conn.execute("SELECT notes FROM scripts").fetchone()[0] or "{}").get("attempts")


# --- catch-up: the 30-minute publish pass finishes what an interrupted run left behind ----------------------

def _leftover(conn):
    upsert_candidates(conn, [Candidate(source="rss", external_id="e9", canonical_url="https://x.example/9")])
    conn.execute("UPDATE candidates SET status = 'selected', selected_at = datetime('now')")
    conn.commit()


def test_catch_up_runs_produce_once_per_interval(env, monkeypatch):
    cfg, conn, _ = env
    cfg.data["pipeline"] = {"catch_up_hours": 2}
    calls = []
    monkeypatch.setattr(main, "cmd_produce", lambda cfg, conn, args, what="": calls.append(what) or 0)
    assert main.catch_up(cfg, conn, _args()) == 0 and calls == []      # nothing left behind
    _leftover(conn)
    assert main.leftovers(conn)["extract"] == 1
    assert main.catch_up(cfg, conn, _args()) == 0 and calls == ["Catch-up run"]
    assert main.catch_up(cfg, conn, _args()) == 0 and len(calls) == 1   # within the interval: wait
    db.set_flag(conn, "last_catch_up", str(time.time() - 3 * 3600))
    assert main.catch_up(cfg, conn, _args()) == 0 and len(calls) == 2
    assert main.catch_up(cfg, conn, _args(dry_run=True)) == 0 and len(calls) == 2
    cfg.data["pipeline"] = {"catch_up_hours": 0}
    db.set_flag(conn, "last_catch_up", "0")
    assert main.catch_up(cfg, conn, _args()) == 0 and len(calls) == 2   # switched off


def test_publish_command_catches_up_only_when_asked(env, monkeypatch):
    cfg, conn, _ = env
    from src.publish import runner as pub
    monkeypatch.setattr(pub, "publish", lambda cfg, conn, dry_run=False, now=False, only=None: 0)
    calls = []
    monkeypatch.setattr(main, "catch_up", lambda cfg, conn, args: calls.append(1) or 0)
    assert main.cmd_publish(cfg, conn, argparse.Namespace(dry_run=False)) == 0 and calls == []   # run-daily's args
    assert main.cmd_publish(cfg, conn, _args(catch_up=True)) == 0 and calls == [1]


# --- stale `running` rows and keep-awake ------------------------------------------------------------------

def test_start_run_closes_an_interrupted_run_of_the_same_command(env):
    cfg, conn, _ = env
    old = db.start_run(conn, "script")
    conn.execute("UPDATE runs SET started_at = datetime('now', '-8 hours') WHERE id = ?", (old,))
    fresh = db.start_run(conn, "voice")                                 # another command: untouched
    assert conn.execute("SELECT status FROM runs WHERE id = ?", (old,)).fetchone()[0] == "running"
    new = db.start_run(conn, "script")
    row = conn.execute("SELECT status, notes, finished_at FROM runs WHERE id = ?", (old,)).fetchone()
    assert row["status"] == "failed" and json.loads(row["notes"]) == {"interrupted": 1} and row["finished_at"]
    assert [r[0] for r in conn.execute("SELECT status FROM runs WHERE id IN (?, ?)", (fresh, new))] == ["running"] * 2


def test_stay_awake_holds_caffeinate_only_on_macos(monkeypatch):
    started = []

    class Proc:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            started.append("terminated")

    monkeypatch.setattr(power, "_proc", None)
    monkeypatch.setattr(power.os.path, "exists", lambda p: True)
    monkeypatch.setattr(power.subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or Proc())
    assert power.stay_awake(pid=77, platform="linux") is False and started == []
    assert power.stay_awake(pid=77, platform="darwin") is True
    assert started[0][1:] == ["-i", "-s", "-w", "77"]
    assert power.stay_awake(pid=77, platform="darwin") is False        # one assertion per process
    power.release()
    assert started[-1] == "terminated" and power._proc is None
    power.release()                                                     # idempotent


def test_pipeline_commands_ask_to_stay_awake_but_the_bot_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "data" / "t.db"))
    cfg = load_config()
    cfg.root = tmp_path
    monkeypatch.setattr(main, "load_config", lambda path=None: cfg)
    asked = []
    monkeypatch.setattr(power, "stay_awake", lambda *a, **k: asked.append(1) or True)
    assert main.main(["publish", "--dry-run"]) == 0 and asked == []     # dry runs are quick
    monkeypatch.setattr(main, "cmd_publish", lambda cfg, conn, args: 0)
    assert main.main(["publish"]) == 0 and asked == [1]
    monkeypatch.setattr(main, "cmd_pause", lambda cfg, conn, args: 0)
    assert main.main(["pause"]) == 0 and asked == [1]                  # admin commands don't
    assert "bot" not in main.AWAKE_COMMANDS and "run-daily" in main.AWAKE_COMMANDS


def test_assemble_notes_carry_timing_and_outages_do_not_count(env, monkeypatch):
    from src.assemble import broll, runner as assemble_runner
    assert assemble_runner._outage(broll.BrollUnavailable("pexels down"))
    assert assemble_runner._outage(httpx.ConnectError("x"))
    assert not assemble_runner._outage(RuntimeError("ffmpeg failed"))
    assert not assemble_runner._outage(subprocess.TimeoutExpired("ffmpeg", 900))
