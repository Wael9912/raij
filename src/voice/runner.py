"""voice: synthesize an Arabic voiceover for every passed script.

Output (only under assets/generated/voice/, the assembler's allowed input):
  <script_id>.wav         48 kHz mono, loudness-normalized to voice.target_lufs
  <script_id>.words.json  word timings + beat spans, for subtitles and b-roll cuts (Phase 6)
A videos row records it as 'voiced'. Over voice.max_seconds → re-synthesized once at a faster
rate; still over → 'failed'. A network failure writes nothing, so the next run retries.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.config import Config
from src.voice import tts
from src.voice.tts import RunCmd, VoiceError

log = logging.getLogger("raij.voice")


class TooLong(VoiceError):
    """Still over the length limit at the fastest allowed rate."""


def _pending(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT x.* FROM scripts x WHERE x.status = 'passed' "
        "AND NOT EXISTS (SELECT 1 FROM videos v WHERE v.script_id = x.id) ORDER BY x.id"
    ).fetchall()
    return [dict(r) for r in rows]


def _brand(cfg: Config, brand_id: str) -> dict[str, Any]:
    for b in cfg.brands:
        if b["id"] == brand_id:
            return b
    raise VoiceError(f"brand {brand_id!r} is not in config")


def voice_script(cfg: Config, script: dict[str, Any], out_dir: Path, synth=tts.synthesize,
                 run: RunCmd = tts.run_cmd, voice_name: str | None = None, stem: str | None = None) -> dict[str, Any]:
    """Synthesize + normalize one script. Returns the videos-row fields. `voice_name`/`stem` let a
    re-voice use another voice without overwriting the first take."""
    brand = _brand(cfg, script["brand_id"])
    v = brand.get("voice") or {}
    voice, rate, pitch = voice_name or v.get("name", "ar-EG-ShakirNeural"), v.get("rate", "+0%"), v.get("pitch", "+0Hz")
    max_s, min_s = cfg.get("voice.max_seconds", 58), cfg.get("voice.min_seconds", 40)
    beats = json.loads(script["beats"])
    text = tts.speech_text(beats)
    wav = out_dir / f"{stem or script['id']}.wav"

    with tempfile.TemporaryDirectory(prefix="raij-tts-") as tmp:
        raw = Path(tmp) / "raw.mp3"
        words = synth(text, voice, rate, pitch, raw)
        seconds = tts.duration(cfg, raw, run=run)
        if seconds > max_s:
            faster = tts.faster_rate(rate, seconds, max_s)
            if faster is None:
                raise TooLong(f"{seconds:.1f}s at {rate}; would need more than +25% speed")
            log.info("Script %d: %.1fs > %ss, re-synthesizing at %s", script["id"], seconds, max_s, faster)
            rate = faster
            words = synth(text, voice, rate, pitch, raw)
            seconds = tts.duration(cfg, raw, run=run)
            if seconds > max_s:
                raise TooLong(f"{seconds:.1f}s even at {rate}")
        loud = tts.normalize(cfg, raw, wav, run=run)
    seconds = tts.duration(cfg, wav, run=run)

    spans = tts.beat_spans(beats, words)
    words_path = wav.with_suffix(".words.json")
    words_path.write_text(json.dumps({"voice": voice, "rate": rate, "duration": seconds,
                                      "words": [asdict(w) for w in words], "beats": spans},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
    notes = {"voice": voice, "rate": rate, "lufs": loud.get("output_i"), "true_peak": loud.get("output_tp"),
             "words": len(words), "script_words": len(script["body_ar"].split())}
    if seconds < min_s:
        notes["warning"] = f"short: {seconds:.1f}s < {min_s}s"
    return {"voice_path": str(wav.relative_to(cfg.root)), "duration_s": seconds, "notes": notes}


def voice(cfg: Config, conn: sqlite3.Connection, dry_run: bool = False, synth=tts.synthesize,
          run: RunCmd = tts.run_cmd) -> int:
    pending = _pending(conn)
    out_dir = cfg.root / "assets" / "generated" / "voice"
    if dry_run:
        log.info("[dry run] %d passed script(s) need a voiceover → %s", len(pending), out_dir)
        for s in pending:
            log.info("[dry run] script %d (%s): %d words", s["id"], s["brand_id"], len(s["body_ar"].split()))
        log.info("[dry run] no synthesis, nothing written")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = conn.execute("INSERT INTO runs (command) VALUES ('voice')").lastrowid
    conn.commit()
    voiced, failed, retry = [], [], []
    for s in pending:
        try:
            row = voice_script(cfg, s, out_dir, synth=synth, run=run)
        except TooLong as exc:
            log.warning("Script %d: too long — %s", s["id"], exc)
            conn.execute("INSERT INTO videos (script_id, status, notes) VALUES (?, 'failed', ?)",
                         (s["id"], json.dumps({"reason": str(exc)}, ensure_ascii=False)))
            conn.commit()
            failed.append({"script_id": s["id"], "error": str(exc)})
            continue
        except Exception as exc:                        # network/ffmpeg: retry next run
            log.error("Script %d: voice failed, will retry next run: %s", s["id"], exc)
            retry.append({"script_id": s["id"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        conn.execute("INSERT INTO videos (script_id, voice_path, duration_s, status, notes) "
                     "VALUES (?, ?, ?, 'voiced', ?)",
                     (s["id"], row["voice_path"], row["duration_s"], json.dumps(row["notes"], ensure_ascii=False)))
        conn.commit()
        voiced.append(s["id"])
        log.info("Script %d → %s (%.1fs, %s LUFS, rate %s)%s", s["id"], row["voice_path"], row["duration_s"],
                 row["notes"]["lufs"], row["notes"]["rate"],
                 f" — {row['notes']['warning']}" if "warning" in row["notes"] else "")

    if not pending or len(voiced) == len(pending):
        status = "ok"
    elif voiced:
        status = "partial"
    else:
        status = "failed"
    notes = {"pending": len(pending), "voiced": len(voiced), "failed": failed, "retry": retry}
    conn.execute("UPDATE runs SET finished_at = datetime('now'), status = ?, notes = ? WHERE id = ?",
                 (status, json.dumps(notes, ensure_ascii=False), run_id))
    conn.commit()
    level = logging.INFO if status == "ok" else logging.WARNING
    log.log(level, "Voice %s: %d/%d voiced, %d too long, %d to retry",
            status, len(voiced), len(pending), len(failed), len(retry))
    return 1 if status == "failed" else 0
