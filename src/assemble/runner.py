"""assemble: turn each voiced script into a finished vertical video.

Per video: stock clips per beat (Pexels/Pixabay, cached in assets/stock) → subtitle PNGs from the
voice word timings → end card → ffmpeg render to assets/generated/video/<video_id>.mp4, plus an
.srt of the same captions. The videos row gets video_path, subtitle_path, broll_manifest and
status 'rendered'. No usable clips for a beat → 'failed'; network/ffmpeg trouble writes nothing,
so the next run retries. Without a stock key the stage stops before touching anything.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from src.assemble import broll, render, subtitles
from src.config import Config
from src.discover.common import make_client

log = logging.getLogger("raij.assemble")

MUSIC_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".flac"}


def _pending(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT v.*, x.beats, x.brand_id FROM videos v JOIN scripts x ON x.id = v.script_id "
        "WHERE v.status = 'voiced' ORDER BY v.id"
    ).fetchall()
    return [dict(r) for r in rows]


def _music(cfg: Config, video_id: int) -> Path | None:
    tracks = sorted(p for p in (cfg.root / "assets" / "music").glob("*") if p.suffix.lower() in MUSIC_EXT)
    return tracks[video_id % len(tracks)].relative_to(cfg.root) if tracks else None


def _brand(cfg: Config, brand_id: str) -> dict[str, Any]:
    return next((b for b in cfg.brands if b["id"] == brand_id), {"id": brand_id})


def assemble_video(cfg: Config, video: dict[str, Any], client: httpx.Client,
                   run: render.RunCmd = render.run_cmd) -> dict[str, Any]:
    beats_text = json.loads(video["beats"])
    timing = json.loads((cfg.root / video["voice_path"]).with_suffix(".words.json").read_text(encoding="utf-8"))
    spans, voice_s = timing["beats"], timing["duration"]
    endcard_s = cfg.get("video.endcard_seconds", 2.0)

    used: set[str] = set()
    manifest, clips_per_beat = [], []
    for i, (beat, span) in enumerate(zip(beats_text, spans)):
        start = 0.0 if i == 0 else span["start"]
        end = spans[i + 1]["start"] if i + 1 < len(spans) else voice_s
        chosen = broll.choose(cfg, client, beat["broll_keywords"], need=end - start, used=used)
        paths = []
        for clip in chosen:
            broll.download(cfg, client, clip)
            paths.append(Path(clip.path))
            manifest.append(broll.manifest_entry(clip, i, start, (end - start) / len(chosen)))
        clips_per_beat.append(paths)

    out_dir = cfg.root / "assets" / "generated" / "video"
    work = out_dir / str(video["id"])
    if work.exists():
        shutil.rmtree(work)
    segments = render.segments_for(spans, clips_per_beat, voice_s)
    total = sum(s.seconds for s in segments) + endcard_s
    renderer = subtitles.Renderer()
    subs_list = subtitles.render_sequence(timing["words"], spans, work, total, renderer)
    card = render.endcard(_brand(cfg, video["brand_id"]), work / "endcard.png", renderer)
    music = _music(cfg, video["id"])
    plan = render.Plan(segments, Path(video["voice_path"]), subs_list, renderer.style.top, card, endcard_s,
                       out_dir / f"{video['id']}.mp4", music=music)
    render.render(cfg, plan, run=run)

    srt_path = out_dir / f"{video['id']}.srt"
    srt_path.write_text(subtitles.srt(timing["words"], spans, renderer), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return {"video_path": str(plan.out.relative_to(cfg.root)), "subtitle_path": str(srt_path.relative_to(cfg.root)),
            "duration_s": plan.total, "manifest": manifest,
            "notes": {"voice_s": voice_s, "music": str(music) if music else None, "clips": len(manifest)}}


def assemble(cfg: Config, conn: sqlite3.Connection, dry_run: bool = False, client: httpx.Client | None = None,
             run: render.RunCmd = render.run_cmd) -> int:
    pending = _pending(conn)
    keys = [name for name, _ in broll.providers(cfg)]
    if dry_run:
        log.info("[dry run] %d voiced video(s) to assemble; stock providers: %s; music tracks: %s",
                 len(pending), ", ".join(keys) or "NONE (set PEXELS_API_KEY or PIXABAY_API_KEY)",
                 "yes" if _music(cfg, 0) else "none (voice only)")
        for v in pending:
            log.info("[dry run] video %d (script %d): %d beats", v["id"], v["script_id"], len(json.loads(v["beats"])))
        log.info("[dry run] nothing searched, downloaded or rendered")
        return 0

    run_id = conn.execute("INSERT INTO runs (command) VALUES ('assemble')").lastrowid
    conn.commit()
    rendered, failed, retry = [], [], []
    if pending and not keys:
        log.error("No stock footage key — set PEXELS_API_KEY or PIXABAY_API_KEY in .env (see SETUP.md)")
        retry = [{"video_id": v["id"], "error": "no stock footage key"} for v in pending]
        pending = []
    own_client = client is None
    client = client or make_client()
    try:
        for v in pending:
            try:
                row = assemble_video(cfg, v, client, run=run)
            except (broll.BrollError, render.GuardrailError) as exc:
                log.warning("Video %d: %s", v["id"], exc)
                notes = {**json.loads(v.get("notes") or "{}"), "reason": str(exc)}
                conn.execute("UPDATE videos SET status = 'failed', notes = ? WHERE id = ?",
                             (json.dumps(notes, ensure_ascii=False), v["id"]))
                conn.commit()
                failed.append({"video_id": v["id"], "error": str(exc)})
                continue
            except Exception as exc:                    # network, ffmpeg: retry next run
                log.error("Video %d: assemble failed, will retry next run: %s", v["id"], exc)
                retry.append({"video_id": v["id"], "error": f"{type(exc).__name__}: {exc}"})
                continue
            notes = {**json.loads(v.get("notes") or "{}"), **row["notes"]}
            conn.execute(
                "UPDATE videos SET video_path = ?, subtitle_path = ?, broll_manifest = ?, duration_s = ?, "
                "status = 'rendered', notes = ? WHERE id = ?",
                (row["video_path"], row["subtitle_path"], json.dumps(row["manifest"], ensure_ascii=False),
                 row["duration_s"], json.dumps(notes, ensure_ascii=False), v["id"]),
            )
            conn.commit()
            rendered.append(v["id"])
            log.info("Video %d → %s (%.1fs, %d clips, music: %s)", v["id"], row["video_path"], row["duration_s"],
                     row["notes"]["clips"], row["notes"]["music"] or "none")
    finally:
        if own_client:
            client.close()

    total = len(rendered) + len(failed) + len(retry)
    status = "ok" if len(rendered) == total else ("partial" if rendered else "failed")
    notes = {"pending": total, "rendered": len(rendered), "failed": failed, "retry": retry}
    conn.execute("UPDATE runs SET finished_at = datetime('now'), status = ?, notes = ? WHERE id = ?",
                 (status, json.dumps(notes, ensure_ascii=False), run_id))
    conn.commit()
    level = logging.INFO if status == "ok" else logging.WARNING
    log.log(level, "Assemble %s: %d/%d rendered, %d failed, %d to retry", status, len(rendered), total,
            len(failed), len(retry))
    return 1 if status == "failed" else 0
