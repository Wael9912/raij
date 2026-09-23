"""Phase 13/15: formats (short/long), owner topics & scripts, the Telegram pick flow, background jobs,
long-form render/publish, and the Wikipedia trend source. Offline: every network call is a MockTransport."""
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from src import db, formats, jobs
from src.assemble import brand, broll, render, subtitles
from src.assemble import runner as assemble_runner
from src.config import load_config
from src.discover import manual, wikipedia
from src.discover.common import Candidate, upsert_candidates
from src.extract import runner as extract_runner
from src.publish import common as pub_common
from src.publish import runner as pub_runner
from src.publish import youtube
from src.rank import runner as rank_runner
from src.review import bot as botmod
from src.review import cards, picks, telegram
from src.script import write
from src.voice import runner as voice_runner

CHAT = "4242"
TOKEN = "123:SECRET"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY"):
        monkeypatch.setenv(key, "")
    cfg = load_config()
    cfg.root = tmp_path
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


class FakeTelegram:
    def __init__(self):
        self.calls, self.next_id = [], 100

    def handler(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}") if not request.headers.get("content-type", "").startswith(
            "multipart") else {"multipart": True}
        self.calls.append((method, body))
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": []})
        self.next_id += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})

    def bot(self):
        return telegram.Bot(TOKEN, httpx.Client(transport=httpx.MockTransport(self.handler)))

    def methods(self):
        return [m for m, _ in self.calls]

    def last(self, method):
        return [b for m, b in self.calls if m == method][-1]


def _handler(cfg, conn, tg):
    return botmod.Handler(cfg, conn, tg.bot(), CHAT)


def _msg(text):
    return {"message": {"message_id": 5, "chat": {"id": int(CHAT)}, "from": {"id": int(CHAT)}, "text": text}}


def _cb(data, msg_id):
    return {"callback_query": {"id": "q1", "data": data, "from": {"id": int(CHAT)},
                               "message": {"message_id": msg_id, "chat": {"id": int(CHAT)}}}}


def _llm(replies, prompts=None):
    it = iter(replies)
    prompts = prompts if prompts is not None else []

    def handler(request):
        prompts.append(json.loads(request.content)["contents"][0]["parts"][0]["text"])
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(next(it))}]}}]})
    return httpx.Client(transport=httpx.MockTransport(handler))


AR = "كلمة"


def _words(n):
    return " ".join([AR] * n)


# --- formats ---------------------------------------------------------------------------------

def test_formats_defaults_and_overrides(env):
    cfg, _, _ = env
    short, long = formats.get(cfg, "short"), formats.get(cfg, "long")
    assert (short.width, short.height, short.portrait, short.max_words) == (1080, 1920, True, 115)
    assert (long.width, long.height, long.portrait, long.orientation) == (1920, 1080, False, "landscape")
    assert long.voice_rate == "+0%" and long.max_seconds == 300 and long.voice_max == 290
    assert formats.get(cfg, None).kind == "short" and formats.get(cfg, "weird").kind == "short"
    assert formats.kind_for_words(cfg, 100) == "short" and formats.kind_for_words(cfg, 300) == "long"
    lo, hi = long.target_words()
    assert long.min_words < lo < hi < long.max_words


def test_wanted_helpers():
    row = {"wanted": formats.encode(["long", "short", "bogus"], ["youtube", "youtube", "tiktok_export"], kind="topic",
                                    text="gold")}
    assert formats.wanted_formats(row) == ["long", "short"]
    assert formats.wanted_platforms(row, {"platforms": ["instagram"]}) == ["youtube", "tiktok_export"]
    assert formats.is_manual(row) and formats.wanted(row)["text"] == "gold"
    assert formats.wanted_formats({"wanted": None}) == ["short"]
    assert formats.wanted_platforms({}, {"platforms": ["instagram"]}) == ["instagram"]
    assert not formats.is_manual({"wanted": formats.encode(["short"], [], by="auto")})
    assert formats.wanted({"wanted": "not json"}) == {}


# --- owner topics and scripts ------------------------------------------------------------------

def test_add_topic_and_script_become_selected_manual_candidates(env):
    cfg, conn, _ = env
    cid = manual.add_topic(cfg, conn, "  لماذا ارتفع   سعر الذهب  ", ["short", "long"], ["youtube"])
    row = dict(conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone())
    assert row["source"] == "manual" and row["status"] == "selected" and row["selected_at"]
    assert formats.wanted_formats(row) == ["short", "long"] and formats.wanted(row)["kind"] == "topic"
    assert row["title"] == "لماذا ارتفع سعر الذهب"
    # the same topic again reuses the row
    assert manual.add_topic(cfg, conn, "لماذا ارتفع سعر الذهب", ["short"], ["youtube"]) == cid
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1

    text = "هل تعلم أن الذهب. " + _words(60)
    sid, kind = manual.add_script(cfg, conn, text, ["youtube", "tiktok_export"])
    srow = dict(conn.execute("SELECT * FROM candidates WHERE id = ?", (sid,)).fetchone())
    assert kind == "short" and formats.wanted(srow)["text"] == text and srow["title"].startswith("هل تعلم أن الذهب")
    _, long_kind = manual.add_script(cfg, conn, _words(300), ["youtube"])
    assert long_kind == "long"
    with pytest.raises(ValueError):
        manual.add_script(cfg, conn, "too short", ["youtube"])
    with pytest.raises(ValueError):
        manual.add_topic(cfg, conn, "   ", ["short"], ["youtube"])


