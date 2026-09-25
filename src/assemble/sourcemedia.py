"""Fetch and frame the story's real pictures and footage (Phase 20) for the render.

`stories.media` (found by extract.media) lists where the items are; this turns them into render-ready files
under assets/source/<story_id>/ — cached, so a regeneration ("new b-roll") never downloads twice:

* images → JPEG checked with Pillow (short side ≥ `media.min_image_px`, sane aspect), then composed full-frame
  like the Commons portraits: the picture uncropped over a blurred, darkened fill of itself with a small
  "Source: domain" credit near the top (`portrait.compose`);
* direct videos (mp4/webm/m3u8 in the page) → a `media.clip_seconds` cut straight from the URL by ffmpeg;
* YouTube → the video at ≤720p via yt-dlp (needs a current yt-dlp: 2026.07 got 403s, 2026.08 works), then up to
  `media.clips_per_video` cuts at 20/45/70 % of its length; the full download is deleted.
Clips are re-encoded into the frame: same orientation → fill (crop); other orientation → contained over the
blurred fill, so a 16:9 news clip in a Short shows whole. Credit is burnt in on clips too (a PNG overlay).
Every failure skips that item; the video falls back to stock for the rest.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
from PIL import Image, ImageOps

from src.assemble import brand, portrait
from src.config import Config
from src.discover.common import FetchError, request

log = logging.getLogger("raij.assemble")

RunCmd = Callable[[list[str]], subprocess.CompletedProcess]
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
MAX_IMAGE_BYTES = 15 * 2**20
YT_FORMAT = "bv*[height<=720][ext=mp4]/bv*[height<=720]/b[height<=720]/b"
POSITIONS = (0.2, 0.45, 0.7)          # where the cuts of a long source video are taken
FPS = 30


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


@dataclass
class Item:
    kind: str                 # image | video | youtube
    url: str
    page: str
    source: str               # domain, for the credit
    id: str                   # stable file id (hash of the URL, plus the cut index)
    path: str = ""            # repo-relative, render-ready file
    still: bool = False
    duration: float = 0.0
    title: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def credit(self) -> str:
        return f"Source: {self.source}"


def _hid(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:10]


def _probe(cfg: Config, src: str, run: RunCmd) -> tuple[int, int, float]:
    """(width, height, seconds) of a video file or URL via ffprobe; zeros when unreadable."""
    ffprobe = cfg.secret("FFPROBE_BIN", "ffprobe")
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
           "-of", "json"]
    if src.startswith("http"):
        cmd += ["-user_agent", BROWSER_UA]
    proc = run(cmd + [src])
    try:
        data = json.loads(proc.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        dur = float((data.get("format") or {}).get("duration") or 0)
        return int(stream.get("width") or 0), int(stream.get("height") or 0), dur
    except (ValueError, IndexError, TypeError):
        return 0, 0, 0.0


def _credit_png(item: Item, work: Path) -> Path:
    path = work / f"credit_{item.id}.png"
    if not path.exists():
        work.mkdir(parents=True, exist_ok=True)
        brand.text_image(item.credit, 26, stroke=2).save(path)
    return path


def cut_command(cfg: Config, src: str, out: Path, start: float, seconds: float, frame: tuple[int, int],
                src_size: tuple[int, int], credit_png: Path | None) -> list[str]:
    """ffmpeg command: `seconds` from `start` of `src`, framed to `frame`, silent, with the credit overlaid."""
    ffmpeg = cfg.secret("FFMPEG_BIN", "ffmpeg")
    W, H = frame                                                       # noqa: N806
    sw, sh = src_size
    portrait_frame = H > W
    same = (sh > sw) == portrait_frame if sw and sh else True
    if same:
        fit = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    else:
        fg = f"scale={W}:-2" if portrait_frame else f"scale=-2:{int(H * 0.92) // 2 * 2}"
        fit = (f"split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
               f"boxblur=luma_radius=40:luma_power=3:chroma_radius=20:chroma_power=2,eq=brightness=-0.12[bg];"
               f"[b]{fg}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    chain = f"[0:v]fps={FPS},{fit},setsar=1,format=yuv420p[v0]"
    inputs = []
    if src.startswith("http"):
        inputs += ["-user_agent", BROWSER_UA]
    inputs += ["-ss", f"{start:.2f}", "-t", f"{seconds:.2f}", "-i", src]
    if credit_png:
        inputs += ["-i", str(credit_png)]
        y = 88 if portrait_frame else 46
        chain += f";[v0][1:v]overlay=(W-w)/2:{y}[v]"
    else:
        chain = chain.replace("[v0]", "[v]")
    return [ffmpeg, "-hide_banner", "-nostats", "-y", *inputs, "-filter_complex", chain, "-map", "[v]", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out)]


def _cut(cfg: Config, src: str, out: Path, start: float, seconds: float, frame: tuple[int, int],
         size: tuple[int, int], credit_png: Path | None, run: RunCmd) -> bool:
    part = out.with_name(out.stem + ".part.mp4")
    proc = run(cut_command(cfg, src, part, start, seconds, frame, size, credit_png))
    if proc.returncode != 0 or not part.exists() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        log.info("source clip cut failed (%s): %s", src[:80], (proc.stderr or "").strip()[-200:])
        return False
    part.rename(out)
    return True


# --- per kind -----------------------------------------------------------------------

def fetch_image(cfg: Config, client: httpx.Client, item: Item, dest: Path) -> Path | None:
    """Download + validate one picture → dest/img_<id>.jpg (cached), or None when it isn't a usable photo."""
    out = dest / f"img_{item.id}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return out
    skip = dest / f"skip_{item.id}"                       # remembered rejects: a regeneration never refetches them
    if skip.exists():
        return None
    dest.mkdir(parents=True, exist_ok=True)
    min_px = int(cfg.get("media.min_image_px", 500))
    headers = {"User-Agent": BROWSER_UA, "Referer": item.page or item.url, "Accept": "image/*,*/*;q=0.8"}
    resp = request(client, "GET", item.url, retries=1, headers=headers, follow_redirects=True)
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype and not ctype.startswith("image/"):
        log.info("source image %s is %s, skipped", item.url[:80], ctype)
        skip.touch()
        return None
    if len(resp.content) > MAX_IMAGE_BYTES:
        skip.touch()
        return None
    try:
        with Image.open(io.BytesIO(resp.content)) as raw:
            img = ImageOps.exif_transpose(raw).convert("RGB")
    except Exception as exc:                                    # not an image after all
        log.info("source image %s unreadable: %s", item.url[:80], exc)
        skip.touch()
        return None
    w, h = img.size
    if min(w, h) < min_px or not (0.35 <= w / h <= 3.0):
        log.info("source image %s is %dx%d, skipped", item.url[:80], w, h)
        skip.touch()
        return None
    if max(w, h) > 2400:
        img.thumbnail((2400, 2400), Image.LANCZOS)
    img.save(out, "JPEG", quality=90)
    return out


