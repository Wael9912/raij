"""Phase 20: real source media, music pool, cover card."""
import io
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from PIL import Image

from src import db
from src.assemble import brand, music, render, runner, sourcemedia
from src.config import load_config
from src.discover.common import Candidate, upsert_candidates
from src.extract import media
from src.extract import runner as extract_runner
from src.review import cards
from tests.test_assemble import BEATS, SPANS, WORDS, FakeFFmpeg

PAGE = """<html><head>
<meta property="og:image" content="https://cdn.example/lead.jpg?w=1200&crop=1" />
<meta property="og:image:width" content="1200" /><meta property="og:image:alt" content="The new glasses" />
<meta property="og:video:url" content="https://cdn.example/promo.mp4" />
<script type="application/ld+json">{"@type":"NewsArticle","image":{"@type":"ImageObject","url":"https://cdn.example/ld.jpg"},
 "video":{"@type":"VideoObject","contentUrl":"https://cdn.example/ld.mp4","embedUrl":"https://www.youtube.com/embed/abcdefghijk"}}</script>
</head><body><article>
<img alt="Jane Doe" sizes="125px" srcset="https://cdn.example/jane.jpg?w=48 48w, https://cdn.example/jane.jpg?w=2400 2400w" src="https://cdn.example/jane.jpg?w=2400"/>
<img alt="Hero again" srcset="https://cdn.example/lead.jpg?w=376 376w, https://cdn.example/lead.jpg?w=2048 2048w" src="https://cdn.example/lead.jpg?w=2048"/>
<img alt="Inside the lab" src="/images/lab.jpg" width="1024" height="683"/>
<img alt="tiny" src="https://cdn.example/pixel.gif" width="1" height="1"/>
<img alt="logo" src="https://cdn.example/site-logo.png"/>
<img class="author-avatar" src="https://cdn.example/ava.jpg"/>
<iframe src="https://www.youtube-nocookie.com/embed/zyxwvutsrqp?rel=0"></iframe>
<video src="https://cdn.example/clip.webm"></video>
<img alt="chart" src="https://cdn.example/chart.svg"/>
</article></body></html>"""


def test_page_media_orders_videos_lead_then_pictures_and_skips_decoration():
    items = media.page_media(PAGE, "https://news.example/story")
    kinds = [(i["kind"], i["url"]) for i in items]
    assert kinds[:5] == [("video", "https://cdn.example/promo.mp4"), ("video", "https://cdn.example/clip.webm"),
                         ("video", "https://cdn.example/ld.mp4"),
                         ("youtube", "https://www.youtube.com/watch?v=zyxwvutsrqp"),
                         ("youtube", "https://www.youtube.com/watch?v=abcdefghijk")]
    images = [i for i in items if i["kind"] == "image"]
    assert [i["url"] for i in images] == ["https://cdn.example/lead.jpg?w=1200&crop=1", "https://cdn.example/ld.jpg",
                                          "https://news.example/images/lab.jpg"]
    assert images[0]["lead"] and images[0]["alt"] == "The new glasses" and images[0]["w"] == 1200
    assert images[2]["alt"] == "Inside the lab" and all(i["source"] == "news.example" for i in items)


def test_yt_search_keeps_relevant_lengths_most_viewed_first():
    lines = [
        {"id": "a" * 11, "title": "Meta Connect 2026: everything revealed", "duration": 600, "view_count": 500},
        {"id": "b" * 11, "title": "Meta Connect 2026 in 3 minutes", "duration": 180, "view_count": 9000},
        {"id": "c" * 11, "title": "Cat compilation", "duration": 200, "view_count": 999999},
        {"id": "d" * 11, "title": "Meta Connect keynote full", "duration": 5000, "view_count": 100},
        {"id": "e" * 11, "title": "Meta Connect short", "duration": 5, "view_count": 100},
    ]

    def run(cmd):
        assert cmd[:2] == ["yt-dlp", "--flat-playlist"] and cmd[-1].startswith("ytsearch6:")
        return subprocess.CompletedProcess(cmd, 0, "\n".join(json.dumps(x) for x in lines), "")

    hits = media.yt_search("Meta Connect 2026", run=run)
    assert [h["url"][-11:] for h in hits] == ["b" * 11, "a" * 11]
    assert hits[0]["kind"] == "youtube" and hits[0]["duration"] == 180 and hits[0]["search"] is True


