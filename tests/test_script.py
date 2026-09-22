import json

import httpx
import pytest

from src import db
from src.config import load_config
from src.discover.common import Candidate, upsert_candidates
from src.script import facts, runner, similarity, write

SOURCE_AR = ("أبدى رئيس الاتحاد الدولي لكرة القدم جياني إنفانتينو استعداده لإجراء إصلاحات داخل الهيئة "
             "وذلك في رسالة موجهة إلى رؤساء مئتين وأحد عشر اتحادا وطنيا وإلى أعضاء مجلس الفيفا")
SECRET = "SOURCE-ONLY-MARKER"


def _words(n, word="كلمة"):
    return " ".join([word] * n)


def _draft(texts=None, n=100):
    """A valid 4-beat draft whose total length is n words unless texts are given."""
    texts = texts or ["هل سمعت الخبر", _words(n - 7), "والنتيجة مفاجئة", "اكتب رأيك"]
    roles = ["hook", "body", "payoff", "cta"]
    return {"beats": [{"role": r, "text": t, "broll_keywords": ["city street"]} for r, t in zip(roles, texts)],
            "description_en": "A story.", "hashtags": ["#news", "أخبار"]}


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


def _story(conn, transcript=SOURCE_AR + " " + SECRET):
    upsert_candidates(conn, [Candidate(source="rss", external_id="e1", canonical_url="https://x.example/1",
                                       title="Headline")])
    conn.execute("UPDATE candidates SET status = 'extracted'")
    conn.execute(
        "INSERT INTO stories (candidate_id, transcript, transcript_src, hook, key_facts, claims, why_trending) "
        "VALUES (1, ?, 'article', 'FIFA may reform', ?, ?, 'Fans care')",
        (transcript, json.dumps(["Letter sent to 211 associations", "Review welcomed"]),
         json.dumps([{"claim": "UEFA threatened boycott", "source": "Sky News Arabia"}])),
    )
    conn.commit()


def _llm(replies, prompts):
    """Gemini mock returning each reply in turn and recording prompts."""
    it = iter(replies)

    def handler(request):
        prompts.append(json.loads(request.content)["contents"][0]["parts"][0]["text"])
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(next(it))}]}}]})
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- similarity --------------------------------------------------------------

def test_normalize_folds_arabic_variants():
    assert similarity.normalize("أَعْلَنَ إصلاحاتٍ جديدة ــ ٢٠٢٧ مستشفى") == "اعلن اصلاحات جديده 2027 مستشفي"


def test_containment_separates_copy_from_retelling():
    copied = "استعداده لإجراء إصلاحات داخل الهيئة وذلك في رسالة موجهة إلى رؤساء"
    retold = "إنفانتينو يفتح الباب أخيرا أمام تغيير طريقة إدارة الفيفا بعد ضغط كبير"
    assert similarity.containment(copied, SOURCE_AR) == 1.0
    assert similarity.containment(retold, SOURCE_AR) == 0.0
    assert "لاجراء اصلاحات داخل" in similarity.shared_phrases(copied, SOURCE_AR)


def test_comparable_only_within_same_script():
    assert similarity.comparable("نص عربي", SOURCE_AR)
    assert not similarity.comparable("نص عربي", "An English source article")


# --- validation --------------------------------------------------------------

def test_validate_accepts_good_draft_and_normalizes_tags():
    punct = _draft(["هل سمعت الخبر؟", _words(92) + " «ميتا» (Meta) 17 ألف، 01:11 — 50%.", "والنتيجة!", "اكتب رأيك…"])
    write.validate(punct, 85, 115)
    d = write.validate(_draft(), 85, 115)
    assert d.words == 100 and d.hashtags == ["#news", "#أخبار"] and d.body_ar.count("\n") == 3


@pytest.mark.parametrize("bad, msg", [
    (_draft(n=60), "60 words — too short; add about 60"),
    (_draft(n=170), "cut about 30"),
    ({**_draft(), "beats": _draft()["beats"][1:]}, "hook"),
    ({**_draft(), "beats": [{**b, "broll_keywords": ["سوق"]} for b in _draft()["beats"]]}, "usable b-roll"),
    ({"beats": "nope"}, "no beats"),
    (_draft(["هل سمعت الخبر", _words(92) + " إيلاي مانニング", "والنتيجة مفاجئة", "اكتب رأيك"]), "stray"),
])
def test_validate_rejects(bad, msg):
    with pytest.raises(write.DraftError, match=msg):
        write.validate(bad, 110, 150)


# --- gate --------------------------------------------------------------------

def _copying_draft():
    body = " ".join([SOURCE_AR] * 3)                                   # lifts the source wholesale
    return _draft(["هل سمعت الخبر", body, "والنتيجة مفاجئة", "اكتب رأيك"])