def frame_image(item: Item, src: Path, dest: Path, frame: tuple[int, int]) -> Path:
    """The picture composed full-frame with its credit (cached per frame size)."""
    out = dest / f"frame_{frame[0]}x{frame[1]}_{item.id}.jpg"
    if not (out.exists() and out.stat().st_size > 0):
        portrait.compose(src, item.credit, out, frame=frame)
    return out


def fetch_video(cfg: Config, item: Item, dest: Path, frame: tuple[int, int], work: Path, run: RunCmd) -> list[Path]:
    """Cuts of a direct video URL, straight from the network (ffmpeg reads http/m3u8 itself)."""
    seconds = float(cfg.get("media.clip_seconds", 10))
    w, h, dur = _probe(cfg, item.url, run)
    if not dur:
        dur = seconds * 2
    starts = _starts(dur, seconds, int(cfg.get("media.clips_per_video", 3)))
    credit = _credit_png(item, work)
    out: list[Path] = []
    for k, start in enumerate(starts):
        path = dest / f"vid_{item.id}_{k}.mp4"
        if (path.exists() and path.stat().st_size > 0) or _cut(cfg, item.url, path, start, min(seconds, dur - start),
                                                               frame, (w, h), credit, run):
            out.append(path)
    return out


def _starts(dur: float, seconds: float, per_video: int) -> list[float]:
    if dur <= seconds * 1.5:
        return [0.0]
    if dur <= seconds * 4:
        return [round(dur * 0.15, 2)]
    return [round(dur * p, 2) for p in POSITIONS[:per_video] if dur * p + seconds <= dur]


