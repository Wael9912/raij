"""Compare Arabic edge-tts voices on one script: synthesize each, transcribe with Gemini, score word accuracy,
and optionally send the clips to the owner's Telegram so they can *hear* them (the owner picks by ear).

    uv run python tools/voice_bench.py --script 39 --voices ar-JO-TaimNeural,ar-SY-LaithNeural [--tashkeel] [--send]

Word accuracy is 1 − WER between the script and Gemini's transcription after light normalization (diacritics,
punctuation, alef/hamza variants, ة/ه, ى/ي), so it measures whether the voice *read the words* — accent and
warmth are for the owner's ears. Everything goes to data/bench/<script>/.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import db, llm  # noqa: E402
from src.config import load_config  # noqa: E402
from src.discover.common import make_client, request  # noqa: E402
from src.script import fusha  # noqa: E402
from src.voice import tts  # noqa: E402

_PUNCT = re.compile(r"[^\w\s]", re.U)


def norm_words(text: str) -> list[str]:
    t = fusha.strip_tashkeel(text)
    t = _PUNCT.sub(" ", t)
    t = re.sub("[إأآٱ]", "ا", t).replace("ة", "ه").replace("ى", "ي")
    return t.split()


def accuracy(ref: str, hyp: str) -> float:
    a, b = norm_words(ref), norm_words(hyp)
    if not a:
        return 0.0
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    matched = sum(m.size for m in sm.get_matching_blocks())
    return round(matched / max(len(a), len(b)), 3)


def transcribe(cfg, client, mp3: Path) -> str:
    """Gemini audio transcription (inline data), through the same model chain as the pipeline."""
    key = cfg.secret("GEMINI_API_KEY")
    models = [cfg.secret("GEMINI_MODEL", "gemini-flash-latest")] + list(cfg.get("llm.gemini_fallback_models", []))
    audio = base64.b64encode(mp3.read_bytes()).decode()
    body = {"contents": [{"role": "user", "parts": [
        {"text": "اكتب النص المنطوق في هذا المقطع الصوتي حرفيا كما سُمع، بالعربية، دون أي تعليق أو تشكيل."},
        {"inline_data": {"mime_type": "audio/mpeg", "data": audio}}]}],
        "generationConfig": {"temperature": 0.0}}
    last = None
    for model in models:
        if model.startswith("gemma"):
            continue                                            # Gemma models don't take audio
        try:
            resp = request(client, "POST", llm.GEMINI_URL.format(model=model), json=body, headers={"x-goog-api-key": key},
                           retries=0, timeout=120)
            parts = resp.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        except Exception as exc:                                # noqa: BLE001 — try the next model
            last = exc
            continue
    raise RuntimeError(f"no Gemini model transcribed the clip: {last}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", type=int, required=True, help="scripts.id to read")
    ap.add_argument("--voices", required=True, help="comma-separated edge-tts voice names")
    ap.add_argument("--rate", default=None, help="override the brand rate, e.g. +5%")
    ap.add_argument("--tashkeel", action="store_true", help="also synthesize the vocalized (beats[].tts) text")
    ap.add_argument("--send", action="store_true", help="send each clip to the owner's Telegram")
    ap.add_argument("--no-transcribe", action="store_true")
    ap.add_argument("--beats-file", help="JSON beats (with tts) to use instead of the script's stored beats")
    args = ap.parse_args()

    cfg = load_config()
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM scripts WHERE id = ?", (args.script,)).fetchone()
    if row is None:
        print(f"script {args.script} not found")
        return 1
    beats = json.loads(Path(args.beats_file).read_text(encoding="utf-8")) if args.beats_file else json.loads(row["beats"])
    body = "\n".join(b["text"] for b in beats)
    brand = next(b for b in cfg.brands if b["id"] == row["brand_id"])
    rate = args.rate or (brand.get("voice") or {}).get("rate", "+0%")
    out = cfg.root / "data" / "bench" / (f"{args.script}-polished" if args.beats_file else str(args.script))
    out.mkdir(parents=True, exist_ok=True)
    plain = tts.speech_text([{**b, "tts": None} for b in beats])
    variants = [("plain", plain)]
    if args.tashkeel and any(b.get("tts") for b in beats):
        variants.append(("tashkeel", tts.speech_text(beats)))
    client = make_client()
    results = []
    bot = chat = None
    if args.send:
        from src.review.runner import make_bot
        bot, chat = make_bot(cfg, client)
    for voice in [v.strip() for v in args.voices.split(",") if v.strip()]:
        for label, text in variants:
            mp3 = out / f"{voice}.{label}.mp3"
            words = tts.synthesize(text, voice, rate, "+0Hz", mp3)
            secs = words[-1].end if words else 0
            hyp = "" if args.no_transcribe else transcribe(cfg, client, mp3)
            acc = None if args.no_transcribe else accuracy(body, hyp)
            results.append({"voice": voice, "variant": label, "seconds": round(secs, 1), "accuracy": acc, "heard": hyp})
            print(f"{voice:24s} {label:9s} {secs:5.1f}s  accuracy={acc}")
            if bot:
                with mp3.open("rb") as f:
                    bot.call("sendAudio", files={"audio": (mp3.name, f, "audio/mpeg")}, chat_id=chat,
                             title=f"{voice} · {label}", performer="Ra'ij voice test",
                             caption=f"🎙 {voice} · {label} · {secs:.0f}s")
    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwritten {out / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