def test_collect_reads_pages_then_search_and_caps():
    cfg = load_config()
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=PAGE)

    def run(cmd):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": "s" * 11, "title": "Story 1 explained",
                                                               "duration": 120, "view_count": 5}), "")
    row = {"id": 1, "source": "rss", "canonical_url": "https://news.example/story", "title": "Story 1",
           "raw_json": "{}"}
    items = media.collect(cfg, httpx.Client(transport=httpx.MockTransport(handler)), row,
                          ["https://news.example/story"], run=run)
    assert calls == ["https://news.example/story"]                         # the canonical page once, not twice
    assert items[0]["kind"] == "video" and items[-1]["url"].endswith("s" * 11) and items[-1]["search"]
    assert len(items) <= cfg.get("media.max_items")


def test_search_query_prefers_the_trend_headline():
    row = {"source": "trends", "title": "حمار", "raw_json": json.dumps({"news": [{"title": "First onager born in Saudi"}]})}
    assert media.search_query(row) == "First onager born in Saudi"
    assert media.search_query({"source": "rss", "title": "Muse glasses", "raw_json": "{}"}) == "Muse glasses"


# --- extract stores it -------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY"):
        monkeypatch.setenv(key, "")
    cfg = load_config()
    cfg.root = tmp_path
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


def test_extract_records_source_media(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    from tests.test_extract import CARD, _gemini
    article = PAGE.replace("<article>", "<article>" + "".join(
        f"<p>Paragraph {i}: the auction in Paris sold nearly every lot, raising more than 950,000 euros.</p>"
        for i in range(8)))
    upsert_candidates(conn, [Candidate(source="rss", external_id="e1", canonical_url="https://news.example/story",
                                       title="Story 1", raw={"feed": "Sky", "summary": "s"})])
    conn.execute("UPDATE candidates SET status = 'selected', selected_at = datetime('now'), topic = 't'")
    conn.commit()

    def handler(request):
        if "generativelanguage" in request.url.host:
            return _gemini(CARD)
        return httpx.Response(200, text=article)

    def run(cmd):
        return subprocess.CompletedProcess(cmd, 0, "", "")            # yt-dlp search: nothing found
    assert extract_runner.extract(cfg, conn, client=httpx.Client(transport=httpx.MockTransport(handler)), run=run,
                                  out_dir=tmp) == 0
    stored = json.loads(conn.execute("SELECT media FROM stories").fetchone()[0])
    assert stored[0]["kind"] == "video" and any(i["kind"] == "image" and i.get("lead") for i in stored)


# --- assemble uses it --------------------------------------------------------------

def _jpeg(w, h, color=(200, 80, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


class FakeTools(FakeFFmpeg):
    """ffmpeg/ffprobe/yt-dlp stand-in: probes answer 1280×720 × 60 s, downloads and cuts create files."""

    def __call__(self, cmd):
        self.cmds.append(cmd)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"streams": [{"width": 1280, "height": 720}],
                                                                   "format": {"duration": "60"}}), "")
        if cmd[0] == "yt-dlp":
            out = Path(cmd[cmd.index("-o") + 1].replace("%(ext)s", "mp4"))
            out.write_bytes(b"yt")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _media_client(downloads):
    def handler(request):
        url = str(request.url)
        downloads.append(url)
        if request.url.host == "api.pexels.com":
            return httpx.Response(200, json={"videos": [
                {"id": 7, "url": "https://www.pexels.com/video/7/", "duration": 9, "user": {"name": "A"},
                 "video_files": [{"link": "https://cdn.example/7.mp4", "width": 1080, "height": 1920}]},
                {"id": 8, "url": "https://www.pexels.com/video/8/", "duration": 9, "user": {"name": "A"},
                 "video_files": [{"link": "https://cdn.example/8.mp4", "width": 1080, "height": 1920}]}]})
        if url.endswith("lead.jpg"):
            return httpx.Response(200, content=_jpeg(1600, 900), headers={"content-type": "image/jpeg"})
        if url.endswith("small.jpg"):
            return httpx.Response(200, content=_jpeg(300, 200), headers={"content-type": "image/jpeg"})
        if url.endswith("page.html"):
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"mp4data")
    return httpx.Client(transport=httpx.MockTransport(handler))


