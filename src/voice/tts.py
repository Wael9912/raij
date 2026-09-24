"""Arabic voiceover: edge-tts synthesis with word timings, then ffmpeg loudness normalization.

edge-tts is free and keyless but needs network. Its service no longer accepts custom SSML, so
pauses come from punctuation: each beat is its own sentence. The Arabic voices read digits
correctly (checked live: 211, 953,531, 2025, 01:11), so script text is spoken as written.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import edge_tts

from src.config import Config

TICKS = 10_000_000                      # edge-tts offsets/durations are in 100 ns units
_END_PUNCT = re.compile(r"[.!؟?…:]$")

RunCmd = Callable[[list[str]], subprocess.CompletedProcess]


class VoiceError(RuntimeError):
    """Synthesis or audio processing failed for this script."""


class Unreachable(VoiceError):
    """edge-tts couldn't be reached at all (DNS, connection, timeout) — the network's fault, not the script's."""


class Truncated(VoiceError):
    """edge-tts returned only part of the audio (seen live 2026-09-24: 22 of 99 words while the Mac's network
    flapped — the 12 s clip was rendered and sent for review)."""


# aiohttp/websocket exception names that mean "no service", as opposed to a rejected request.
_UNREACHABLE = ("ClientConnector", "ClientOSError", "ServerDisconnected", "TimeoutError", "ConnectionReset",
                "WSServerHandshake", "gaierror")


@dataclass
class Word:
    text: str
    start: float
    end: float


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def speech_text(beats: list[dict[str, Any]]) -> str:
    """One sentence per beat, so the voice pauses between beats. A beat's vocalized `tts` text (Phase 18,
    diacritics for the engine) is spoken when present; the plain `text` is what subtitles show."""
    lines = []
    for b in beats:
        t = str(b.get("tts") or b["text"]).strip()
        lines.append(t if _END_PUNCT.search(t) else t + ".")
    return "\n".join(lines)


async def _synthesize(text: str, voice: str, rate: str, pitch: str, out: Path) -> list[Word]:
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="WordBoundary")
    words = []
    with open(out, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / TICKS
                words.append(Word(chunk["text"], round(start, 3), round(start + chunk["duration"] / TICKS, 3)))
    return words


def synthesize(text: str, voice: str, rate: str, pitch: str, out: Path, attempts: int = 4,
               sleep: Callable[[float], None] = time.sleep) -> list[Word]:
    """Write MP3 to `out` and return word timings. Retries transient network failures with backoff
    (2, 4, 8 s): edge-tts is keyless and the pipeline's one voice, so a throttle from a datacenter IP
    gets a real chance to clear before the script is left for the next run (A13)."""
    last: Exception | None = None
    for i in range(attempts):
        if i:
            sleep(2 ** i)
        try:
            words = asyncio.run(_synthesize(text, voice, rate, pitch, out))
        except Exception as exc:                       # edge-tts raises aiohttp/websocket errors
            last = exc
            continue
        if out.exists() and out.stat().st_size > 0 and words:
            return words
        last = VoiceError("edge-tts returned no audio")
    out.unlink(missing_ok=True)
    kind = Unreachable if any(mark in type(last).__name__ for mark in _UNREACHABLE) else VoiceError
    raise kind(f"edge-tts failed after {attempts} attempts: {type(last).__name__}: {last}")


def check_complete(words: list[Word], script_words: int, ratio: float = 0.6, min_words: int = 8) -> None:
    """Raise Truncated when the voice spoke far fewer words than the script has. edge-tts tokenizes a little
    differently from whitespace (a date is one token, "Giants.com" two), so real takes land at 0.9–1.05 of
    the script's count; a cut-off stream lands far below."""
    if script_words >= min_words and len(words) < ratio * script_words:
        raise Truncated(f"edge-tts returned {len(words)} of {script_words} words — cut-off stream")


def ffprobe_bin(cfg: Config) -> str:
    ffmpeg = cfg.secret("FFMPEG_BIN", "ffmpeg")
    return str(Path(ffmpeg).with_name("ffprobe")) if "/" in ffmpeg else "ffprobe"


def loudnorm_filter(target: float, measured: dict[str, str] | None = None) -> str:
    base = f"loudnorm=I={target}:TP=-1.5:LRA=11"
    if measured is None:
        return base + ":print_format=json"
    return (base + f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true:print_format=json")


def _loudnorm_json(stderr: str) -> dict[str, str]:
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end < start:
        raise VoiceError("loudnorm printed no measurements")
    return json.loads(stderr[start:end + 1])


def normalize(cfg: Config, src: Path, dst: Path, run: RunCmd = run_cmd) -> dict[str, str]:
    """Two-pass EBU R128 loudness normalization to voice.target_lufs → 48 kHz mono WAV."""
    ffmpeg = cfg.secret("FFMPEG_BIN", "ffmpeg")
    target = cfg.get("voice.target_lufs", -14)
    first = run([ffmpeg, "-hide_banner", "-nostats", "-i", str(src), "-af", loudnorm_filter(target),
                 "-f", "null", "-"])
    if first.returncode != 0:
        raise VoiceError(f"loudnorm measure failed: {first.stderr.strip()[-200:]}")
    measured = _loudnorm_json(first.stderr)
    second = run([ffmpeg, "-hide_banner", "-nostats", "-y", "-i", str(src), "-af",
                  loudnorm_filter(target, measured), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    if second.returncode != 0:
        raise VoiceError(f"loudnorm apply failed: {second.stderr.strip()[-200:]}")
    return _loudnorm_json(second.stderr)


def duration(cfg: Config, path: Path, run: RunCmd = run_cmd) -> float:
    proc = run([ffprobe_bin(cfg), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try:
        return round(float(proc.stdout.strip()), 3)
    except ValueError:
        raise VoiceError(f"ffprobe could not read {path.name}") from None


def faster_rate(base: str, seconds: float, limit: float, cap: int = 25) -> str | None:
    """Rate that should bring `seconds` under `limit`, e.g. '+0%' at 63s/58s → '+11%'; None if over cap."""
    base_pct = int(base.strip().rstrip("%") or 0)
    needed = base_pct + int((seconds / limit - 1) * 100) + 3      # small margin: speed-up isn't linear
    return f"{needed:+d}%" if needed <= cap else None


def _chars(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def beat_spans(beats: list[dict[str, Any]], words: list[Word]) -> list[dict[str, Any]]:
    """Start/end time of each beat. Aligns on letters+digits only, because edge-tts tokenizes
    differently from whitespace ("Giants.com", "لـ 37")."""
    stream, bounds = "", []
    for b in beats:
        bounds.append(len(stream))
        stream += _chars(b["text"])
    spans: list[dict[str, Any]] = [{"role": b["role"], "start": None, "end": None} for b in beats]
    pos = 0
    for w in words:
        token = _chars(w.text)
        if not token:
            continue
        at = stream.find(token, pos)
        if at == -1:
            continue                                   # voice said something unexpected; skip it
        pos = at + len(token)
        i = max(k for k, start in enumerate(bounds) if start <= at)
        span = spans[i]
        span["start"] = w.start if span["start"] is None else span["start"]
        span["end"] = w.end
    # A beat with no matched words gets the gap between its neighbours.
    for i, span in enumerate(spans):
        if span["start"] is None:
            prev_end = next((spans[j]["end"] for j in range(i - 1, -1, -1) if spans[j]["end"] is not None), 0.0)
            span["start"] = span["end"] = prev_end
    return spans