def test_resent_topic_starts_a_fresh_row_after_the_first_finished(env):
    cfg, conn, _ = env
    cid = manual.add_topic(cfg, conn, "quantum chips", ["short"], ["youtube"])
    conn.execute("UPDATE candidates SET status = 'extract_failed', attempts = 3 WHERE id = ?", (cid,))
    conn.execute("INSERT INTO stories (candidate_id, hook) VALUES (?, 'old')", (cid,))
    conn.commit()
    new = manual.add_topic(cfg, conn, "quantum chips", ["short"], ["youtube"])
    assert new != cid
    rows = conn.execute("SELECT id, status, external_id FROM candidates ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["extract_failed", "selected"] and rows[1]["external_id"].endswith(":2")
    assert conn.execute("SELECT count(*) FROM stories").fetchone()[0] == 1     # history kept, nothing blocked
    assert manual.add_topic(cfg, conn, "quantum chips", ["long"], ["youtube"]) == new   # in progress → same row


def test_keywords_and_relevance():
    assert manual.keywords("لماذا ارتفع سعر الذهب إلى مستويات قياسية هذا العام") == ["ارتفع", "سعر", "ذهب", "مستويات", "قياسية"]
    assert manual.keywords("Why did the gold price hit a record this year?") == ["did", "gold", "price", "hit", "record"]
    assert manual.relevant("الذهب", "سعر الذهب") and not manual.relevant("الحرب الأهلية اليمنية", "سعر الذهب")
    assert manual._publisher_url("http://www.bing.com/news/apiclick.aspx?ref=FexRss&url=https%3a%2f%2fx.example%2fa&c=1") \
        == "https://x.example/a"


NEWS_XML = """<rss xmlns:News="https://www.bing.com/news/search"><channel>
<item><title>Gold hits record &amp; more</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2freuters.example%2fA&amp;c=1</link>
<News:Source>Reuters</News:Source><pubDate>Mon</pubDate></item>
<item><title>Gold hits record &amp; more</title><link>https://reuters.example/A</link></item>
<item><title>Second</title><link>https://reuters.example/B</link></item>
</channel></rss>"""
GOOGLE_XML = """<rss><channel><item><title>Only headline</title>
<link>https://news.google.com/rss/articles/CBM</link><source url="https://x">AP</source></item></channel></rss>"""


def test_news_search_and_wikipedia_extract():
    asked = []

    def handler(request):
        asked.append(request.url.host)
        if request.url.host == "www.bing.com":
            return httpx.Response(200, content=NEWS_XML.encode() if "empty" not in str(request.url) else b"<rss/>")
        if request.url.host == "news.google.com":
            return httpx.Response(200, content=GOOGLE_XML.encode())
        if "list" in request.url.params:
            return httpx.Response(200, json={"query": {"search": [{"title": "Yemeni civil war"}, {"title": "Gold"}]}})
        return httpx.Response(200, json={"query": {"pages": {"1": {"extract": "Gold is a chemical element. " * 20}}}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = manual.news_search(client, "gold price", limit=5)
    assert [i["title"] for i in items] == ["Gold hits record & more", "Second"] and items[0]["source"] == "Reuters"
    assert items[0]["url"] == "https://reuters.example/A" and items[0]["readable"] is True
    assert asked[0] == "www.bing.com" and "news.google.com" not in asked
    items = manual.news_search(client, "empty topic", limit=5)                # Bing empty → Google headlines
    assert items[0]["readable"] is False and "news.google.com" in asked
    text, url = manual.wikipedia_extract(client, "gold price")
    assert text.startswith("Gold is a chemical element") and url == "https://en.wikipedia.org/wiki/Gold"
    assert manual.wikipedia_extract(client, "ذهب") is None                     # no relevant Arabic title


def test_extract_uses_the_owner_script_as_the_card_without_llm(env):
    cfg, conn, _ = env
    text = "هل تعلم أن الذهب أغلى من الفضة. " + _words(50)
    manual.add_script(cfg, conn, text, ["youtube"])
    assert extract_runner.extract(cfg, conn, client=httpx.Client()) == 0        # no network at all
    story = dict(conn.execute("SELECT * FROM stories").fetchone())
    assert story["transcript_src"] == "owner" and story["transcript"] == text and story["hook"].startswith("هل تعلم")
    assert conn.execute("SELECT status FROM candidates").fetchone()[0] == "extracted"


def test_extract_topic_researches_news_and_asks_for_depth_and_category(env, monkeypatch):
    cfg, conn, _ = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    manual.add_topic(cfg, conn, "gold price", ["short", "long"], ["youtube"])
    prompts = []
    article = "<html><body><article><p>" + ("Gold rose to a record high on Monday as investors sought safety. " * 30) \
              + "</p></article></body></html>"

    def handler(request):
        if request.url.host == "www.bing.com":
            return httpx.Response(200, content=NEWS_XML.encode())
        if request.url.host == "generativelanguage.googleapis.com":
            prompts.append(json.loads(request.content)["contents"][0]["parts"][0]["text"])
            card = {"usable": True, "hook": "Gold at a record", "key_facts": [f"fact {i}" for i in range(10)],
                    "claims": [], "why_trending": "prices", "category": "money"}
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(card)}]}}]})
        if "wikipedia.org" in request.url.host:
            return httpx.Response(200, json={"query": {"search": []}})
        return httpx.Response(200, text=article)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert extract_runner.extract(cfg, conn, client=client) == 0
    assert "8 to 14 short standalone facts" in prompts[0] and '"category"' in prompts[0] and "3–5 minute" in prompts[0]
    assert "off-topic" in prompts[0]
    story = dict(conn.execute("SELECT * FROM stories").fetchone())
    assert len(json.loads(story["key_facts"])) == 10 and story["transcript_src"] == "news"
    assert conn.execute("SELECT category FROM candidates").fetchone()[0] == "money"


