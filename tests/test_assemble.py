import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from src import db
from src.assemble import brand, broll, render, runner, subtitles
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
    words = ["كلمة"] * 6
    lines = r.wrap(words, list(range(6)))
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


def test_subtitles_wait_for_hook_title(tmp_path):
    lst = subtitles.render_sequence(WORDS, SPANS, tmp_path, total=4.6, hide_until=0.6)
    lines = lst.read_text().splitlines()
    assert lines[1:3] == ["file 'blank.png'", "duration 0.600"]          # blank while the title shows
    durations = [float(line.split()[1]) for line in lines if line.startswith("duration")]
    assert abs(sum(durations) - 4.6) < 0.01
    assert len(list(tmp_path.glob("c*.png"))) == 3                    # "هل" (0.1–0.4) is never shown


def test_arabic_needs_real_shaping():
    from src import textshape
    assert textshape.available(), "raqm missing — brew install libraqm"
    r = subtitles.Renderer()
    assert r.glyphs("«كلمة»") == '"كلمة"'                              # logical text; HarfBuzz shapes it


def test_series_badge_by_category_or_script_pick():
    look = {"series": {"tech": "عالم التقنية", "wow-facts": "هل تعلم؟"}}
    assert brand.series_name(look, "tech") == "عالم التقنية"
    assert brand.series_name(look, "tech", "هل تعلم؟") == "هل تعلم؟"
    assert brand.series_name(look, "tech", "made up") == "عالم التقنية"
    assert brand.series_name(look, "sports") is None and brand.series_name({}, "tech") is None


def test_long_hook_title_shrinks_instead_of_losing_words():
    title = "ضغوط دولية تلاحق رئيس الفيفا"
    assert len(brand.wrap(title, 118, 960)) == 3 and " ".join(brand.wrap(title, 118, 960)) == title
    assert brand._title_block(title, None).width <= 1000


def test_hook_sequence_lasts_its_seconds(tmp_path):
    lst = brand.hook_sequence("هل يواجه رئيس الفيفا النهاية؟", "رياضة في دقيقة", tmp_path, 2.5)
    lines = lst.read_text().splitlines()
    durations = [float(line.split()[1]) for line in lines if line.startswith("duration")]
    assert abs(sum(durations) - 2.5 - 1 / 30) < 0.01 and lines[-1] == "file 'hook_blank.png'"
    from PIL import Image
    assert Image.open(tmp_path / "hook_hold.png").size == (1080, 1920)


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
    assert "logo.png" in cmd and "xfade" in cmd and "hook.txt" not in cmd        # no hook title on this script
    assert not (tmp / "assets/generated/video/1").exists()             # subtitle PNGs cleaned up

    assert runner.assemble(cfg, conn, client=_stock_client([]), run=ff) == 0      # idempotent
    assert len(ff.cmds) == 1


def test_hook_title_and_series_from_script(env, monkeypatch):
    cfg, conn, _ = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    _voiced(cfg, conn)
    conn.execute("UPDATE candidates SET category = 'wow-facts'")
    conn.execute("UPDATE scripts SET notes = ?", (json.dumps({"hook_title": "مزاد لا يصدق"}, ensure_ascii=False),))
    conn.commit()
    ff = FakeFFmpeg()
    assert runner.assemble(cfg, conn, client=_stock_client([]), run=ff) == 0
    notes = json.loads(conn.execute("SELECT notes FROM videos").fetchone()[0])
    assert notes["hook_title"] == "مزاد لا يصدق" and notes["series"] == "هل تعلم؟"
    assert "hook.txt" in " ".join(ff.cmds[0])


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
    lst = subtitles.render_sequence(WORDS, SPANS, work, 3.6, hide_until=0.5)
    look = {"id": "raij", "name": "رائج"}
    plan = render.Plan(segs, Path("assets/generated/voice/1.wav"), lst, 1250,
                       brand.endcard(look, work / "e.png", "هل تعلم؟"), 1.0,
                       tmp / "assets/generated/video/1.mp4",
                       hook_list=brand.hook_sequence("عنوان قصير جدا", "هل تعلم؟", work, 0.5),
                       logo=brand.logo_layer(look, work / "logo.png"), transition=0.3)
    out = render.render(cfg, plan)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                       "stream=codec_type,width,height:format=duration", "-of", "json", str(out)],
                                      capture_output=True, text=True, check=True).stdout)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in probe["streams"])
    assert abs(float(probe["format"]["duration"]) - 3.6) < 0.15


