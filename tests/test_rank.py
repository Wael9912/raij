import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src import db, llm
from src.config import load_config
from src.discover.common import Candidate, upsert_candidates
from src.rank import runner
from src.rank.retellability import parse_verdicts
from src.rank.score import percentiles, score_rows

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _ts(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    # Blank (not unset) so load_dotenv can't pull real keys from .env back in.
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.setenv(key, "")
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


def _gemini_reply(payload):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


# --- llm adapter -------------------------------------------------------------

def test_parse_json_tolerates_fences_and_prose():
    assert llm.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json('Sure! {"a": [1, 2]} hope that helps') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        llm.parse_json("no json here")


def test_llm_falls_back_past_failing_provider(env, monkeypatch):
    cfg, _, _ = env
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GROQ_API_KEY", "q")
    hits = []

    def handler(request):
        hits.append(request.url.host)
        if "generativelanguage" in request.url.host:
            return httpx.Response(400, json={"error": {"message": "bad key"}})
        assert request.headers["authorization"] == "Bearer q"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert llm.complete_json(cfg, "json please", client=client) == {"ok": True}
    assert hits == ["generativelanguage.googleapis.com", "api.groq.com"]


def test_llm_error_lists_every_provider(env):
    cfg, _, _ = env
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(llm.LLMError) as exc:
        llm.complete(cfg, "hi", client=client)
    msg = str(exc.value)
    assert "gemini: GEMINI_API_KEY not set" in msg and "groq: GROQ_API_KEY not set" in msg and "ollama" in msg


def test_load_prompt_rejects_unfilled_placeholders():
    with pytest.raises(ValueError):
        llm.load_prompt("classify", categories="tech")


# --- scoring -----------------------------------------------------------------

def test_percentiles():
    assert percentiles({1: 10.0, 2: 20.0, 3: 30.0, 4: None}) == {1: 0.0, 2: 0.5, 3: 1.0, 4: 0.5}
    assert percentiles({1: 5.0, 2: 5.0}) == {1: 0.5, 2: 0.5}


def test_score_prefers_fast_fresh_videos():
    def row(i, views, hours, source="youtube"):
        return {"id": i, "source": source, "views": views, "likes": views and views // 20, "comments": views and views // 100,
                "published_at": (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "raw_json": "{}"}

    rows = [row(1, 100_000, 2), row(2, 100_000, 40), row(3, 1_000, 2), row(4, None, 1, source="rss")]
    scored = score_rows(rows, {"view_velocity": 0.5, "engagement": 0.3, "recency": 0.2}, now=NOW)
    order = [s.id for s in scored]
    assert order.index(1) < order.index(2) and order.index(1) < order.index(3)
    rss = next(s for s in scored if s.id == 4)
    assert rss.parts["view_velocity"] == 0.5 and rss.parts["engagement"] == 0.5
    assert all(0 <= s.score <= 1 for s in scored)


# --- retellability -----------------------------------------------------------

def test_parse_verdicts_drops_malformed():
    payload = {"items": [
        {"id": 1, "retellable": True, "category": "tech", "reason": "facts"},
        {"id": "2", "retellable": False, "category": "Culture", "reason": "dance"},
        {"id": 3, "retellable": "yes", "category": "tech"},        # not a bool
        {"id": 4, "retellable": True, "category": "gossip"},       # unknown category
        {"id": 99, "retellable": True, "category": "tech"},        # not asked
        {"id": 1, "retellable": False, "category": "tech"},        # duplicate
    ]}
    got = parse_verdicts(payload, {1, 2, 3, 4}, {"tech", "culture"})
    assert [(v.id, v.retellable, v.category) for v in got] == [(1, True, "tech"), (2, False, "culture")]


# --- rank end to end ---------------------------------------------------------

VERDICTS = {
    "howto": (True, "life-hack"), "chip": (True, "tech"), "gpu": (True, "tech"), "phone": (True, "tech"),
    "dance": (False, "culture"), "vote": (True, "political"), "whale": (True, "wow-facts"),
    "goal": (True, "sports"),
}


def _seed(conn):
    cands = []
    for i, (slug, _) in enumerate(VERDICTS.items()):
        cands.append(Candidate(
            source="youtube", external_id=f"{slug:x<11}"[:11], canonical_url=f"https://youtu.be/{slug}",
            title=slug, views=100_000 - i * 10_000, likes=1000, comments=100, published_at=_ts(3),
            raw={"description": f"<b>{slug}</b> video"},
        ))
    upsert_candidates(conn, cands)


def _classifier(calls):
    def handler(request):
        prompt = json.loads(request.content)["contents"][0]["parts"][0]["text"]
        items = json.loads(re.search(r"Items \(JSON\):\n(.*)\n\nRespond", prompt, re.S).group(1))
        calls.append([it["title"] for it in items])
        return _gemini_reply({"items": [
            {"id": it["id"], "retellable": VERDICTS[it["title"]][0], "category": VERDICTS[it["title"]][1],
             "topic": "gpu-launch" if it["title"] in ("gpu", "phone") else it["title"],
             "reason": f"because {it['title']}"} for it in items
        ]})
    return handler


def test_rank_selects_top_retellable(env, monkeypatch, capsys):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    cfg.data["ranking"]["batch_size"] = 5
    _seed(conn)
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(_classifier(calls)))

    assert runner.rank(cfg, conn, client=client, out_dir=tmp) == 0
    assert [len(c) for c in calls] == [5, 3]                          # batched
    assert "<b>" not in json.dumps(calls)

    report = json.loads(capsys.readouterr().out)
    titles = [s["title"] for s in report["selected"]]
    assert len(titles) == 5
    assert "dance" not in titles and "vote" not in titles             # not retellable / political
    assert sum(1 for t in titles if t in ("chip", "gpu", "phone")) == 2   # max_per_category
    assert "phone" not in titles                                      # same topic as "gpu"
    assert all(s["reason"] and s["score_parts"] for s in report["selected"])
    assert [f["title"] for f in report["flagged"]] == ["vote"]
    assert json.loads((tmp / f"{report['date']}.json").read_text()) == report

    status = dict(conn.execute("SELECT title, status FROM candidates").fetchall())
    assert status["dance"] == "rejected" and status["vote"] == "flagged"
    run = conn.execute("SELECT status FROM runs WHERE command = 'rank'").fetchone()
    assert run["status"] == "ok"

    # Same day again: no new LLM calls, same five picks.
    assert runner.rank(cfg, conn, client=client, out_dir=tmp) == 0
    assert len(calls) == 2
    assert [s["title"] for s in json.loads(capsys.readouterr().out)["selected"]] == titles


def test_rank_without_llm_scores_but_selects_nothing(env, capsys):
    cfg, conn, tmp = env
    _seed(conn)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert runner.rank(cfg, conn, client=client, out_dir=tmp) == 1
    assert json.loads(capsys.readouterr().out)["selected"] == []
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE score IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE retellable IS NOT NULL").fetchone()[0] == 0
    run = conn.execute("SELECT status, notes FROM runs WHERE command = 'rank'").fetchone()
    assert run["status"] == "failed" and "llm_error" in json.loads(run["notes"])


def test_rank_dry_run_writes_nothing(env, capsys):
    cfg, conn, tmp = env
    _seed(conn)
    assert runner.rank(cfg, conn, dry_run=True, out_dir=tmp) == 0
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE status != 'new'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert not list(tmp.glob("*.json"))