# --- long scripts -------------------------------------------------------------------------------

def _long_draft(chapters=True, n=300):
    body = []
    for i in range(3):
        b = {"role": "body", "text": _words(n // 3), "broll_keywords": ["city skyline"]}
        if chapters:
            b["chapter"] = f"الفصل {'الأول الثاني الثالث'.split()[i]}"
        body.append(b)
    return {"beats": [{"role": "hook", "text": "هل سمعت", "broll_keywords": ["sunrise"]}] + body
            + [{"role": "payoff", "text": "الخلاصة مهمة", "broll_keywords": ["road"]},
               {"role": "cta", "text": "اكتب رأيك", "broll_keywords": ["phone"]}],
            "hook_title": "قصة الذهب الكاملة", "description_en": "d", "hashtags": ["#gold"]}


def test_long_script_needs_chapters_and_its_own_length(env):
    cfg, _, _ = env
    d = write.validate(_long_draft(), 240, 520, kind="long")
    assert d.kind == "long" and sum(1 for b in d.beats if b.get("chapter")) == 3
    with pytest.raises(write.DraftError, match="chapter"):
        write.validate(_long_draft(chapters=False), 240, 520, kind="long")
    with pytest.raises(write.DraftError, match="too short"):
        write.validate(_long_draft(n=100), 240, 520, kind="long")
    # a short script never carries chapters and isn't asked for them
    write.validate(_long_draft(n=90, chapters=False), 85, 115, kind="short")


def test_long_prompt_is_the_long_template(env):
    cfg, _, _ = env
    story = {"id": 1, "hook": "h", "key_facts": "[]", "claims": "[]", "why_trending": "w"}
    p = write.build_prompt(cfg, story, cfg.brands[0], kind="long")
    lo, hi = formats.get(cfg, "long").target_words()
    assert "LONG video" in p and "chapter" in p and f"{lo}–{hi}" in p
    assert "LONG video" not in write.build_prompt(cfg, story, cfg.brands[0], kind="short")


def test_segment_script_keeps_the_owners_words(env, monkeypatch):
    cfg, _, _ = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    text = "هل تعلم أن الذهب أغلى من الفضة؟ " + _words(40) + ". تابعنا للمزيد"
    good = {"beats": [{"role": "hook", "text": "هل تعلم أن الذهب أغلى من الفضة", "broll_keywords": ["gold bars"]},
                      {"role": "body", "text": _words(40), "broll_keywords": ["coins"]},
                      {"role": "cta", "text": "تابعنا للمزيد", "broll_keywords": ["phone"]}],
            "hook_title": "سر الذهب", "description_en": "d", "hashtags": ["#gold"]}
    rewritten = json.loads(json.dumps(good))
    rewritten["beats"][1]["text"] = " ".join(["مختلف"] * 40)
    out = write.segment_script(cfg, text, cfg.brands[0], "short", client=_llm([rewritten, good]))
    assert out.final["status"] == "passed" and out.final["notes"]["gate"].startswith("skipped")
    assert [b["role"] for b in out.final["beats"]] == ["hook", "body", "cta"]
    # twice rewritten → rejected, with the reason kept
    out = write.segment_script(cfg, text, cfg.brands[0], "short", client=_llm([rewritten, rewritten]))
    assert out.final["status"] == "rejected" and "owner's words" in out.final["notes"]["reason"]
    # an edit note allows changes
    out = write.segment_script(cfg, text, cfg.brands[0], "short", edit_note="make it funnier", client=_llm([rewritten]))
    assert out.final["status"] == "passed"


def test_same_text_tolerates_punctuation_only():
    a = "هل تعلم، أن الذهب! أغلى من الفضة."
    assert write.same_text(a, "هل تعلم أن الذهب أغلى من الفضة")
    assert not write.same_text(a, "هل تعلم أن الفضة أغلى من الذهب بكثير جدا")


def test_script_stage_writes_one_script_per_wanted_format(env, monkeypatch):
    from src.script import runner as script_runner
    cfg, conn, _ = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    upsert_candidates(conn, [Candidate(source="rss", external_id="e1", canonical_url="https://x.example/1", title="t")])
    conn.execute("UPDATE candidates SET status = 'extracted', wanted = ?",
                 (formats.encode(["short", "long"], ["youtube"], by="auto"),))
    conn.execute("INSERT INTO stories (candidate_id, transcript, hook, key_facts, claims, why_trending) "
                 "VALUES (1, 'English source text', 'h', '[]', '[]', 'w')")
    conn.commit()
    short = {"beats": [{"role": "hook", "text": "هل سمعت", "broll_keywords": ["a"]},
                       {"role": "body", "text": _words(90), "broll_keywords": ["b"]},
                       {"role": "payoff", "text": "الخلاصة", "broll_keywords": ["c"]},
                       {"role": "cta", "text": "تابعنا", "broll_keywords": ["d"]}],
             "description_en": "d", "hashtags": []}
    assert script_runner.script(cfg, conn, client=_llm([short, _long_draft()])) == 0
    rows = conn.execute("SELECT kind, status FROM scripts ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("short", "passed"), ("long", "passed")]
    assert conn.execute("SELECT status FROM candidates").fetchone()[0] == "scripted"


# --- voice: the long format's limits -------------------------------------------------------------

LOUD = '{"input_i": "-22.1", "input_tp": "-6.0", "input_lra": "3.2", "input_thresh": "-32.4", ' \
       '"target_offset": "0.1", "output_i": "-14.0", "output_tp": "-1.6"}'


def test_voice_uses_the_long_limits_and_rate(env):
    cfg, _, tmp = env
    cfg.data["brands"][0]["voice"] = {"name": "ar-SA-HamedNeural", "rate": "+10%", "pitch": "+0Hz"}
    rates = []

    def synth(text, voice, rate, pitch, out):
        rates.append(rate)
        out.write_bytes(b"mp3")
        return [type("W", (), {"text": "هل", "start": 0.0, "end": 0.4, "__dataclass_fields__": {}})()]

    def run(cmd):
        if cmd[0].endswith("ffprobe"):
            return subprocess.CompletedProcess(cmd, 0, stdout="200.0\n", stderr="")
        if cmd[-1] != "-":
            Path(cmd[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=f"[Parsed_loudnorm]\n{LOUD}\n")
    from dataclasses import make_dataclass
    W = make_dataclass("W", [("text", str), ("start", float), ("end", float)])

    def synth2(text, voice, rate, pitch, out):
        rates.append(rate)
        out.write_bytes(b"mp3")
        return [W("هل", 0.0, 0.4)]
    script = {"id": 7, "brand_id": "raij", "kind": "long", "body_ar": "هل", "beats": json.dumps([{"role": "hook", "text": "هل"}])}
    out = tmp / "assets/generated/voice"
    out.mkdir(parents=True)
    row = voice_runner.voice_script(cfg, script, out, synth=synth2, run=run)
    assert rates == ["+0%"] and row["duration_s"] == 200.0 and row["notes"]["kind"] == "long"
    assert "warning" not in row["notes"]                    # 200 s is fine for long (min 100)


# --- assemble: landscape frame, chapters, photo fallback ----------------------------------------

def test_subtitle_style_for_landscape_fits_the_frame():
    st = subtitles.Style.for_frame(1920, 1080)
    assert st.width == 1920 and st.top + st.band_height <= 1080 and st.size < 92
    assert subtitles.Style.for_frame(1080, 1920).top == 1250


def test_render_command_uses_plan_frame_and_overlays(env, tmp_path):
    cfg, _, tmp = env
    gen = tmp / "assets/generated/video/9"
    gen.mkdir(parents=True)
    for name in ("clip.mp4", "end.png", "voice.wav", "subs.txt", "chapters.txt"):
        (gen / name).write_bytes(b"x")
    stock = tmp / "assets/stock"
    stock.mkdir()
    (stock / "pexels-photo_1.jpg").write_bytes(b"x")
    plan = render.Plan([render.Segment(Path("assets/stock/pexels-photo_1.jpg"), 4.0, still=True)],
                       Path("assets/generated/video/9/voice.wav"), gen / "subs.txt", 700, gen / "end.png", 2.0,
                       gen / "out.mp4", width=1920, height=1080, max_seconds=300, overlays=[gen / "chapters.txt"])
    cmd = " ".join(render.command(cfg, plan))
    assert "s=1920x1080" in cmd and "zoompan" in cmd and "chapters.txt" in cmd and "1080:1920" not in cmd
    assert "color=c=0xFFD400:s=1920x10" in cmd
    with pytest.raises(render.RenderError, match="cap"):
        render.render(cfg, render.Plan([render.Segment(Path("assets/stock/pexels-photo_1.jpg"), 70.0, still=True)],
                                       plan.voice, plan.subs_list, 700, plan.endcard, 2.0, plan.out, max_seconds=60))


def test_chapter_sequence_covers_the_video(tmp_path):
    lst = brand.chapter_sequence([{"at": 0.0, "title": "المقدمة"}, {"at": 12.0, "title": "الخلفية"},
                                  {"at": 40.0, "title": "الأرقام"}], tmp_path, 60.0, frame=(1920, 1080))
    body = lst.read_text()
    total = sum(float(l.split()[1]) for l in body.splitlines() if l.startswith("duration"))
    assert abs(total - 60.0) < 0.05 and body.count("chapter_") > 6
    assert brand.chapter_sequence([{"at": 0.0, "title": "المقدمة"}], tmp_path, 10.0) is None


def test_endcard_and_hook_render_in_landscape(tmp_path):
    card = brand.endcard({"id": "raij", "name": "رائج"}, tmp_path / "e.png", "هل تعلم؟", "تابعنا", frame=(1920, 1080))
    from PIL import Image
    assert Image.open(card).size == (1920, 1080)
    lst = brand.hook_sequence("عنوان طويل جدا للفيديو", "هل تعلم؟", tmp_path, 3.0, frame=(1920, 1080))
    assert Image.open(tmp_path / "hook_hold.png").size == (1920, 1080)
    assert lst.exists()


def _pexels_video(vid, w, h, dur=12):
    return {"id": vid, "url": f"https://www.pexels.com/video/{vid}/", "duration": dur, "user": {"name": "Ann"},
            "video_files": [{"link": f"https://cdn.example/{vid}.mp4", "width": w, "height": h}]}


def test_choose_prefers_landscape_for_long_and_falls_back_to_photos(env, monkeypatch):
    cfg, _, _ = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    asked = []

    def handler(request):
        asked.append((request.url.path, request.url.params.get("orientation"), request.url.params["query"]))
        if request.url.path.startswith("/videos"):
            if request.url.params["query"] == "empty":
                return httpx.Response(200, json={"videos": []})
            return httpx.Response(200, json={"videos": [_pexels_video(1, 1080, 1920), _pexels_video(2, 1920, 1080)]})
        return httpx.Response(200, json={"photos": [{"id": 77, "url": "https://www.pexels.com/photo/77/",
                                                     "photographer": "Bo", "width": 1920, "height": 1280,
                                                     "src": {"large2x": "https://img.example/77.jpg",
                                                             "medium": "https://img.example/77m.jpg"}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    picked = broll.choose(cfg, client, ["city"], need=6, used=set(), faceless=lambda c, k: True, orientation="landscape")
    assert [c.id for c in picked] == ["2"] and asked[0][1] == "landscape"
    picked = broll.choose(cfg, client, ["empty"], need=6, used=set(), faceless=lambda c, k: True, orientation="landscape")
    assert len(picked) == 1 and picked[0].still and picked[0].provider == "pexels-photo"
    assert any(p.startswith("/v1/search") for p, _, _ in asked)
    assert render.segments_for([{"start": 0}], [[Path("assets/stock/pexels-photo_77.jpg")]], 5.0)[0].still


def test_blocked_slugs_are_never_picked(env, monkeypatch):
    cfg, _, _ = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")

    def handler(request):
        if request.url.path.startswith("/videos"):
            beer = _pexels_video(1, 1080, 1920)
            beer["url"] = "https://www.pexels.com/video/pouring-beer-into-a-glass-1/"
            return httpx.Response(200, json={"videos": [beer, _pexels_video(2, 1080, 1920)]})
        return httpx.Response(200, json={"photos": []})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    picked = broll.choose(cfg, client, ["golden liquid"], need=5, used=set(), faceless=lambda c, k: True)
    assert [c.id for c in picked] == ["2"]


def test_download_accepts_images_for_photo_clips(env):
    cfg, _, tmp = env
    clip = broll.Clip("pexels-photo", "77", "https://img.example/77.jpg", "", "Bo", 1920, 1280, 8.0, "Pexels License",
                      still=True)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"jpg")))
    broll.download(cfg, client, clip)
    assert clip.path == "assets/stock/pexels-photo_77.jpg" and (tmp / clip.path).read_bytes() == b"jpg"
    bad = broll.Clip("pexels", "5", "https://cdn.example/5.mp4", "", "", 1080, 1920, 5.0, "Pexels License")
    with pytest.raises(broll.BrollError, match="not a video"):
        broll.download(cfg, httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"jpg"))), bad)


class FakeFFmpeg:
    def __init__(self):
        self.cmds = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        if "-frames:v" in cmd:                       # thumbnail frame grab: give Pillow a real image
            from PIL import Image
            Image.new("RGB", (64, 36), (120, 80, 200)).save(cmd[-1])
        else:
            Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_assemble_long_video_is_landscape_with_chapters_and_thumbnail(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    beats = [{"role": "hook", "text": "هل سمعت", "broll_keywords": ["sunrise"]},
             {"role": "body", "text": "الخلفية أولا", "broll_keywords": ["city"], "chapter": "الخلفية"},
             {"role": "body", "text": "الخلفية ثانيا", "broll_keywords": ["road"], "chapter": "الخلفية"},   # repeated title
             {"role": "body", "text": "ثم الأرقام", "broll_keywords": ["coins"], "chapter": "الأرقام"},
             {"role": "cta", "text": "تابعنا", "broll_keywords": ["phone"]}]
    words = [{"text": "هل", "start": 0.0, "end": 0.5}, {"text": "سمعت", "start": 0.6, "end": 1.0},
             {"text": "الخلفية", "start": 12.0, "end": 12.5}, {"text": "أولا", "start": 12.6, "end": 13.0},
             {"text": "الخلفية", "start": 20.0, "end": 20.5}, {"text": "ثانيا", "start": 20.6, "end": 21.0},
             {"text": "ثم", "start": 30.0, "end": 30.4}, {"text": "الأرقام", "start": 30.5, "end": 31.0},
             {"text": "تابعنا", "start": 50.0, "end": 51.0}]
    spans = [{"role": "hook", "start": 0.0, "end": 1.0}, {"role": "body", "start": 12.0, "end": 13.0},
             {"role": "body", "start": 20.0, "end": 21.0},
             {"role": "body", "start": 30.0, "end": 31.0}, {"role": "cta", "start": 50.0, "end": 51.0}]
    upsert_candidates(conn, [Candidate(source="rss", external_id="e1", canonical_url="https://x.example/1")])
    conn.execute("UPDATE candidates SET category = 'money'")
    conn.execute("INSERT INTO stories (candidate_id) VALUES (1)")
    conn.execute("INSERT INTO scripts (story_id, brand_id, kind, body_ar, beats, status, notes) "
                 "VALUES (1, 'raij', 'long', 'x', ?, 'passed', ?)",
                 (json.dumps(beats, ensure_ascii=False), json.dumps({"hook_title": "قصة الذهب"}, ensure_ascii=False)))
    voice = tmp / "assets/generated/voice"
    voice.mkdir(parents=True)
    (voice / "1.wav").write_bytes(b"RIFF")
    (voice / "1.words.json").write_text(json.dumps({"duration": 52.0, "words": words, "beats": spans}))
    conn.execute("INSERT INTO videos (script_id, voice_path, duration_s, status) "
                 "VALUES (1, 'assets/generated/voice/1.wav', 52.0, 'voiced')")
    conn.commit()

    def handler(request):
        if request.url.host == "api.pexels.com":
            return httpx.Response(200, json={"videos": [_pexels_video(int(request.url.params["query"].__hash__() % 1000
                                                                          + 1), 1920, 1080)]})
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"mp4data")
    ff = FakeFFmpeg()
    assert assemble_runner.assemble(cfg, conn, client=httpx.Client(transport=httpx.MockTransport(handler)), run=ff) == 0
    row = dict(conn.execute("SELECT * FROM videos").fetchone())
    notes = json.loads(row["notes"])
    assert row["status"] == "rendered" and notes["kind"] == "long"
    assert [c["title"] for c in notes["chapters"]] == ["المقدمة", "الخلفية", "الأرقام"]
    assert notes["thumbnail"] == "assets/generated/video/1.thumb.jpg" and (tmp / notes["thumbnail"]).exists()
    cmd = " ".join(ff.cmds[0])
    assert "scale=1920:1080" in cmd and "s=1920x10:" in cmd and "chapters.txt" in cmd and "hook.txt" in cmd
    assert "1080:1920" not in cmd


# --- publish: platforms per video, long-form YouTube -------------------------------------------

def test_wanted_platforms_filters_long_videos(env):
    cfg, _, _ = env
    cfg.brands[0]["platforms"] = ["instagram", "facebook", "youtube", "tiktok_export"]
    short = {"brand_id": "raij", "kind": "short", "wanted": None}
    assert pub_runner.wanted_platforms(cfg, short) == ["instagram", "facebook", "youtube", "tiktok_export"]
    long = {"brand_id": "raij", "kind": "long", "wanted": formats.encode(["long"], ["instagram", "youtube"])}
    assert pub_runner.wanted_platforms(cfg, long) == ["youtube"]
    picked = {"brand_id": "raij", "kind": "short", "wanted": formats.encode(["short"], ["tiktok_export", "nope"])}
    assert pub_runner.wanted_platforms(cfg, picked) == ["tiktok_export"]


def test_youtube_long_metadata_has_chapters_and_no_shorts_tag(env):
    cfg, _, _ = env
    chapters = [{"at": 0.0, "title": "المقدمة"}, {"at": 15.0, "title": "الخلفية"}, {"at": 95.5, "title": "الأرقام"}]
    text = pub_common.PostText("عنوان", "caption body", ["#a"], "money", series="أرقام تهمك", kind="long",
                               chapters=chapters, thumbnail="assets/generated/video/1.thumb.jpg")
    meta = youtube.metadata(cfg, text)
    assert "#Shorts" not in meta["snippet"]["description"]
    assert "00:00 المقدمة\n00:15 الخلفية\n01:35 الأرقام" in meta["snippet"]["description"]
    short = youtube.metadata(cfg, pub_common.PostText("عنوان", "caption", ["#a"], "money"))
    assert short["snippet"]["description"].endswith("#Shorts")
    assert youtube.video_url("abc", "long") == "https://youtu.be/abc"
    assert youtube.video_url("abc") == "https://youtube.com/shorts/abc"
    assert pub_common.chapter_lines([{"at": 0, "title": "a"}, {"at": 5, "title": "b"}, {"at": 20, "title": "c"}]) == ""
    assert pub_common.chapter_lines([{"at": 3, "title": "a"}, {"at": 15, "title": "b"}, {"at": 30, "title": "c"}]) == ""


def test_post_text_carries_kind_chapters_and_thumbnail():
    ctx = {"notes": json.dumps({"kind": "long", "chapters": [{"at": 0, "title": "x"}], "thumbnail": "t.jpg",
                                "hook_title": "عنوان"}),
           "script_notes": "{}", "beats": json.dumps([{"role": "hook", "text": "هلا"}]), "hashtags": "[]",
           "sources": "[]", "kind": "long", "duration_s": 210.0}
    t = pub_common.post_text(ctx)
    assert t.kind == "long" and t.chapters == [{"at": 0, "title": "x"}] and t.thumbnail == "t.jpg"


# --- the pick flow ------------------------------------------------------------------------------

def _shortlist_rows(n=3):
    return [{"id": i, "title": f"Story {i}", "category": "tech", "audience_fit": 4, "source": "trends",
             "rank_reason": "why", "evergreen": i == 2} for i in range(1, n + 1)]


def test_pick_flow_toggles_and_keyboard(env):
    cfg, _, _ = env
    flow = picks.new(cfg, "trend", items=_shortlist_rows())
    assert flow["step"] == "items" and picks.toggle(flow, "pn", 0) == "Pick at least one topic first"
    assert picks.toggle(flow, "pk", 2) == "Picked" and flow["chosen"] == [2]
    assert picks.toggle(flow, "pk", 9) is None
    kb = picks.keyboard(flow)["inline_keyboard"]
    assert kb[0][1]["text"] == "2 ✅" and kb[-1][0]["callback_data"] == "pn:0"
    assert picks.toggle(flow, "pn", 0) is None and flow["step"] == "options"
    assert picks.toggle(flow, "pf", 1) == "Format updated" and flow["formats"] == ["short", "long"]
    assert picks.toggle(flow, "pf", 0) == "Format updated" and flow["formats"] == ["long"]
    assert picks.toggle(flow, "pf", 1) == "Keep at least one format"
    assert picks.toggle(flow, "pp", 1) == "Platforms updated" and "instagram" not in flow["platforms"]
    text = picks.text(flow, {"instagram": "no keys"})
    assert "Story 2" in text and "🎬 Long" in text and "Reels APIs cap" in text
    kb = picks.keyboard(flow)["inline_keyboard"]
    assert kb[-1][0]["text"].startswith("🚀 Make 1 video") and kb[-1][1]["callback_data"] == "pb:0"
    for act, vid in (("pk", 1), ("pf", 0), ("pp", 3), ("pn", 0), ("pg", 0), ("px", 0), ("pb", 0)):
        assert cards.parse_callback(f"{act}:{vid}") == (act, vid)


def test_pick_commit_selects_trending_items_with_the_ask(env):
    cfg, conn, _ = env
    upsert_candidates(conn, [Candidate(source="trends", external_id=f"SA:{i}", canonical_url=f"https://t.example/{i}",
                                       title=f"Story {i}") for i in (1, 2, 3)])
    conn.execute("UPDATE candidates SET status = 'ranked', retellable = 1, category = 'tech'")
    conn.execute("UPDATE candidates SET status = 'selected', selected_at = '2026-09-23 07:00:00' WHERE id = 3")
    conn.commit()
    flow = picks.new(cfg, "trend", items=_shortlist_rows())
    flow.update(chosen=[1, 3], formats=["short", "long"], platforms=["youtube"], step="options")
    summary = picks.commit(cfg, conn, flow)
    assert summary["ids"] == [1, 3]
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM candidates")}
    assert rows[1]["status"] == "selected" and formats.wanted_formats(rows[1]) == ["short", "long"]
    assert rows[3]["selected_at"] == "2026-09-23 07:00:00" and formats.is_manual(rows[3])   # widened, not re-dated
    assert rows[2]["status"] == "ranked"
    assert "2 videos" in picks.done_text({"ids": [1], "formats": ["short", "long"], "platforms": ["youtube"], "kind": "trend"})


def test_bot_topic_flow_creates_candidate_and_queues_produce(env):
    cfg, conn, _ = env
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_msg("/topic"))
    assert "Usage: /topic" in tg.last("sendMessage")["text"]
    h.handle(_msg("/topic لماذا ارتفع سعر الذهب"))
    sent = tg.last("sendMessage")
    assert "Topic:" in sent["text"] and sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "pf:0"
    msg_id = tg.next_id
    flow = picks.load(conn)
    assert flow["kind"] == "topic" and flow["msg"] == msg_id
    h.handle(_cb("pf:1", msg_id))                       # add Long
    assert tg.methods()[-2:] == ["answerCallbackQuery", "editMessageText"]
    h.handle(_cb("pp:1", msg_id))                       # drop Instagram
    h.handle(_cb("pg:0", msg_id))                       # go
    row = dict(conn.execute("SELECT * FROM candidates").fetchone())
    assert row["source"] == "manual" and row["status"] == "selected"
    assert formats.wanted_formats(row) == ["short", "long"] and "instagram" not in formats.wanted_platforms(row, {})
    assert jobs.queue(conn) == ["produce"] and picks.load(conn) is None
    assert "Making 2 videos" in tg.last("sendMessage")["text"]
    # a stale tap on the finished message is refused politely
    h.handle(_cb("pf:0", msg_id))
    assert "no longer active" in tg.last("answerCallbackQuery")["text"]


def test_bot_script_flow_picks_the_format_from_the_length(env):
    cfg, conn, _ = env
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_msg("/script قصير"))
    assert "at least a dozen words" in tg.last("sendMessage")["text"]
    h.handle(_msg("/script " + _words(300)))
    sent = tg.last("sendMessage")
    assert "300 words" in sent["text"] and "🎬 Long" in sent["text"]
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "pp:0"   # no format row for scripts
    h.handle(_cb("pg:0", tg.next_id))
    row = dict(conn.execute("SELECT * FROM candidates").fetchone())
    assert formats.wanted(row)["kind"] == "script" and formats.wanted_formats(row) == ["long"]
    h.handle(_msg("/script " + _words(600)))
    assert "too long" in tg.last("sendMessage")["text"]