def fetch_youtube(cfg: Config, item: Item, dest: Path, frame: tuple[int, int], work: Path, run: RunCmd) -> list[Path]:
    """Cuts of a YouTube video: full download at ≤720p into the work dir, cut, delete."""
    per_video = int(cfg.get("media.clips_per_video", 3))
    existing = sorted(p for p in dest.glob(f"yt_{item.id}_*.mp4") if p.stat().st_size > 0)
    if existing:
        return existing[:per_video]
    if not shutil.which("yt-dlp") and run is run_cmd:
        log.info("yt-dlp is not installed — no YouTube source clips")
        return []
    work.mkdir(parents=True, exist_ok=True)
    max_s = int(cfg.get("media.max_video_seconds", 900))
    stem = work / f"yt_{item.id}"
    proc = run(["yt-dlp", "-f", YT_FORMAT, "--no-playlist", "--no-warnings", "--match-filter", f"duration<={max_s}",
                "--socket-timeout", "30", "-o", f"{stem}.%(ext)s", item.url])
    files = sorted(work.glob(f"yt_{item.id}.*"))
    if proc.returncode != 0 or not files:
        log.info("YouTube download failed for %s: %s", item.url, (proc.stderr or "").strip()[-200:])
        for f in files:
            f.unlink(missing_ok=True)
        return []
    full = files[0]
    try:
        w, h, dur = _probe(cfg, str(full), run)
        seconds = float(cfg.get("media.clip_seconds", 10))
        credit = _credit_png(item, work)
        out: list[Path] = []
        for k, start in enumerate(_starts(dur or seconds, seconds, per_video)):
            path = dest / f"yt_{item.id}_{k}.mp4"
            if _cut(cfg, str(full), path, start, seconds, frame, (w, h), credit, run):
                out.append(path)
        return out
    finally:
        full.unlink(missing_ok=True)


# --- entry point --------------------------------------------------------------------

def prepare(cfg: Config, client: httpx.Client, media: list[dict[str, Any]], story_id: int, frame: tuple[int, int],
            work: Path, run: RunCmd = run_cmd, max_items: int | None = None) -> list[Item]:
    """Render-ready source items (paths repo-relative), in the order the assembler should use them:
    stills and clips interleaved so consecutive cuts differ. Never raises for one bad item."""
    if not media or not cfg.get("media.enabled", True):
        return []
    max_items = max_items or int(cfg.get("media.max_items", 10))
    dest = cfg.root / "assets" / "source" / str(story_id)
    dest.mkdir(parents=True, exist_ok=True)
    stills: list[Item] = []
    clips: list[Item] = []
    for raw in media:
        kind, url = str(raw.get("kind") or ""), str(raw.get("url") or "")
        if not url or kind not in ("image", "video", "youtube"):
            continue
        item = Item(kind, url, str(raw.get("page") or url), str(raw.get("source") or ""), _hid(url),
                    title=str(raw.get("title") or raw.get("alt") or ""), extra={k: v for k, v in raw.items()
                                                                                   if k not in ("kind", "url")})
        try:
            if kind == "image":
                src = fetch_image(cfg, client, item, dest)
                if src:
                    framed = frame_image(item, src, dest, frame)
                    item.path, item.still = str(framed.relative_to(cfg.root)), True
                    item.extra["photo"] = str(src.relative_to(cfg.root))
                    stills.append(item)
            else:
                paths = (fetch_youtube if kind == "youtube" else fetch_video)(cfg, item, dest, frame, work, run)
                for k, p in enumerate(paths):
                    w, h, dur = _probe(cfg, str(p), run)
                    clips.append(Item(kind, url, item.page, item.source, f"{item.id}_{k}", str(p.relative_to(cfg.root)),
                                      False, dur or float(cfg.get("media.clip_seconds", 10)), item.title, dict(item.extra)))
        except (FetchError, httpx.HTTPError, OSError, ValueError) as exc:
            log.info("source media %s skipped: %s", url[:80], exc)
        if len(stills) + len(clips) >= max_items:
            break
    # Interleave: a still, a clip, a still… so the video never runs three photos in a row when clips exist.
    out: list[Item] = []
    while (stills or clips) and len(out) < max_items:
        if stills:
            out.append(stills.pop(0))
        if clips and len(out) < max_items:
            out.append(clips.pop(0))
    return out


def manifest_entry(item: Item, beat: int, start: float, dur: float) -> dict[str, Any]:
    return {"provider": "source", "id": item.id, "kind": item.kind, "page": item.page, "author": item.source,
            "license": "source material (credited)", "credit": item.credit, "path": item.path, "still": item.still,
            "title": item.title, "beat": beat, "at": round(start, 3), "seconds": round(dur, 3)}
