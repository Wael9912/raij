import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from src import db
from src.assemble import broll, render, runner, subtitles
from src.config import load_config
from src.discover.common import Candidate, upsert_candidates

BEATS = [{"role": "hook", "text": "هل سمعت", "broll_keywords": ["auction hammer", "crowd"]},
         {"role": "cta", "text": "اكتب رأيك", "broll_keywords": ["phone typing"]}]
WORDS = [{"text": "هل", "start": 0.1, "end": 0.4}, {"text": "سمعت", "start": 0.4, "end": 0.9},
         {"text": "اكتب", "start": 1.6, "end": 2.0}, {"text": "رأيك", "start": 2.0, "end": 2.5}]
SPANS = [{"role": "hook", "start": 0.1, "end": 0.9}, {"role": "cta", "start": 1.6, "end": 2.5}]
HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    for key in ("PEXELS_API_KEY", "PIXABAY_API_KEY"):
        monkeypatch.setenv(key, "")
    cfg = load_config()
    cfg.root = tmp_path
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


def _voiced(cfg, conn):
    upsert_candidates(conn, [Candidate(source="rss", external_id="e1", canonical_url="https://x.example/1")])
    conn.execute("INSERT INTO stories (candidate_id) VALUES (1)")
    conn.execute("INSERT INTO scripts (story_id, brand_id, body_ar, beats, status) VALUES (1, 'raij', 'x', ?, 'passed')",
                 (json.dumps(BEATS, ensure_ascii=False),))
    voice = cfg.root / "assets/generated/voice"
    voice.mkdir(parents=True)
    (voice / "1.wav").write_bytes(b"RIFF")
    (voice / "1.words.json").write_text(json.dumps({"duration": 2.6, "words": WORDS, "beats": SPANS}))
    conn.execute("INSERT INTO videos (script_id, voice_path, duration_s, status, notes) "
                 "VALUES (1, 'assets/generated/voice/1.wav', 2.6, 'voiced', '{\"rate\": \"+10%\"}')")
    conn.commit()