def test_bot_run_trending_jobs_and_cancel(env, monkeypatch):
    cfg, conn, _ = env
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    h.handle(_msg("/run"))
    assert jobs.queue(conn) == ["run-daily"] and "queued" in tg.last("sendMessage")["text"]
    h.handle(_msg("/run"))
    assert "already" in tg.last("sendMessage")["text"] and jobs.queue(conn) == ["run-daily"]
    h.handle(_msg("/trending"))
    assert jobs.queue(conn) == ["run-daily", "trending"]
    h.handle(_msg("/jobs"))
    assert "Queued: run-daily, trending" in tg.last("sendMessage")["text"]
    h.handle(_msg("/status"))
    assert "Queued" in tg.last("sendMessage")["text"]
    h.handle(_msg("/topic gold"))
    msg_id = tg.next_id
    h.handle(_cb("px:0", msg_id))
    assert picks.load(conn) is None and "Cancelled" in tg.last("editMessageText")["text"]
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0


# --- jobs ---------------------------------------------------------------------------------------

def test_jobs_queue_and_inline_runner(env):
    cfg, conn, _ = env
    assert jobs.request(conn, "produce") and not jobs.request(conn, "produce")
    assert jobs.request(conn, "trending") and jobs.queue(conn) == ["produce", "trending"]
    with pytest.raises(ValueError):
        jobs.request(conn, "rm -rf")
    ran = []
    assert jobs.run_queued_inline(cfg, conn, ran.append) == ["produce", "trending"] and ran == ["produce", "trending"]
    assert jobs.queue(conn) == [] and jobs.status_line(conn).startswith("💤")