def test_prompt_never_contains_source_text(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _story(conn)
    prompts = []
    assert runner.script(cfg, conn, client=_llm([_draft()], prompts), out_dir=tmp) == 0
    assert prompts and all(SECRET not in p and "رؤساء" not in p for p in prompts)
    assert "Letter sent to 211 associations" in prompts[0] and "Sky News Arabia" in prompts[0]


def test_similar_draft_is_rewritten_once_then_passes(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _story(conn)
    prompts = []
    assert runner.script(cfg, conn, client=_llm([_copying_draft(), _draft()], prompts), out_dir=tmp) == 0
    rows = [dict(r) for r in conn.execute("SELECT version, status, similarity, notes FROM scripts ORDER BY id")]
    assert [(r["version"], r["status"]) for r in rows] == [(1, "superseded"), (2, "passed")]
    assert rows[0]["similarity"] > 0.35 and rows[1]["similarity"] == 0.0
    assert "TOO CLOSE" in prompts[1] and SECRET not in prompts[1]
    assert conn.execute("SELECT status FROM candidates").fetchone()[0] == "scripted"


def test_still_similar_after_rewrite_is_rejected(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _story(conn)
    assert runner.script(cfg, conn, client=_llm([_copying_draft()] * 2, []), out_dir=tmp) == 1
    final = conn.execute("SELECT status, notes FROM scripts ORDER BY id DESC").fetchone()
    assert final["status"] == "rejected" and "after rewrite" in json.loads(final["notes"])["reason"]
    assert conn.execute("SELECT status FROM candidates").fetchone()[0] == "script_rejected"


def test_english_source_skips_gate(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _story(conn, transcript="Giants mourn the loss of former GM Ernie Accorsi, who died at 84.")
    assert runner.script(cfg, conn, client=_llm([_draft()], []), out_dir=tmp) == 0
    row = conn.execute("SELECT status, similarity, notes FROM scripts").fetchone()
    assert row["status"] == "passed" and row["similarity"] is None and "skipped" in row["notes"]


def test_bad_length_retries_with_feedback(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _story(conn)
    prompts = []
    assert runner.script(cfg, conn, client=_llm([_draft(n=60), _draft()], prompts), out_dir=tmp) == 0
    assert "60 words" in prompts[1]
    assert conn.execute("SELECT count(*) FROM scripts").fetchone()[0] == 1


def test_llm_outage_writes_nothing_and_rerun_is_idempotent(env, monkeypatch):
    cfg, conn, tmp = env
    _story(conn)
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert runner.script(cfg, conn, client=down, out_dir=tmp) == 1
    assert conn.execute("SELECT count(*) FROM scripts").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM candidates").fetchone()[0] == "extracted"

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    prompts = []
    client = _llm([_draft()], prompts)
    assert runner.script(cfg, conn, client=client, out_dir=tmp) == 0
    assert runner.script(cfg, conn, client=client, out_dir=tmp) == 0
    assert len(prompts) == 1 and conn.execute("SELECT count(*) FROM scripts").fetchone()[0] == 1


def test_dry_run_makes_no_calls_or_writes(env, caplog):
    cfg, conn, tmp = env
    _story(conn)
    caplog.set_level("INFO")

    def handler(request):
        raise AssertionError("network call in dry run")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert runner.script(cfg, conn, dry_run=True, client=client, out_dir=tmp) == 0
    assert "1 story×brand" in caplog.text
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert not list(tmp.glob("*.json"))


# --- number check ------------------------------------------------------------

CARD = {"hook": "Auction raised nearly one million euros",
        "key_facts": json.dumps(["953,531 euros including fees", "Letter to 211 associations", "Died in 2025 at 91"]),
        "claims": json.dumps([{"claim": "17,000 reports by 01:11 GMT", "source": "Downdetector"}]),
        "why_trending": ""}


def test_numbers_parse_grouping_scales_and_arabic_digits():
    assert facts.numbers("953,531 euros, 17 ألف بلاغ، ٢١١ اتحادا، 2.5 million") == [953531, 17000, 211, 2.5e6]


def test_unsupported_numbers_allow_rounding_only():
    ok = "أكثر من 950 ألف يورو و211 اتحادا وفي 2025 عن 91 عاما و17 ألف بلاغ و3 أشهر"
    assert facts.unsupported(ok, CARD) == []
    assert facts.unsupported("995 ألف يورو و201 اتحادا", CARD) == ["995000", "201"]


def test_wrong_number_triggers_retry_naming_it(env, monkeypatch):
    cfg, conn, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _story(conn)
    wrong = _draft(["هل سمعت الخبر", _words(92) + " 201 اتحادا", "والنتيجة مفاجئة", "اكتب رأيك"])
    right = _draft(["هل سمعت الخبر", _words(92) + " 211 اتحادا", "والنتيجة مفاجئة", "اكتب رأيك"])
    prompts = []
    assert runner.script(cfg, conn, client=_llm([wrong, right], prompts), out_dir=tmp) == 0
    assert "not on the story card: 201" in prompts[1]
    assert "211" in conn.execute("SELECT body_ar FROM scripts").fetchone()[0]


def test_faceless_keywords_and_person_field():
    beats = _draft()["beats"]
    beats[1] = {**beats[1], "broll_keywords": ["elderly man smiling", "stadium at night", "football coach"],
                "person": "Ernie Accorsi"}
    d = write.validate({**_draft(), "beats": beats}, 85, 115)
    assert d.beats[1]["broll_keywords"] == ["stadium at night"] and d.beats[1]["person"] == "Ernie Accorsi"
    beats[1] = {**beats[1], "broll_keywords": ["smiling woman portrait"]}
    with pytest.raises(write.DraftError, match="no people"):
        write.validate({**_draft(), "beats": beats}, 85, 115)