# --- clip selection & cache --------------------------------------------------

def test_clips_needed_scales_with_beat_length():
    assert [broll.clips_needed(s) for s in (3, 7, 7.1, 20, 60)] == [1, 1, 2, 3, 4]


def test_choose_prefers_fresh_short_clips_and_skips_long_ones(env, monkeypatch):
    cfg, _, _ = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    vids = [_pexels_video(1, dur=117), _pexels_video(2, dur=40), _pexels_video(3, dur=9), _pexels_video(4, dur=12)]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"videos": vids})))
    picked = broll.choose(cfg, client, ["x"], need=14, used=set(), recent={"pexels:3"})
    assert [c.id for c in picked] == ["4", "2"]          # 2 cuts; 117s skipped; recently used #3 last


def test_prune_stock_keeps_only_recent_clips(env):
    cfg, conn, tmp = env
    _voiced(cfg, conn)
    stock = tmp / "assets/stock"
    stock.mkdir(parents=True)
    for name in ("pexels_1.mp4", "pexels_2.mp4"):
        (stock / name).write_bytes(b"x")
    conn.execute("UPDATE videos SET broll_manifest = ?, status = 'rendered'",
                 (json.dumps([{"provider": "pexels", "id": "1", "path": "assets/stock/pexels_1.mp4"}]),))
    conn.commit()
    assert runner.prune_stock(cfg, conn, keep_days=14) == 1
    assert [p.name for p in stock.glob("*.mp4")] == ["pexels_1.mp4"]


# --- public-figure photos ----------------------------------------------------

from src.assemble import portrait  # noqa: E402


def _wiki(licence="CC BY-SA 4.0", repo="shared", nonfree=None, disambig=False, image="Face.jpg"):
    def handler(request):
        if request.url.host == "upload.wikimedia.org":
            from io import BytesIO
            from PIL import Image
            buf = BytesIO()
            Image.new("RGB", (400, 600), (120, 90, 60)).save(buf, "JPEG")
            return httpx.Response(200, content=buf.getvalue())
        titles = request.url.params["titles"]
        if titles.startswith("File:"):
            meta = {"LicenseShortName": {"value": licence}, "Artist": {"value": "<a href='x'>Jane Doe</a>"}}
            if nonfree:
                meta["NonFree"] = {"value": "true"}
            return httpx.Response(200, json={"query": {"pages": {"-1": {
                "imagerepository": repo, "imageinfo": [{"thumburl": "https://upload.wikimedia.org/f.jpg",
                                                        "descriptionurl": "https://commons.wikimedia.org/f",
                                                        "extmetadata": meta}]}}}})
        page = {"title": titles, "pageprops": {"disambiguation": ""} if disambig else {}}
        if image:
            page["pageimage"] = image
        return httpx.Response(200, json={"query": {"pages": {"1": page}}})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_photo_lookup_accepts_free_commons_licence():
    p = portrait.lookup(_wiki(), "Gianni Infantino")
    assert (p.license, p.author, p.credit) == ("CC BY-SA 4.0", "Jane Doe",
                                               "Photo: Jane Doe / CC BY-SA 4.0 via Wikimedia Commons")


@pytest.mark.parametrize("kwargs", [dict(repo="local"), dict(licence="Fair use"), dict(nonfree=True),
                                    dict(disambig=True), dict(image=None), dict(licence="All rights reserved")])
def test_photo_lookup_refuses_non_free(kwargs):
    assert portrait.lookup(_wiki(**kwargs), "Someone") is None


def test_compose_makes_portrait_frame(tmp_path):
    from PIL import Image
    src = tmp_path / "in.jpg"
    Image.new("RGB", (1600, 900), (10, 200, 10)).save(src)
    out = portrait.compose(src, "Photo: Jane Doe / CC BY 2.0 via Wikimedia Commons", tmp_path / "f.jpg")
    assert Image.open(out).size == (1080, 1920)