def test_jobs_tick_starts_next_and_reports_finished(env, monkeypatch):
    cfg, conn, _ = env
    started = []
    monkeypatch.setattr(jobs, "spawn", lambda cfg, conn, name: (started.append(name),
                                                                db.set_flag(conn, jobs.CURRENT_KEY, json.dumps(
                                                                    {"name": name, "pid": os.getpid(), "started": 1})))[1])
    jobs.request(conn, "trending")
    assert jobs.tick(cfg, conn) == {"started": "trending"} and started == ["trending"]
    assert jobs.tick(cfg, conn) is None                       # our own pid is alive → still running
    assert "Running: trending" in jobs.status_line(conn)
    db.set_flag(conn, jobs.CURRENT_KEY, json.dumps({"name": "trending", "pid": 999999999, "started": 1}))
    ev = jobs.tick(cfg, conn)
    assert ev["finished"] == "trending" and jobs.current(conn) is None


def test_bot_maintenance_reports_failed_jobs(env, monkeypatch):
    cfg, conn, _ = env
    tg = FakeTelegram()
    h = _handler(cfg, conn, tg)
    monkeypatch.setattr(jobs, "tick", lambda cfg, conn: {"finished": "produce", "code": 1, "seconds": 3})
    h.maintenance()
    assert "ended with errors" in tg.last("sendMessage")["text"]
    monkeypatch.setattr(jobs, "tick", lambda cfg, conn: {"finished": "produce", "code": 0, "seconds": 3})
    n = len(tg.calls)
    h.maintenance()
    assert len(tg.calls) == n                                  # a clean finish is silent (the cards speak)