MEDIA = [{"kind": "image", "url": "https://cdn.example/lead.jpg", "page": "https://news.example/s", "source": "news.example",
          "lead": True},
         {"kind": "image", "url": "https://cdn.example/small.jpg", "page": "https://news.example/s", "source": "news.example"},
         {"kind": "image", "url": "https://cdn.example/page.html", "page": "https://news.example/s", "source": "news.example"},
         {"kind": "youtube", "url": "https://www.youtube.com/watch?v=abcdefghijk", "page": "https://www.youtube.com/watch?v=abcdefghijk",
          "source": "youtube.com", "title": "Explainer"}]


def test_prepare_frames_pictures_cuts_clips_and_caches(env):
    cfg, _, tmp = env
    downloads, tools = [], FakeTools()
    items = sourcemedia.prepare(cfg, _media_client(downloads), MEDIA, 5, (1080, 1920), tmp / "work", run=tools)
    assert [(i.kind, i.still) for i in items] == [("image", True), ("youtube", False), ("youtube", False), ("youtube", False)]
    assert items[0].path.startswith("assets/source/5/frame_1080x1920_") and (tmp / items[0].path).exists()
    assert (tmp / items[0].extra["photo"]).exists()
    with Image.open(tmp / items[0].path) as framed:
        assert framed.size == (1080, 1920)
    assert all(i.path.startswith("assets/source/5/yt_") for i in items[1:]) and items[1].credit == "Source: youtube.com"
    cuts = [c for c in tools.cmds if c[0] != "ffprobe" and c[0] != "yt-dlp"]
    assert len(cuts) == 3 and "-ss" in cuts[0] and cuts[0][cuts[0].index("-ss") + 1] == "12.00"   # 20 % of 60 s
    graph = cuts[0][cuts[0].index("-filter_complex") + 1]
    assert "boxblur" in graph and "overlay" in graph and "credit_" in " ".join(cuts[0])   # 16:9 in a Short: blur fill
    assert not list((tmp / "work").glob("yt_*.mp4"))                                     # full download deleted
    # Second call: everything cached, nothing fetched or cut again.
    n_dl, n_cmd = len(downloads), len(tools.cmds)
    again = sourcemedia.prepare(cfg, _media_client(downloads), MEDIA, 5, (1080, 1920), tmp / "work", run=tools)
    assert [i.path for i in again] == [i.path for i in items]
    assert len(downloads) == n_dl and all(c[0] == "ffprobe" for c in tools.cmds[n_cmd:])


def test_cut_command_fills_when_orientation_matches():
    cfg = load_config()
    cmd = sourcemedia.cut_command(cfg, "in.mp4", Path("out.mp4"), 3.0, 10.0, (1920, 1080), (1280, 720), None)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "force_original_aspect_ratio=increase,crop=1920:1080" in graph and "boxblur" not in graph and "-an" in cmd


def test_assign_real_spreads_over_beats_hook_first():
    items = [f"r{i}" for i in range(4)]
    assert runner.assign_real(items, [2, 1, 2], reuse=0) == [["r0", "r3"], ["r1"], ["r2"]]
    assert runner.assign_real([], [2, 1]) == [[], []]
    assert runner.assign_real(items, [1, 1], reuse=0) == [["r0"], ["r1"]]
    # Pool of 2 over 3 beats × 2 slots: a second pass repeats items, never twice in one beat, then stock.
    assert runner.assign_real(["a", "b"], [2, 2, 2], reuse=1) == [["a", "b"], ["b"], ["a"]]
    assert runner.assign_real(["a"], [2, 2], reuse=1) == [["a"], ["a"]]