def _pexels_video(vid, w=1080, h=1920, dur=12):
    return {"id": vid, "url": f"https://www.pexels.com/video/{vid}/", "duration": dur, "user": {"name": "Ann"},
            "video_files": [{"link": f"https://cdn.example/{vid}_4k.mp4", "width": w * 2, "height": h * 2},
                            {"link": f"https://cdn.example/{vid}_hd.mp4", "width": w, "height": h},
                            {"link": f"https://cdn.example/{vid}_sd.mp4", "width": w // 2, "height": h // 2}]}


def _stock_client(downloads):
    def handler(request):
        if request.url.host == "api.pexels.com":
            q = request.url.params["query"]
            return httpx.Response(200, json={"videos": [_pexels_video(101 if "hammer" in q else 202),
                                                        _pexels_video(303, 1920, 1080)]})
        downloads.append(str(request.url))
        return httpx.Response(200, content=b"mp4data")
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- subtitles ---------------------------------------------------------------

def test_visual_order_keeps_ltr_runs_left_to_right():
    r = subtitles.Renderer()
    words = ["زار", "New", "York", "في", "2025"]
    assert [words[i] for i in r.visual_order(words, [0, 1, 2, 3, 4])] == ["2025", "في", "New", "York", "زار"]


def test_wrap_balances_two_lines():
    r = subtitles.Renderer()
    words = ["كلمة"] * 9
    lines = r.wrap(words, list(range(9)))
    assert len(lines) == 2 and abs(len(lines[0]) - len(lines[1])) <= 1


def test_cues_break_at_beats_and_pauses():
    cues = subtitles.make_cues(WORDS, SPANS, subtitles.Renderer())
    assert [(c.start, c.end, c.lines) for c in cues] == [(0.1, 0.9, [[0, 1]]), (1.6, 2.5, [[2, 3]])]


def test_render_sequence_covers_whole_video(tmp_path):
    lst = subtitles.render_sequence(WORDS, SPANS, tmp_path, total=4.6)
    body = lst.read_text()
    durations = [float(line.split()[1]) for line in body.splitlines() if line.startswith("duration")]
    assert abs(sum(durations) - 4.6) < 0.01
    assert body.splitlines()[-1] == "file 'blank.png'"
    assert len(list(tmp_path.glob("c*.png"))) == 4                    # one frame per highlighted word


def test_srt():
    out = subtitles.srt(WORDS, SPANS)
    assert out.startswith("1\n00:00:00,100 --> 00:00:00,900\nهل سمعت\n") and "2\n00:00:01,600" in out


# --- guardrail ---------------------------------------------------------------

def test_guard_allows_only_asset_dirs(env):
    cfg, _, tmp = env
    (tmp / "assets/stock").mkdir(parents=True)
    (tmp / "assets/stock/a.mp4").write_bytes(b"x")
    assert render.guard(cfg, Path("assets/stock/a.mp4")) == (tmp / "assets/stock/a.mp4").resolve()
    for bad in (Path("data/source.mp4"), tmp / "downloads/x.mp4", Path("assets/stock/../../etc/passwd")):
        with pytest.raises(render.GuardrailError):
            render.guard(cfg, bad)
    (tmp / "elsewhere.mp4").write_bytes(b"x")
    (tmp / "assets/stock/link.mp4").symlink_to(tmp / "elsewhere.mp4")
    with pytest.raises(render.GuardrailError):
        render.guard(cfg, Path("assets/stock/link.mp4"))                 # symlink escaping the dir


def test_command_refuses_source_video(env):
    cfg, _, tmp = env
    plan = render.Plan([render.Segment(Path("data/yt_source.mp4"), 2.0)], Path("assets/generated/voice/1.wav"),
                       tmp / "assets/generated/video/1/subs.txt", 1250, Path("assets/generated/video/1/e.png"),
                       2.0, tmp / "out.mp4")
    with pytest.raises(render.GuardrailError):
        render.command(cfg, plan)


# --- b-roll ------------------------------------------------------------------

def test_pick_file_prefers_smallest_full_hd():
    assert broll._pick_file(_pexels_video(1)["video_files"])["link"].endswith("_hd.mp4")


def test_choose_prefers_portrait_and_skips_used(env, monkeypatch):
    cfg, _, _ = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    client = _stock_client([])
    used = {"pexels:101"}
    picked = broll.choose(cfg, client, ["auction hammer"], need=30, used=used)
    assert [(c.id, c.portrait) for c in picked] == [("303", False)]
    assert "pexels:303" in used


def test_choose_without_key_explains(env):
    cfg, _, _ = env
    with pytest.raises(broll.BrollError, match="PEXELS_API_KEY"):
        broll.choose(cfg, httpx.Client(), ["x"], need=1, used=set())


def test_download_is_cached(env, monkeypatch):
    cfg, _, tmp = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    downloads = []
    client = _stock_client(downloads)
    clip = broll.search_pexels(client, "k", "auction hammer")[0]
    broll.download(cfg, client, clip)
    broll.download(cfg, client, clip)
    assert downloads == ["https://cdn.example/101_hd.mp4"] and clip.path == "assets/stock/pexels_101.mp4"


def test_segments_cover_voice():
    segs = render.segments_for(SPANS, [[Path("a")], [Path("b"), Path("c")]], 2.6)
    assert [(str(s.clip), s.seconds) for s in segs] == [("a", 1.6), ("b", 0.5), ("c", 0.5)]


# --- stage -------------------------------------------------------------------

class FakeFFmpeg:
    def __init__(self):
        self.cmds = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_assemble_end_to_end(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    _voiced(cfg, conn)
    (tmp / "assets/music").mkdir(parents=True)
    (tmp / "assets/music/calm.mp3").write_bytes(b"x")
    ff = FakeFFmpeg()
    assert runner.assemble(cfg, conn, client=_stock_client([]), run=ff) == 0
    row = dict(conn.execute("SELECT * FROM videos").fetchone())
    assert row["status"] == "rendered" and row["video_path"] == "assets/generated/video/1.mp4"
    assert (tmp / row["video_path"]).exists() and (tmp / row["subtitle_path"]).read_text().startswith("1\n")
    manifest = json.loads(row["broll_manifest"])
    assert [(m["id"], m["beat"], m["license"]) for m in manifest] == [("101", 0, "Pexels License"),
                                                                      ("202", 1, "Pexels License")]
    notes = json.loads(row["notes"])
    assert notes["rate"] == "+10%" and notes["music"] == "assets/music/calm.mp3"
    assert row["duration_s"] == 4.6                                    # 2.6s voice + 2s end card
    cmd = " ".join(ff.cmds[0])
    assert "sidechaincompress" in cmd and "pexels_101.mp4" in cmd and "subs.txt" in cmd
    assert not (tmp / "assets/generated/video/1").exists()             # subtitle PNGs cleaned up

    assert runner.assemble(cfg, conn, client=_stock_client([]), run=ff) == 0      # idempotent
    assert len(ff.cmds) == 1


def test_no_stock_key_changes_nothing(env):
    cfg, conn, _ = env
    _voiced(cfg, conn)
    assert runner.assemble(cfg, conn, client=httpx.Client(), run=FakeFFmpeg()) == 1
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == "voiced"
    assert "no stock footage key" in conn.execute("SELECT notes FROM runs").fetchone()[0]


def test_ffmpeg_failure_retries_next_run(env, monkeypatch):
    cfg, conn, _ = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    _voiced(cfg, conn)
    fail = lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "Invalid data")      # noqa: E731
    assert runner.assemble(cfg, conn, client=_stock_client([]), run=fail) == 1
    assert conn.execute("SELECT status FROM videos").fetchone()[0] == "voiced"


def test_dry_run(env, caplog):
    cfg, conn, tmp = env
    _voiced(cfg, conn)
    caplog.set_level("INFO")
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("side effect in dry run"))  # noqa: E731
    assert runner.assemble(cfg, conn, dry_run=True, client=httpx.Client(transport=httpx.MockTransport(boom)),
                           run=boom) == 0
    assert "NONE" in caplog.text and not (tmp / "assets/stock").exists()
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


# --- real render snapshot ----------------------------------------------------

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_real_render_snapshot(env):
    """A tiny real ffmpeg render: portrait output, right length, audio present."""
    cfg, _, tmp = env
    stock, voice, work = tmp / "assets/stock", tmp / "assets/generated/voice", tmp / "assets/generated/video/1"
    for d in (stock, voice, work):
        d.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                    "-t", "1", "-pix_fmt", "yuv420p", str(stock / "land.mp4")], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2.6",
                    "-ar", "48000", "-ac", "1", str(voice / "1.wav")], check=True)
    segs = render.segments_for(SPANS, [[Path("assets/stock/land.mp4")]] * 2, 2.6)
    lst = subtitles.render_sequence(WORDS, SPANS, work, 3.6)
    plan = render.Plan(segs, Path("assets/generated/voice/1.wav"), lst, 1250,
                       render.endcard({"id": "raij", "name": "رائج"}, work / "e.png"), 1.0,
                       tmp / "assets/generated/video/1.mp4")
    out = render.render(cfg, plan)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                       "stream=codec_type,width,height:format=duration", "-of", "json", str(out)],
                                      capture_output=True, text=True, check=True).stdout)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in probe["streams"])
    assert abs(float(probe["format"]["duration"]) - 3.6) < 0.15
