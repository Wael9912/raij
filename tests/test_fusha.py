"""Phase 18 — professional فصحى: dialect gate, editor pass with vocalized TTS text, plain subtitles."""
import json

import httpx
import pytest

from src import db, llm
from src.config import load_config
from src.script import fusha, polish, write
from src.voice import runner as voice_runner
from src.voice import tts

EGYPTIAN = "ليه الدولار الامريكي مولع اليومين دول؟ مسؤولو الفيدرالي لمحوا لزيادات جديدة لو التضخم ما هداش."
FUSHA = "لماذا يرتفع الدولار الأمريكي هذه الأيام؟ ألمح مسؤولو الفيدرالي إلى زيادات جديدة إذا لم يهدأ التضخم."


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GROQ_API_KEY", "")
    cfg = load_config()
    cfg.root = tmp_path
    monkeypatch.setattr(llm, "_EXHAUSTED", set())          # a 429 in one test must not exhaust models for the next
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    yield cfg, conn, tmp_path
    conn.close()


def _llm(replies):
    it = iter(replies)

    def handler(request):
        try:
            reply = next(it)
        except StopIteration:
            reply = 429                                           # out of scripted answers = out of quota
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "nope"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(reply, ensure_ascii=False)}]}}]})
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- detector ---------------------------------------------------------------------------

def test_dialect_words_flags_egyptian_gulf_levantine_but_not_fusha():
    assert fusha.dialect_words(EGYPTIAN) == ["ليه", "مولع", "اليومين", "ما هداش"]
    assert fusha.dialect_words("زين، كيف تبحث عن أغنية نسيت اسمها؟") == ["زين"]
    assert fusha.dialect_words("شو هالشي؟ كتير حلو") == ["شو", "هالشي", "كتير"]
    assert fusha.dialect_words("وليه ما تجرب؟ بالحين") == ["ليه", "الحين"]        # clitics
    assert fusha.dialect_words(FUSHA) == []
    assert fusha.dialect_words("رصد العلماء إشارات راديوية متكررة من الكوكب بيتا بيكتوريس بي عبر تلسكوب ميركات.") == []
    assert fusha.dialect_words("ارتفع الذهب فوق 4000 دولار، زيادة قدرها 3 في المئة. عاد السوق إلى الهدوء.") == []


def test_tashkeel_helpers():
    assert fusha.strip_tashkeel("أَعْلَنَتْ يُوتِيُوبُ") == "أعلنت يوتيوب"
    assert fusha.same_letters("أَعْلَنَتْ يُوتِيُوبُ،", "أعلنت يوتيوب")
    assert not fusha.same_letters("أَعْلَنَتْ يُوتِيُوبَ", "اعلنت يوتيوب")          # hamza changed = different letters


# --- editor pass --------------------------------------------------------------------------

BEATS = [{"role": "hook", "text": "ليه الذهب مولع؟", "broll_keywords": ["gold bars"]},
         {"role": "body", "text": "ارتفع الذهب إلى 4378 دولار للأونصة حسب رويترز.", "broll_keywords": ["market chart"]},
         {"role": "payoff", "text": "المستثمرون يبحثون عن ملاذ آمن.", "broll_keywords": ["vault"]},
         {"role": "cta", "text": "شاركها مع صديق.", "broll_keywords": ["phone"]}]
STORY = {"hook": "gold record", "key_facts": json.dumps(["Spot gold hit 4378 dollars an ounce"]), "claims": "[]",
         "why_trending": "", "transcript": ""}


def _reply(texts, tts=None):
    tts = tts or [None] * len(texts)
    return {"beats": [{"text": t, "tts": v or ""} for t, v in zip(texts, tts)]}


