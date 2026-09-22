import json
import subprocess
from pathlib import Path

import pytest

from src import db
from src.config import load_config
from src.discover.common import Candidate, upsert_candidates
from src.voice import runner, tts
from src.voice.tts import Word

BEATS = [{"role": "hook", "text": "هل سمعت بالخبر؟", "broll_keywords": ["x"]},
         {"role": "body", "text": "جمع المزاد 953,531 يورو في Giants.com", "broll_keywords": ["x"]},
         {"role": "cta", "text": "اكتب رأيك", "broll_keywords": ["x"]}]
WORDS = [Word("هل", 0.1, 0.3), Word("سمعت", 0.3, 0.6), Word("بالخبر", 0.6, 1.0),
         Word("جمع", 1.4, 1.7), Word("المزاد", 1.7, 2.1), Word("953,531", 2.1, 5.5), Word("يورو", 5.5, 5.8),
         Word("في", 5.8, 5.9), Word("Giants", 5.9, 6.3), Word("com", 6.3, 6.6),
         Word("اكتب", 7.0, 7.3), Word("رأيك", 7.3, 7.7)]
LOUD = '{"input_i": "-22.1", "input_tp": "-6.0", "input_lra": "3.2", "input_thresh": "-32.4", ' \
       '"target_offset": "0.1", "output_i": "-14.0", "output_tp": "-1.6"}'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    cfg = load_config()
    cfg.root = tmp_path                                 # outputs land in tmp/assets/generated/voice
    cfg.data["brands"][0]["voice"] = {"name": "ar-EG-ShakirNeural", "rate": "+0%", "pitch": "+0Hz"}
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


def _script(conn):
    upsert_candidates(conn, [Candidate(source="rss", external_id="e1", canonical_url="https://x.example/1")])
    conn.execute("INSERT INTO stories (candidate_id, hook) VALUES (1, 'h')")
    conn.execute("INSERT INTO scripts (story_id, brand_id, body_ar, beats, status) VALUES (1, 'raij', ?, ?, 'passed')",
                 ("\n".join(b["text"] for b in BEATS), json.dumps(BEATS, ensure_ascii=False)))
    conn.commit()


class FakeAudio:
    """Stands in for edge-tts + ffmpeg/ffprobe; the raw clip lasts durations[i] on synthesis i."""

    def __init__(self, durations):
        self.durations, self.rates, self.last = list(durations), [], 0.0

    def synth(self, text, voice, rate, pitch, out):
        self.rates.append(rate)
        self.last = self.durations.pop(0)
        out.write_bytes(b"mp3")
        return WORDS

    def run(self, cmd):
        if cmd[0].endswith("ffprobe"):
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{self.last}\n", stderr="")
        if cmd[-1] != "-":
            Path(cmd[-1]).write_bytes(b"RIFF")          # second loudnorm pass writes the wav
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=f"[Parsed_loudnorm]\n{LOUD}\n")


def test_speech_text_one_sentence_per_beat():
    assert tts.speech_text(BEATS) == "هل سمعت بالخبر؟\nجمع المزاد 953,531 يورو في Giants.com.\nاكتب رأيك."


def test_beat_spans_align_despite_different_tokenization():
    spans = tts.beat_spans(BEATS, WORDS)
    assert [(s["role"], s["start"], s["end"]) for s in spans] == [
        ("hook", 0.1, 1.0), ("body", 1.4, 6.6), ("cta", 7.0, 7.7)]


def test_faster_rate():
    assert tts.faster_rate("+0%", 63, 58) == "+11%"
    assert tts.faster_rate("-5%", 60, 58) == "+1%"
    assert tts.faster_rate("+0%", 80, 58) is None


def test_loudnorm_second_pass_uses_measurements():
    f = tts.loudnorm_filter(-14, json.loads(LOUD))
    assert "measured_I=-22.1" in f and "offset=0.1" in f and "linear=true" in f