def test_xfade_chain_keeps_cut_times_and_length(env):
    cfg, _, tmp = env
    segs = [render.Segment(Path(f"assets/stock/{c}.mp4"), s) for c, s in (("a", 3.0), ("b", 2.5), ("c", 4.0))]
    plan = render.Plan(segs, Path("assets/generated/voice/1.wav"), tmp / "assets/generated/video/1/subs.txt",
                       1250, Path("assets/generated/video/1/e.png"), 2.0, tmp / "o.mp4", transition=0.3)
    cmd = " ".join(render.command(cfg, plan))
    assert "trim=duration=3.3," in cmd and "trim=duration=2.8," in cmd      # each clip runs into the next
    assert ":offset=3.000[x1]" in cmd and ":offset=5.500[x2]" in cmd and "fade:duration=0.3:offset=9.500[bg]" in cmd
    assert "concat=n=" not in cmd and plan.total == 11.5
    assert "color=c=0xFFD400" in cmd                                        # progress bar
    hard = render.command(cfg, render.Plan(segs, plan.voice, plan.subs_list, 1250, plan.endcard, 2.0, plan.out,
                                           transition=0, progress_bar=False))
    assert "concat=n=4" in " ".join(hard) and "xfade=" not in " ".join(hard)


def test_still_segments_get_zoom_filter(env):
    cfg, _, tmp = env
    segs = render.segments_for(SPANS, [[Path("assets/stock/wikimedia_x.jpg")], [Path("assets/stock/a.mp4")]], 2.6)
    assert [s.still for s in segs] == [True, False]
    plan = render.Plan(segs, Path("assets/generated/voice/1.wav"), tmp / "assets/generated/video/1/subs.txt",
                       1250, Path("assets/generated/video/1/e.png"), 2.0, tmp / "o.mp4")
    cmd = " ".join(render.command(cfg, plan))
    assert "zoompan" in cmd and "-loop 1 -framerate 30" in cmd


def test_person_beat_opens_on_licensed_photo(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    _voiced(cfg, conn)
    beats = [dict(BEATS[0], person="Gianni Infantino"), BEATS[1]]
    conn.execute("UPDATE scripts SET beats = ?", (json.dumps(beats, ensure_ascii=False),))
    conn.commit()
    wiki, stock = _wiki(), _stock_client([])

    def handler(request):
        host = request.url.host
        return (wiki if "wiki" in host else stock)._transport.handle_request(request)

    ff = FakeFFmpeg()
    assert runner.assemble(cfg, conn, client=httpx.Client(transport=httpx.MockTransport(handler)), run=ff) == 0
    row = conn.execute("SELECT broll_manifest, notes FROM videos").fetchone()
    manifest = json.loads(row["broll_manifest"])
    assert manifest[0]["provider"] == "wikimedia" and manifest[0]["beat"] == 0
    assert json.loads(row["notes"])["credits"] == ["Photo: Jane Doe / CC BY-SA 4.0 via Wikimedia Commons"]
    assert "photo_0.jpg" in " ".join(ff.cmds[0])


# --- faceless check ----------------------------------------------------------

def test_choose_skips_clips_with_faces_and_tries_next_keyword(env, monkeypatch):
    cfg, _, _ = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    queries = []

    def handler(request):
        q = request.url.params["query"]
        queries.append(q)
        ids = [1, 2] if q == "office" else [3]
        return httpx.Response(200, json={"videos": [_pexels_video(i) for i in ids]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    faceless = lambda _c, clip: clip.id == "3"                      # noqa: E731  clips 1, 2 show faces
    picked = broll.choose(cfg, client, ["office", "desk lamp"], need=5, used=set(), faceless=faceless)
    assert [c.id for c in picked] == ["3"] and queries == ["office", "desk lamp"]
    with pytest.raises(broll.BrollError, match="faceless"):
        broll.choose(cfg, client, ["office"], need=5, used=set(), faceless=lambda _c, _clip: False)


def test_pexels_previews_sampled_from_video_pictures():
    v = {**_pexels_video(9), "video_pictures": [{"picture": f"https://img/{i}.jpg"} for i in range(8)]}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"videos": [v]})))
    assert broll.search_pexels(client, "k", "x")[0].previews == ["https://img/2.jpg", "https://img/4.jpg",
                                                                "https://img/6.jpg"]


def test_face_detector_sees_no_face_in_plain_scene():
    import numpy as np
    from src.assemble import faces
    scene = np.full((720, 405, 3), (40, 120, 200), np.uint8)
    assert faces.face_ratio(scene) == 0.0
