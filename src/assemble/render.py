"""Render the final video (1080×1920 Shorts or 1920×1080 long-form, `Plan.width/height`) with ffmpeg: b-roll
segments joined by xfade transitions → end card,
then overlays (subtitles, animated hook title, channel logo, progress bar); voice plus optional CC0
music ducked under it.

Guardrail: every media input is resolved (symlinks followed) and must sit inside
config.ALLOWED_MEDIA_SUBDIRS under the repo root — licensed stock, our generated assets, CC0
music. Anything else (e.g. a source video) makes the render refuse to start.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from src.config import ALLOWED_MEDIA_SUBDIRS, Config

RunCmd = Callable[[list[str]], subprocess.CompletedProcess]
W, H, FPS = 1080, 1920, 30                # the Shorts frame; long videos pass their own size in the Plan
STILL_EXT = {".jpg", ".jpeg", ".png"}
# Varied per cut, in order; the cut into the end card is always a plain fade.
LOGO_XY = (48, 150)                       # top-left, below the platforms' top bar
TRANSITIONS = ("smoothleft", "fade", "slideup", "zoomin", "smoothright", "circleopen", "slidedown", "fadeblack")


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
    hook_list: Path | None = None         # ffconcat list of the animated hook title (full-frame PNGs)
    logo: Path | None = None              # full-frame PNG with the channel logo, hidden on the end card
    transition: float = 0.3               # seconds of overlap per cut; 0 = hard cuts
    progress_bar: bool = True
    width: int = W
    height: int = H
    max_seconds: float = 60.0             # the format's cap (formats.Format.max_seconds)
    overlays: list[Path] = field(default_factory=list)   # more full-frame ffconcat PNG lists (chapter cards)
    logo_xy: tuple[int, int] = LOGO_XY
    extra: dict = field(default_factory=dict)

    @property
    def total(self) -> float:
        return round(sum(s.seconds for s in self.segments) + self.endcard_seconds, 3)


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
    W, H = plan.width, plan.height        # noqa: N806 — frame size of this render
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    n = 0
    d = plan.transition if len(plan.segments) and plan.transition > 0 else 0.0
    for seg in plan.segments:
        # Each segment runs `d` longer: the next one fades in over that tail, so cuts stay on the beat.
        dur = round(seg.seconds + d, 3)
        if seg.still:
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", str(dur), "-i", str(guard(cfg, seg.clip))]
            filters.append(f"[{n}:v]scale={W}:{H},zoompan=z='min(1+0.0008*on,1.06)':x='iw/2-(iw/zoom/2)':"
                           f"y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},trim=duration={dur},"
                           f"setpts=PTS-STARTPTS,settb=AVTB,setsar=1,format=yuv420p[v{n}]")
        else:
            inputs += ["-stream_loop", "-1", "-i", str(guard(cfg, seg.clip))]
            filters.append(f"[{n}:v]trim=duration={dur},setpts=PTS-STARTPTS,fps={FPS},"
                           f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
                           f"settb=AVTB,format=yuv420p[v{n}]")
        labels.append(f"[v{n}]")
        n += 1
    inputs += ["-loop", "1", "-t", str(plan.endcard_seconds), "-i", str(guard(cfg, plan.endcard))]
    filters.append(f"[{n}:v]fps={FPS},scale={W}:{H},setsar=1,format=yuv420p,"
                   f"trim=duration={plan.endcard_seconds},setpts=PTS-STARTPTS,settb=AVTB[v{n}]")
    labels.append(f"[v{n}]")
    n += 1
    if d:
        acc, offset = labels[0], 0.0
        for k, (seg, nxt) in enumerate(zip(plan.segments, labels[1:]), 1):
            offset += seg.seconds
            kind = "fade" if k == len(plan.segments) else TRANSITIONS[(k - 1) % len(TRANSITIONS)]
            out = "[bg]" if k == len(plan.segments) else f"[x{k}]"
            filters.append(f"{acc}{nxt}xfade=transition={kind}:duration={d}:offset={offset:.3f}{out}")
            acc = out
    else:
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[bg]")

    guard(cfg, plan.subs_list.parent)
    inputs += ["-f", "concat", "-safe", "0", "-i", str(plan.subs_list)]
    filters.append(f"[{n}:v]format=rgba[subs]")
    filters.append(f"[bg][subs]overlay=0:{plan.subs_top}:eof_action=pass[o0]")
    n += 1
    top = "[o0]"
    body_end = round(plan.total - plan.endcard_seconds, 3)
    if plan.hook_list:
        guard(cfg, plan.hook_list.parent)
        inputs += ["-f", "concat", "-safe", "0", "-i", str(plan.hook_list)]
        filters.append(f"[{n}:v]format=rgba[hook]")
        filters.append(f"{top}[hook]overlay=0:0:eof_action=pass[o1]")
        n, top = n + 1, "[o1]"
    for k, lst in enumerate(plan.overlays):
        guard(cfg, lst.parent)
        inputs += ["-f", "concat", "-safe", "0", "-i", str(lst)]
        filters.append(f"[{n}:v]format=rgba[ov{k}]")
        filters.append(f"{top}[ov{k}]overlay=0:0:eof_action=pass[ox{k}]")
        n, top = n + 1, f"[ox{k}]"
    if plan.logo:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", str(plan.total), "-i", str(guard(cfg, plan.logo))]
        filters.append(f"[{n}:v]format=rgba[logo]")
        filters.append(f"{top}[logo]overlay={plan.logo_xy[0]}:{plan.logo_xy[1]}:enable='lt(t,{body_end})'[o2]")
        n, top = n + 1, "[o2]"
    if plan.progress_bar:
        filters.append(f"color=c=0xFFD400:s={W}x10:r={FPS}:d={plan.total}[bar]")
        filters.append(f"{top}[bar]overlay=x='-w+w*t/{body_end}':y=0:enable='lt(t,{body_end})'[o3]")
        top = "[o3]"
    filters.append(f"{top}format=yuv420p[vout]")

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
    if plan.total > plan.max_seconds:
        raise RenderError(f"planned video is {plan.total:.1f}s > the format's {plan.max_seconds:.0f}s cap")
    cmd = command(cfg, plan)
    part = plan.out.with_name(plan.out.stem + ".part.mp4")
    cmd[-1] = str(part)
    proc = run(cmd)
    if proc.returncode != 0 or not part.exists():
        part.unlink(missing_ok=True)
        raise RenderError(f"ffmpeg failed: {(proc.stderr or '').strip()[-400:]}")
    part.rename(plan.out)
    return plan.out