def test_voice_end_to_end(env):
    cfg, conn, tmp = env
    _script(conn)
    fake = FakeAudio([48.2])
    assert runner.voice(cfg, conn, synth=fake.synth, run=fake.run) == 0
    row = dict(conn.execute("SELECT * FROM videos").fetchone())
    assert row["status"] == "voiced" and row["voice_path"] == "assets/generated/voice/1.wav"
    assert row["duration_s"] == 48.2 and json.loads(row["notes"])["lufs"] == "-14.0"
    timings = json.loads((tmp / "assets/generated/voice/1.words.json").read_text())
    assert timings["beats"][1] == {"role": "body", "start": 1.4, "end": 6.6} and len(timings["words"]) == 12
    assert (tmp / "assets/generated/voice/1.wav").exists()
    assert not list(tmp.glob("assets/generated/voice/*.mp3"))      # raw TTS stays in a temp dir

    assert runner.voice(cfg, conn, synth=fake.synth, run=fake.run) == 0      # idempotent
    assert conn.execute("SELECT count(*) FROM videos").fetchone()[0] == 1


def test_too_long_is_resynthesized_faster(env):
    cfg, conn, _ = env
    _script(conn)
    fake = FakeAudio([63.0, 56.5, 56.5])
    assert runner.voice(cfg, conn, synth=fake.synth, run=fake.run) == 0
    assert fake.rates == ["+0%", "+11%"]
    assert json.loads(conn.execute("SELECT notes FROM videos").fetchone()[0])["rate"] == "+11%"


def test_still_too_long_fails(env):
    cfg, conn, _ = env
    _script(conn)
    fake = FakeAudio([63.0, 59.0])
    assert runner.voice(cfg, conn, synth=fake.synth, run=fake.run) == 1
    row = conn.execute("SELECT status, notes FROM videos").fetchone()
    assert row["status"] == "failed" and "even at +11%" in row["notes"]


def test_short_clip_kept_with_warning(env):
    cfg, conn, _ = env
    _script(conn)
    fake = FakeAudio([31.0])
    assert runner.voice(cfg, conn, synth=fake.synth, run=fake.run) == 0
    assert "short" in json.loads(conn.execute("SELECT notes FROM videos").fetchone()[0])["warning"]


def test_network_failure_writes_nothing(env):
    cfg, conn, _ = env
    _script(conn)

    def down(*a):
        raise tts.VoiceError("edge-tts failed: ClientConnectorError")

    assert runner.voice(cfg, conn, synth=down, run=FakeAudio([]).run) == 1
    assert conn.execute("SELECT count(*) FROM videos").fetchone()[0] == 0


def test_synthesize_retries_then_gives_up(tmp_path, monkeypatch):
    calls = []

    async def flaky(text, voice, rate, pitch, out):
        calls.append(1)
        if len(calls) < 2:
            raise OSError("socket closed")
        out.write_bytes(b"mp3")
        return WORDS

    monkeypatch.setattr(tts, "_synthesize", flaky)
    assert tts.synthesize("t", "v", "+0%", "+0Hz", tmp_path / "a.mp3") == WORDS and len(calls) == 2

    async def dead(*a):
        raise OSError("down")

    monkeypatch.setattr(tts, "_synthesize", dead)
    with pytest.raises(tts.VoiceError, match="OSError"):
        tts.synthesize("t", "v", "+0%", "+0Hz", tmp_path / "b.mp3")
    assert not (tmp_path / "b.mp3").exists()


def test_dry_run_writes_nothing(env, caplog):
    cfg, conn, tmp = env
    _script(conn)
    caplog.set_level("INFO")

    def boom(*a):
        raise AssertionError("synthesis in dry run")

    assert runner.voice(cfg, conn, dry_run=True, synth=boom, run=boom) == 0
    assert "1 passed script" in caplog.text
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert not (tmp / "assets").exists()