def _voiced_with_media(cfg, conn, media_items):
    upsert_candidates(conn, [Candidate(source="rss", external_id="e1", canonical_url="https://x.example/1")])
    conn.execute("UPDATE candidates SET category = 'tech'")
    conn.execute("INSERT INTO stories (candidate_id, media) VALUES (1, ?)", (json.dumps(media_items),))
    conn.execute("INSERT INTO scripts (story_id, brand_id, body_ar, beats, status, notes) VALUES (1, 'raij', 'x', ?, 'passed', ?)",
                 (json.dumps(BEATS, ensure_ascii=False), json.dumps({"hook_title": "نظارات ميتا الجديدة"}, ensure_ascii=False)))
    voice = cfg.root / "assets/generated/voice"
    voice.mkdir(parents=True)
    (voice / "1.wav").write_bytes(b"RIFF")
    (voice / "1.words.json").write_text(json.dumps({"duration": 2.6, "words": WORDS, "beats": SPANS}))
    conn.execute("INSERT INTO videos (script_id, voice_path, duration_s, status, notes) "
                 "VALUES (1, 'assets/generated/voice/1.wav', 2.6, 'voiced', '{}')")
    conn.commit()


def _pool(tmp, tracks):
    (tmp / "assets/music").mkdir(parents=True, exist_ok=True)
    for t in tracks:
        (tmp / "assets/music" / t["file"]).write_bytes(b"mp3")
    (tmp / "assets/music/pool.json").write_text(json.dumps(tracks))


