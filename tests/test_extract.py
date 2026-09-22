import json
import subprocess
from pathlib import Path

import httpx
import pytest

from src import db
from src.config import load_config
from src.discover.common import Candidate, upsert_candidates
from src.extract import runner, sources

ARTICLE_HTML = """<html><head><title>X</title></head><body><nav>Home | World | Sport</nav>
<article><h1>Record auction</h1>{}</article><footer>© Example</footer></body></html>""".format(
    "".join(f"<p>Paragraph {i}: the auction in Paris sold nearly every lot, raising more than "
            f"950,000 euros for the late star's foundation, organisers said on Sunday.</p>" for i in range(6))
)

VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000 align:start position:0%
hello<00:00:00.500><c> world</c>

00:00:02.000 --> 00:00:04.000
hello world

00:00:04.000 --> 00:00:06.000
this is &amp; a test
"""

CARD = {"usable": True, "reason": "", "hook": "A Paris auction just made history.",
        "key_facts": ["Nearly every lot sold", "Over 950,000 euros raised", "Proceeds go to a foundation"],
        "claims": [{"claim": "950,000 euros total", "source": "Sky News Arabia"}],
        "why_trending": "Fans are nostalgic for the star."}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.setenv(key, "")
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


def _select(conn, cands):
    upsert_candidates(conn, cands)
    conn.execute("UPDATE candidates SET status = 'selected', selected_at = datetime('now'), "
                 "topic = 'some-topic', rank_reason = 'good story'")
    conn.commit()


def _gemini(payload):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


def _no_cmd(cmd):
    raise AssertionError(f"unexpected subprocess: {cmd}")


# --- parsing -----------------------------------------------------------------

def test_parse_vtt_strips_timing_tags_and_rolling_duplicates():
    assert sources.parse_vtt(VTT) == "hello world this is & a test"


def test_article_text_drops_page_chrome():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=ARTICLE_HTML)))
    text = sources.article_text(client, "https://news.example/a")
    assert "950,000 euros" in text and "Home | World" not in text


def test_validate_card():
    card = runner.validate_card({**CARD, "key_facts": CARD["key_facts"] * 3})
    assert len(card["key_facts"]) == 5
    with pytest.raises(sources.ExtractError, match="paywall"):
        runner.validate_card({"usable": False, "reason": "paywall stub"})
    with pytest.raises(runner.CardError):
        runner.validate_card({"usable": True, "hook": "", "key_facts": ["a"]})


# --- YouTube: subtitles, whisper fallback, no media persists -----------------

def test_youtube_subs_prefers_arabic():
    def fake(cmd):
        out = Path(cmd[cmd.index("-o") + 1]).parent
        (out / "subs.en.vtt").write_text(VTT)
        (out / "subs.ar.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nمرحبا\n")
        return subprocess.CompletedProcess(cmd, 0)
    assert sources.youtube_subs("https://www.youtube.com/watch?v=abcdefghijk", run=fake) == "مرحبا"


def test_whisper_audio_deleted_even_on_error(tmp_path):
    seen = {}

    def fake(cmd):
        out = Path(cmd[cmd.index("-o") + 1]).parent
        (out / "audio.webm").write_bytes(b"\x00" * 64)
        seen["dir"] = out
        return subprocess.CompletedProcess(cmd, 0)

    def boom(audio, model):
        assert audio.exists()
        raise RuntimeError("model crashed")

    with pytest.raises(RuntimeError):
        sources.youtube_whisper("https://www.youtube.com/watch?v=abcdefghijk", run=fake, transcribe=boom)
    assert not seen["dir"].exists()

    assert sources.youtube_whisper("u", run=fake, transcribe=lambda a, m: "spoken words") == "spoken words"
    assert not seen["dir"].exists()


def test_youtube_without_subs_falls_back_to_whisper(env, monkeypatch):
    cfg, _, _ = env
    calls = []

    def fake(cmd):
        calls.append(cmd)
        if "-f" in cmd:
            (Path(cmd[cmd.index("-o") + 1]).parent / "audio.m4a").write_bytes(b"x")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(sources, "_whisper", lambda audio, model: "transcribed speech")
    row = {"id": 1, "source": "youtube", "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk",
           "title": "t", "duration_s": 60, "raw_json": "{}"}
    got = sources.source_text(cfg, httpx.Client(), row, run=fake)
    assert (got.src, got.text) == ("whisper", "transcribed speech")
    assert "--skip-download" in calls[0] and "-f" in calls[1]


def test_long_video_without_subs_skips_whisper(env):
    cfg, _, _ = env
    row = {"id": 1, "source": "youtube", "canonical_url": "u", "title": "t", "duration_s": 3600, "raw_json": "{}"}
    with pytest.raises(sources.ExtractError, match="longer than"):
        sources.source_text(cfg, httpx.Client(), row, run=lambda c: subprocess.CompletedProcess(c, 0))


# --- routing per source type -------------------------------------------------

def test_trends_reads_linked_articles_and_skips_failures(env):
    cfg, _, _ = env

    def handler(request):
        if request.url.host == "blocked.example":
            return httpx.Response(403)
        return httpx.Response(200, text=ARTICLE_HTML)

    raw = {"news": [{"title": "Blocked", "url": "https://blocked.example/x", "source": "B"},
                    {"title": "Good", "url": "https://good.example/y", "source": "G"}]}
    row = {"id": 1, "source": "trends", "title": "term", "canonical_url": "t", "raw_json": json.dumps(raw)}
    got = sources.source_text(cfg, httpx.Client(transport=httpx.MockTransport(handler)), row, run=_no_cmd)
    assert got.src == "news" and got.urls == ["https://good.example/y"] and "[G] Good" in got.text


def test_rss_falls_back_to_feed_summary(env):
    cfg, _, _ = env
    row = {"id": 1, "source": "rss", "title": "Headline", "canonical_url": "https://x.example/a",
           "raw_json": json.dumps({"summary": "<p>Short &amp; sweet summary.</p>"})}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    got = sources.source_text(cfg, client, row, run=_no_cmd)
    assert got.src == "summary" and got.text == "Headline\n\nShort & sweet summary."


def test_reddit_uses_selftext_else_linked_article(env):
    cfg, _, _ = env
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=ARTICLE_HTML)))
    long_self = {"selftext": "TIL " + "fact " * 100, "linked_url": "https://www.reddit.com/r/x"}
    row = {"id": 1, "source": "reddit", "title": "TIL", "canonical_url": "r", "raw_json": json.dumps(long_self)}
    assert sources.source_text(cfg, client, row, run=_no_cmd).src == "selftext"
    row["raw_json"] = json.dumps({"selftext": "", "linked_url": "https://news.example/story"})
    got = sources.source_text(cfg, client, row, run=_no_cmd)
    assert got.src == "article" and got.urls == ["https://news.example/story"]


# --- stage -------------------------------------------------------------------

def _rss_cand(i, summary="A long enough summary of what happened at the auction in Paris this weekend."):
    return Candidate(source="rss", external_id=f"e{i}", canonical_url=f"https://news.example/{i}",
                     title=f"Story {i}", raw={"feed": "Sky News Arabia", "summary": summary})


def test_extract_end_to_end_and_idempotent(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _select(conn, [_rss_cand(1), _rss_cand(2, summary="")])
    prompts = []

    def handler(request):
        if "generativelanguage" in request.url.host:
            prompt = json.loads(request.content)["contents"][0]["parts"][0]["text"]
            prompts.append(prompt)
            return _gemini(CARD)
        if request.url.path == "/1":
            return httpx.Response(200, text=ARTICLE_HTML)
        return httpx.Response(200, text="<html><body>subscribe</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert runner.extract(cfg, conn, client=client, run=_no_cmd, out_dir=tmp) == 0

    story = dict(conn.execute("SELECT * FROM stories").fetchone())
    assert story["transcript_src"] == "article" and "950,000 euros" in story["transcript"]
    assert json.loads(story["key_facts"]) == CARD["key_facts"]
    assert json.loads(story["sources"]) == ["https://news.example/1"]
    assert "some-topic" in prompts[0] and "Sky News Arabia" in prompts[0]
    statuses = dict(conn.execute("SELECT external_id, status FROM candidates").fetchall())
    assert statuses == {"e1": "extracted", "e2": "extract_failed"}          # no text for #2
    run = conn.execute("SELECT status, notes FROM runs WHERE command = 'extract'").fetchone()
    assert run["status"] == "partial" and json.loads(run["notes"])["extracted"] == 1
    assert len(json.loads(next(tmp.glob("*.json")).read_text())) == 1

    assert runner.extract(cfg, conn, client=client, run=_no_cmd, out_dir=tmp) == 0
    assert conn.execute("SELECT count(*) FROM stories").fetchone()[0] == 1
    assert len(prompts) == 1


def test_llm_outage_leaves_candidates_for_retry(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _select(conn, [_rss_cand(1)])

    def handler(request):
        if "generativelanguage" in request.url.host:
            return httpx.Response(400, json={"error": {"message": "quota"}})
        return httpx.Response(200, text=ARTICLE_HTML)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert runner.extract(cfg, conn, client=client, run=_no_cmd, out_dir=tmp) == 1
    assert conn.execute("SELECT status FROM candidates").fetchone()[0] == "selected"
    assert conn.execute("SELECT count(*) FROM stories").fetchone()[0] == 0


def test_dry_run_makes_no_calls_or_writes(env, caplog):
    cfg, conn, tmp = env
    _select(conn, [_rss_cand(1)])

    def handler(request):
        raise AssertionError("network call in dry run")

    caplog.set_level("INFO")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert runner.extract(cfg, conn, dry_run=True, client=client, run=_no_cmd, out_dir=tmp) == 0
    assert "1 selected candidate" in caplog.text
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM candidates").fetchone()[0] == "selected"
    assert not list(tmp.glob("*.json"))