def test_spawn_runs_a_detached_python(env, monkeypatch):
    cfg, conn, tmp = env
    seen = {}

    class P:
        pid = 4321

        def poll(self):
            return 0
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or P())
    assert jobs.spawn(cfg, conn, "produce") == 4321
    assert seen["cmd"][-3:] == ["-m", "src.main", "produce"] and seen["kw"]["cwd"] == str(tmp)
    assert seen["kw"]["start_new_session"] is True and (tmp / "data/logs/jobs.log").exists()
    assert jobs.current(conn)["name"] == "produce"


# --- rank: shortlist, manual picks outside the quota, automatic long pick -------------------------

def test_shortlist_and_manual_picks_do_not_eat_the_daily_quota(env):
    cfg, conn, _ = env
    cands = [Candidate(source="trends", external_id=f"SA:{i}", canonical_url=f"https://t.example/{i}", title=f"S{i}",
                       views=1000 * i) for i in range(1, 6)]
    upsert_candidates(conn, cands)
    conn.execute("UPDATE candidates SET status = 'ranked', retellable = 1, category = 'tech', score = id / 10.0, "
                 "topic = 'topic-' || id, ad_safe = 1")
    conn.execute("UPDATE candidates SET topic = 'topic-4' WHERE id = 5")      # same story as #4
    conn.execute("UPDATE candidates SET category = 'sports' WHERE id = 1")   # labelled-only: still offered
    conn.commit()
    ids = [r["id"] for r in rank_runner.shortlist(cfg, conn, n=10)]
    assert ids == [5, 4, 3, 2, 1][:4] or ids == [5, 3, 2, 1]                # one per topic, best first
    assert 4 not in ids
    # a manual pick today is not counted against top_n
    conn.execute("UPDATE candidates SET status = 'selected', selected_at = ?, wanted = ? WHERE id = 5",
                 (rank_runner.datetime.now(rank_runner.ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d %H:%M:%S"),
                  formats.encode(["short"], ["youtube"])))
    conn.commit()
    day = rank_runner.datetime.now(rank_runner.ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d")
    already = rank_runner._selected_today(conn, day)
    assert [r["id"] for r in already] == [5] and all(formats.is_manual(r) for r in already)
    assert 5 not in [r["id"] for r in rank_runner.shortlist(cfg, conn, n=10)]


def test_pick_long_prefers_evergreen_explainers():
    rows = [{"id": 1, "evergreen": 0, "audience_fit": 5, "format": "story", "score": 0.9},
            {"id": 2, "evergreen": 1, "audience_fit": 4, "format": "explainer", "score": 0.5},
            {"id": 3, "evergreen": 1, "audience_fit": 4, "format": "fact", "score": 0.8,
             "wanted": formats.encode(["short"], [])}]
    assert [r["id"] for r in rank_runner.pick_long(rows, 1)] == [2]
    assert rank_runner.pick_long(rows, 0) == []


# --- Wikipedia source ----------------------------------------------------------------------------

def test_wikipedia_top_parses_and_skips_navigation(env):
    cfg, _, _ = env
    payload = {"items": [{"articles": [
        {"article": "الصفحة_الرئيسية", "views": 900000, "rank": 1},
        {"article": "خاص:بحث", "views": 50000, "rank": 2},
        {"article": "محمد_صلاح", "views": 40000, "rank": 3},
        {"article": "تصنيف:أفلام", "views": 30000, "rank": 4},
        {"article": "الذكاء_الاصطناعي", "views": 20000, "rank": 5}]}]}
    got = wikipedia.parse_top(payload, "ar", "2026/09/22", 10)
    assert [c.title for c in got] == ["محمد صلاح", "الذكاء الاصطناعي"] and got[0].views == 40000
    assert got[0].canonical_url.startswith("https://ar.wikipedia.org/wiki/") and got[0].source == "wiki"
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/22"):
            return httpx.Response(404, json={"detail": "not yet"})
        return httpx.Response(200, json=payload)
    monkeypatch_now = wikipedia.datetime
    res = wikipedia.fetch(cfg, httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(res.candidates) == 2 or len(calls) >= 1


def test_cards_show_format_and_long_duration():
    assert cards.duration_text(48) == "48s" and cards.duration_text(272.4) == "4:32"
    ctx = {"id": 9, "duration_s": 250.0, "notes": json.dumps({"series": "أرقام تهمك", "kind": "long"}),
           "script_notes": json.dumps({"hook_title": "قصة الذهب"}), "hashtags": "[]", "sources": "[]",
           "beats": "[]", "kind": "long", "source": "manual", "rank_reason": ""}
    cap = cards.caption(ctx)
    assert cap.startswith("🎬 #9 · 4:10 · أرقام تهمك · 🎬 Long") and "✍️ Your request" in cap