def test_assemble_puts_real_media_first_with_cover_music_and_credits(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    _voiced_with_media(cfg, conn, MEDIA[:1] + MEDIA[3:])
    _pool(tmp, [{"file": "a.mp3", "title": "Cipher", "artist": "Kevin MacLeod", "site": "incompetech.com",
                 "license": "CC BY 4.0", "moods": ["tech"]},
                {"file": "b.mp3", "title": "Lasting Hope", "artist": "Kevin MacLeod", "license": "CC BY 4.0", "moods": ["calm"]}])
    tools = FakeTools()
    assert runner.assemble(cfg, conn, client=_media_client([]), run=tools) == 0
    row = dict(conn.execute("SELECT * FROM videos").fetchone())
    assert row["status"] == "rendered"
    manifest = json.loads(row["broll_manifest"])
    # Beat 0 (hook, 1 slot): the lead picture. Beat 1 (1 slot): a source clip. No stock needed at all.
    assert [(m["provider"], m["beat"]) for m in manifest] == [("source", 0), ("source", 1)]
    assert manifest[0]["still"] and manifest[0]["credit"] == "Source: news.example" and not manifest[1]["still"]
    notes = json.loads(row["notes"])
    assert notes["media"] == {"real": 2, "photos": 1, "clips": 1, "stock": 0, "found": 2, "domains": ["news.example", "youtube.com"]}
    assert notes["music"] == "assets/music/a.mp3"                      # tech → Cipher, never the calm track
    assert "Music: Cipher by Kevin MacLeod (incompetech.com), CC BY 4.0" in notes["credits"]
    assert "Media: news.example, youtube.com" in notes["credits"]
    render_cmd = " ".join(tools.cmds[-1])
    assert "frame_1080x1920_" in render_cmd and "yt_" in render_cmd and "cover.jpg" in render_cmd
    assert "adelay=800" in render_cmd and "volume=0.28" in render_cmd and "hook.txt" in render_cmd
    with Image.open(tmp / notes["cover"]) as cover:
        assert cover.size == (1080, 1920)
    # The review caption says what the picture is made of.
    ctx = {**row, "notes": row["notes"], "kind": "short", "hashtags": "[]", "sources": "[]", "beats": "[]",
           "script_notes": "{}", "brand_id": "raij", "title": "Story", "wanted": None}
    line = cards.media_line(notes)
    assert line.startswith("🖼 1 source photo · 🎞 1 source clip") and "🎵 Cipher by Kevin MacLeod" in line
    assert cards.media_line({"media": {"real": 0, "stock": 3}}).startswith("⚠️ no source media")


def test_assemble_backfills_media_for_older_stories(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    _voiced_with_media(cfg, conn, [])
    conn.execute("UPDATE stories SET media = NULL, sources = ?", (json.dumps(["https://news.example/s"]),))
    conn.commit()
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "news.example":
            return httpx.Response(200, text='<html><head><meta property="og:image" content="https://cdn.example/lead.jpg"></head></html>')
        return _media_client([]).get(str(request.url)) if False else _media_client([]).send(request)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    tools = FakeTools()
    assert runner.assemble(cfg, conn, client=client, run=tools) == 0
    stored = json.loads(conn.execute("SELECT media FROM stories").fetchone()[0])
    assert stored and stored[0]["url"] == "https://cdn.example/lead.jpg"
    notes = json.loads(conn.execute("SELECT notes FROM videos").fetchone()[0])
    assert notes["media"]["photos"] == 2                                # the one real picture, reused on beat 2


def test_assemble_falls_back_to_stock_when_media_fails(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    _voiced_with_media(cfg, conn, [{"kind": "image", "url": "https://cdn.example/page.html", "page": "p", "source": "x"}])
    tools = FakeTools()
    assert runner.assemble(cfg, conn, client=_media_client([]), run=tools) == 0
    row = dict(conn.execute("SELECT * FROM videos").fetchone())
    manifest = json.loads(row["broll_manifest"])
    assert all(m["provider"] == "pexels" for m in manifest) and len(manifest) == 2
    notes = json.loads(row["notes"])
    assert notes["media"]["real"] == 0 and notes["media"]["stock"] == 2 and notes["music"] is None


def test_music_pick_moods_rotation_and_loose_fallback(env):
    cfg, _, tmp = env
    assert music.pick(cfg, 1) == (None, None)
    (tmp / "assets/music").mkdir(parents=True)
    (tmp / "assets/music/x.mp3").write_bytes(b"m")
    assert music.pick(cfg, 3) == (Path("assets/music/x.mp3"), None)
    _pool(tmp, [{"file": "t1.mp3", "title": "T1", "artist": "K", "license": "CC BY 4.0", "moods": ["tech"]},
                {"file": "t2.mp3", "title": "T2", "artist": "K", "license": "CC BY 4.0", "moods": ["tech"]},
                {"file": "c.mp3", "title": "C", "artist": "K", "license": "CC BY 4.0", "moods": ["calm"]},
                {"file": "gone.mp3", "title": "G", "artist": "K", "license": "CC BY 4.0", "moods": ["tech"]}])
    (tmp / "assets/music/gone.mp3").unlink()
    picks = {music.pick(cfg, i, "tech")[0].name for i in range(4)}
    assert picks == {"t1.mp3", "t2.mp3"}                                # gone.mp3 skipped, the two rotate
    assert music.pick(cfg, 0, "tech", kind="long")[0].name in {"t1.mp3", "t2.mp3", "c.mp3"}
    assert music.pick(cfg, 0, "money")[0].name == "c.mp3"               # money → calm
    assert music.pick(cfg, 0, "tech")[1] == "Music: T1 by K, CC BY 4.0"


def test_cover_card_variants(tmp_path):
    look = {"id": "raij", "name": "رائج"}
    photo = tmp_path / "hero.jpg"
    Image.new("RGB", (1600, 900), (10, 120, 200)).save(photo)
    out = brand.cover(look, tmp_path / "c.jpg", "عنوان تجريبي طويل بعض الشيء", "هل تعلم؟", photo=photo)
    with Image.open(out) as img:
        assert img.size == (1080, 1920)
    out = brand.cover(look, tmp_path / "c2.jpg", None, None, photo=None, frame=(1280, 720))
    with Image.open(out) as img:
        assert img.size == (1280, 720)


def test_hook_sequence_waits_for_the_cover(tmp_path):
    lst = brand.hook_sequence("عنوان", "هل تعلم؟", tmp_path, 2.0, delay=0.8)
    body = lst.read_text()
    assert body.splitlines()[1:3] == ["file 'hook_blank.png'", "duration 0.800"]


def test_render_segments_start_after_the_lead():
    segs = render.segments_for([{"start": 0.1}, {"start": 1.6}], [[Path("a.mp4")], [Path("b.jpg")]], 2.6, lead=0.8)
    assert [round(s.seconds, 3) for s in segs] == [0.8, 1.0] and segs[1].still