def test_polish_keeps_only_verified_changes(env):
    cfg, _, _ = env
    good = ["لماذا يشتعل الذهب؟", "ارتفع الذهب إلى 4378 دولار للأونصة حسب رويترز.", "يبحث المستثمرون عن ملاذ آمن.", "شاركها مع صديق."]
    voc = ["لِمَاذَا يَشْتَعِلُ الذَّهَبُ؟", "ارْتَفَعَ الذَّهَبُ إِلَى 4378 دُولَارٍ لِلْأُونْصَةِ حَسَبَ رُويتِرْز.",
           "يَبْحَثُ الْمُسْتَثْمِرُونَ عَنْ مَلَاذٍ آمِنٍ.", "شَارِكْهَا مَعَ صَدِيقٍ."]
    beats, notes = polish.polish(cfg, BEATS, STORY, {"name": "رائج"}, client=_llm([_reply(good, voc)]))
    assert [b["text"] for b in beats] == good and all(b["tts"] for b in beats)
    assert beats[1]["broll_keywords"] == ["market chart"]                        # the rest of the beat survives
    assert notes["changed"] == 2 and notes["tts"] == 4 and "kept" not in notes and notes["model"]   # 2 beats reworded


def test_polish_rejects_dialect_number_changes_and_bad_tashkeel(env):
    cfg, _, _ = env
    reply = _reply(["ليه الذهب مولع؟",                                           # still dialect → draft kept
                    "ارتفع الذهب إلى 4400 دولار للأونصة حسب رويترز.",             # digit changed → draft kept
                    "يبحث المستثمرون عن ملاذ آمن.",                              # fine
                    "شاركها مع صديق."],
                   [None, None, "يَبْحَثُ الْمُسْتَثْمِرُونَ عَنْ مَلَاذٍ آمِنٍ.", "شَارِكْهَا مَعَ صَدِيقَيْنِ."])   # last: letters differ
    beats, notes = polish.polish(cfg, BEATS, STORY, {"name": "رائج"}, client=_llm([reply]))
    assert beats[0]["text"] == BEATS[0]["text"] and beats[1]["text"] == BEATS[1]["text"]
    assert beats[2]["text"] == "يبحث المستثمرون عن ملاذ آمن." and beats[2]["tts"]
    assert "tts" not in beats[3] and "tts" not in beats[0]
    assert notes["kept"] == [0, 1] and notes["changed"] == 1 and notes["tts"] == 1


def test_polish_survives_llm_failure_and_count_mismatch(env):
    cfg, _, _ = env
    beats, notes = polish.polish(cfg, BEATS, STORY, {}, client=_llm([_reply(["واحد", "اثنان"])]))
    assert beats == BEATS and notes["error"] == "beat count mismatch"
    beats, notes = polish.polish(cfg, BEATS, STORY, {}, client=_llm([429]))       # then out of quota everywhere
    assert beats == BEATS and "error" in notes


# --- writer integration --------------------------------------------------------------------

def _draft_reply(texts):
    roles = ["hook", "body", "body", "payoff", "cta"]
    return {"beats": [{"role": r, "text": t, "broll_keywords": ["gold bars"]} for r, t in zip(roles, texts)],
            "hook_title": "الذهب يكسر التوقعات", "series": "أرقام تهمك", "description_en": "Gold.", "hashtags": ["#gold"]}


