"""Render the final 1080×1920 video with ffmpeg: b-roll segments → subtitle overlay → end card,
voice plus optional CC0 music ducked under it.

Guardrail: every media input is resolved (symlinks followed) and must sit inside
config.ALLOWED_MEDIA_SUBDIRS under the repo root — licensed stock, our generated assets, CC0
music. Anything else (e.g. a source video) makes the render refuse to start.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from src.assemble.subtitles import Renderer
from src.config import ALLOWED_MEDIA_SUBDIRS, Config

RunCmd = Callable[[list[str]], subprocess.CompletedProcess]
W, H, FPS = 1080, 1920, 30
STILL_EXT = {".jpg", ".jpeg", ".png"}


class GuardrailError(RuntimeError):
    """A render input is outside the allowed media directories."""


class RenderError(RuntimeError):
    pass


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=900)


def guard(cfg: Config, path: Path) -> Path:
    real = (cfg.root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    for sub in ALLOWED_MEDIA_SUBDIRS:
        if real.is_relative_to((cfg.root / sub).resolve()):
            return real
    raise GuardrailError(f"refusing render input outside {', '.join(ALLOWED_MEDIA_SUBDIRS)}: {path}")


@dataclass
class Segment:
    clip: Path
    seconds: float
    still: bool = False                   # a composed photo frame (slow zoom) instead of video


@dataclass
class Plan:
    segments: list[Segment]
    voice: Path
    subs_list: Path                       # ffconcat list of subtitle PNGs
    subs_top: int
    endcard: Path
    endcard_seconds: float
    out: Path
    music: Path | None = None
    extra: dict = field(default_factory=dict)

    @property
    def total(self) -> float:
        return round(sum(s.seconds for s in self.segments) + self.endcard_seconds, 3)


def endcard(brand: dict, out: Path, renderer: Renderer | None = None) -> Path:
    """Placeholder brand card: channel name + follow prompt on a dark background."""
    r = renderer or Renderer()
    img = Image.new("RGB", (W, H), (18, 22, 34))
    d = ImageDraw.Draw(img)
    name = r.glyphs(brand.get("name") or brand["id"])
    big = r.ar.font_variant(size=220)
    d.text((W / 2, H * 0.42), name, font=big, fill=(255, 212, 0), anchor="mm")
    d.text((W / 2, H * 0.56), r.glyphs("تابعنا للمزيد"), font=r.ar.font_variant(size=90),
           fill=(255, 255, 255), anchor="mm")
    img.save(out)
    return out


def segments_for(beats: list[dict], clips_per_beat: list[list[Path]], voice_seconds: float) -> list[Segment]:
    """Each beat's footage runs from its start to the next beat's start (first from 0, last to the
    end of the voice), split evenly across that beat's clips. Image files become stills."""
    segs = []
    for i, (beat, clips) in enumerate(zip(beats, clips_per_beat)):
        start = 0.0 if i == 0 else beat["start"]
        end = beats[i + 1]["start"] if i + 1 < len(beats) else voice_seconds
        span = max(end - start, 0.5)
        for c in clips:
            segs.append(Segment(c, round(span / len(clips), 3), still=Path(c).suffix.lower() in STILL_EXT))
    return segs


def command(cfg: Config, plan: Plan) -> list[str]:
    ffmpeg = cfg.secret("FFMPEG_BIN", "ffmpeg")
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    n = 0
    for seg in plan.segments:
        if seg.still:
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", str(seg.seconds), "-i", str(guard(cfg, seg.clip))]
            filters.append(f"[{n}:v]scale={W}:{H},zoompan=z='min(1+0.0008*on,1.06)':x='iw/2-(iw/zoom/2)':"
                           f"y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},trim=duration={seg.seconds},"
                           f"setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v{n}]")
        else:
            inputs += ["-stream_loop", "-1", "-i", str(guard(cfg, seg.clip))]
            filters.append(f"[{n}:v]trim=duration={seg.seconds},setpts=PTS-STARTPTS,fps={FPS},"
                           f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
                           f"format=yuv420p[v{n}]")
        labels.append(f"[v{n}]")
        n += 1
    inputs += ["-loop", "1", "-t", str(plan.endcard_seconds), "-i", str(guard(cfg, plan.endcard))]
    filters.append(f"[{n}:v]fps={FPS},scale={W}:{H},setsar=1,format=yuv420p,"
                   f"trim=duration={plan.endcard_seconds}[v{n}]")
    labels.append(f"[v{n}]")
    n += 1
    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[bg]")

    guard(cfg, plan.subs_list.parent)
    inputs += ["-f", "concat", "-safe", "0", "-i", str(plan.subs_list)]
    filters.append(f"[{n}:v]format=rgba[subs]")
    filters.append(f"[bg][subs]overlay=0:{plan.subs_top}:eof_action=pass,format=yuv420p[vout]")
    n += 1

    inputs += ["-i", str(guard(cfg, plan.voice))]
    voice_idx = n
    n += 1
    fade_at = max(plan.total - 1.5, 0)
    if plan.music:
        inputs += ["-stream_loop", "-1", "-i", str(guard(cfg, plan.music))]
        filters.append(f"[{voice_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[vo][sc]")
        filters.append(f"[{n}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=0.35[mus]")
        filters.append("[mus][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck]")
        filters.append(f"[vo][duck]amix=inputs=2:duration=longest:normalize=0,"
                       f"atrim=duration={plan.total},afade=t=out:st={fade_at}:d=1.5[aout]")
    else:
        filters.append(f"[{voice_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                       f"apad=whole_dur={plan.total}[aout]")

    return [ffmpeg, "-hide_banner", "-nostats", "-y", *inputs,
            "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
            "-t", str(plan.total), "-r", str(FPS),
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(plan.out)]


def render(cfg: Config, plan: Plan, run: RunCmd = run_cmd) -> Path:
    if plan.total > cfg.get("video.max_seconds", 60):
        raise RenderError(f"planned video is {plan.total:.1f}s > video.max_seconds")
    cmd = command(cfg, plan)
    part = plan.out.with_name(plan.out.stem + ".part.mp4")
    cmd[-1] = str(part)
    proc = run(cmd)
    if proc.returncode != 0 or not part.exists():
        part.unlink(missing_ok=True)
        raise RenderError(f"ffmpeg failed: {(proc.stderr or '').strip()[-400:]}")
    part.rename(plan.out)
    return plan.out