def _words(n, seed="ارتفع الذهب بقوة هذا العام"):
    return " ".join((seed + " ").split() * (n // 5 + 1))[:10_000].split()[:n]


def test_write_script_rejects_dialect_then_polishes(env):
    cfg, _, _ = env
    cfg.data["script"]["min_words"] = 20
    cfg.data["script"]["max_words"] = 200
    body = " ".join(_words(30))
    dialect = _draft_reply(["ليه الذهب مولع اليومين دول؟", body, body, body, "شاركها مع صديق."])
    clean = _draft_reply(["لماذا يرتفع الذهب هذه الأيام؟", body, body, body, "شاركها مع صديق."])
    polished = _reply(["لماذا يرتفع الذهب هذه الأيام؟", body, body, body, "شاركها مع صديق."],
                      ["لِمَاذَا يَرْتَفِعُ الذَّهَبُ هَذِهِ الْأَيَّامَ؟", None, None, None, "شَارِكْهَا مَعَ صَدِيقٍ."])
    story = {"id": 1, "hook": "", "key_facts": "[]", "claims": "[]", "why_trending": "", "transcript": "Gold rose."}
    out = write.write_script(cfg, story, cfg.brands[0], client=_llm([dialect, clean, polished]))
    final = out.final
    assert final["status"] == "passed" and final["beats"][0]["text"] == "لماذا يرتفع الذهب هذه الأيام؟"
    assert final["beats"][0]["tts"].startswith("لِمَاذَا") and "tts" not in final["beats"][1]
    assert final["notes"]["polish"]["tts"] == 2 and final["notes"]["model"]
    assert final["body_ar"].startswith("لماذا")


def test_write_script_polish_can_be_switched_off(env):
    cfg, _, _ = env
    cfg.data["script"]["min_words"] = 20
    cfg.data["script"]["max_words"] = 200
    cfg.data["script"]["polish"] = False
    body = " ".join(_words(30))
    clean = _draft_reply(["لماذا يرتفع الذهب هذه الأيام؟", body, body, body, "شاركها مع صديق."])
    story = {"id": 1, "hook": "", "key_facts": "[]", "claims": "[]", "why_trending": "", "transcript": "Gold rose."}
    out = write.write_script(cfg, story, cfg.brands[0], client=_llm([clean]))
    assert out.final["status"] == "passed" and "polish" not in out.final["notes"]


# --- voice ------------------------------------------------------------------------------------

def test_speech_text_prefers_vocalized_beats_and_words_come_back_plain(env, tmp_path):
    cfg, conn, _ = env
    beats = [{"role": "hook", "text": "لماذا يرتفع الذهب؟", "tts": "لِمَاذَا يَرْتَفِعُ الذَّهَبُ؟"},
             {"role": "cta", "text": "شاركها مع صديق"}]
    assert tts.speech_text(beats) == "لِمَاذَا يَرْتَفِعُ الذَّهَبُ؟\nشاركها مع صديق."
    conn.execute("INSERT INTO candidates (source, external_id, canonical_url, status) VALUES ('rss', 'e', 'u', 'x')")
    conn.execute("INSERT INTO stories (candidate_id) VALUES (1)")
    conn.execute("INSERT INTO scripts (story_id, brand_id, body_ar, beats, status) VALUES (1, 'raij', ?, ?, 'passed')",
                 ("لماذا يرتفع الذهب؟\nشاركها مع صديق", json.dumps(beats, ensure_ascii=False)))
    conn.commit()
    spoken = []

    def synth(text, voice, rate, pitch, out):
        spoken.append(text)
        out.write_bytes(b"mp3")
        return [tts.Word("لِمَاذَا", 0.0, 0.4), tts.Word("يَرْتَفِعُ", 0.4, 0.9), tts.Word("الذَّهَبُ؟", 0.9, 1.4),
                tts.Word("شاركها", 1.6, 2.0), tts.Word("مع", 2.0, 2.2), tts.Word("صديق", 2.2, 2.6)]

    def run(cmd):
        class P:
            returncode, stdout = 0, "2.6\n"
            stderr = '{"input_i": "-20", "input_tp": "-3", "input_lra": "5", "input_thresh": "-30", "target_offset": "0", "output_i": "-14", "output_tp": "-1.5"}'
        return P()

    (tmp_path / "voice").mkdir()
    row = voice_runner.voice_script(cfg, dict(conn.execute("SELECT * FROM scripts").fetchone()), tmp_path / "voice",
                                    synth=synth, run=run)
    assert spoken[0].startswith("لِمَاذَا")                                      # the engine got the diacritics
    data = json.loads((tmp_path / "voice" / "1.words.json").read_text(encoding="utf-8"))
    assert [w["text"] for w in data["words"]][:3] == ["لماذا", "يرتفع", "الذهب؟"]     # subtitles get plain words
    assert data["beats"][0]["end"] == 1.4 and data["beats"][1]["start"] == 1.6     # alignment survived
    assert row["notes"]["tashkeel"] == 1
