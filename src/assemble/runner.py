"""assemble: turn each voiced script into a finished vertical video.

Per video: stock clips per beat (Pexels/Pixabay, cached in assets/stock) → subtitle PNGs from the
voice word timings → end card → ffmpeg render to assets/generated/video/<video_id>.mp4, plus an
.srt of the same captions. The videos row gets video_path, subtitle_path, broll_manifest and
status 'rendered'. No usable clips for a beat → 'failed'; network/ffmpeg trouble only counts
notes.attempts, so the next run retries — until pipeline.max_attempts, then 'failed' (A6; each
retry re-downloads 50–80 MB of stock). Without a stock key the stage stops before touching anything.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from src.assemble import brand, broll, portrait, render, subtitles
from src import db
from src.config import Config
from src.discover.common import make_client

log = logging.getLogger("raij.assemble")

MUSIC_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".flac"}


# A video row plus what assemble_video needs from its script and story.
VIDEO_SELECT = ("SELECT v.*, x.beats, x.brand_id, x.notes AS script_notes, c.category FROM videos v "
                "JOIN scripts x ON x.id = v.script_id LEFT JOIN stories s ON s.id = x.story_id "
                "LEFT JOIN candidates c ON c.id = s.candidate_id")


def _pending(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(f"{VIDEO_SELECT} WHERE v.status = 'voiced' ORDER BY v.id").fetchall()
    return [dict(r) for r in rows]


def _music(cfg: Config, video_id: int) -> Path | None:
    tracks = sorted(p for p in (cfg.root / "assets" / "music").glob("*") if p.suffix.lower() in MUSIC_EXT)
    return tracks[video_id % len(tracks)].relative_to(cfg.root) if tracks else None


def _recent_clips(conn: sqlite3.Connection, days: int = 7) -> set[str]:
    """provider:id of clips in videos rendered recently, so consecutive videos don't look alike."""
    rows = conn.execute(
        "SELECT broll_manifest FROM videos WHERE broll_manifest IS NOT NULL AND created_at >= datetime('now', ?)",
        (f"-{int(days)} days",),
    ).fetchall()
    return {f"{m['provider']}:{m['id']}" for r in rows for m in json.loads(r[0])}


def prune_stock(cfg: Config, conn: sqlite3.Connection, keep_days: int) -> int:
    """Delete cached stock files no video from the last keep_days (or still pending) refers to."""
    rows = conn.execute(
        "SELECT broll_manifest FROM videos WHERE broll_manifest IS NOT NULL "
        "AND (created_at >= datetime('now', ?) OR status NOT IN ('rendered', 'failed'))",
        (f"-{int(keep_days)} days",),
    ).fetchall()
    keep = {Path(m["path"]).name for r in rows for m in json.loads(r[0])}
    removed = 0
    for f in (cfg.root / "assets" / "stock").glob("*"):
        if f.suffix in (".mp4", ".jpg") and f.name not in keep:
            f.unlink()
            removed += 1
    return removed


def _brand(cfg: Config, brand_id: str) -> dict[str, Any]:
    return next((b for b in cfg.brands if b["id"] == brand_id), {"id": brand_id})


def assemble_video(cfg: Config, video: dict[str, Any], client: httpx.Client,
                   run: render.RunCmd = render.run_cmd, recent: set[str] | None = None,
                   exclude: set[str] | None = None) -> dict[str, Any]:
    """`exclude` (provider:id) clips are never picked — used by review's "new b-roll"."""
    beats_text = json.loads(video["beats"])
    timing = json.loads((cfg.root / video["voice_path"]).with_suffix(".words.json").read_text(encoding="utf-8"))
    spans, voice_s = timing["beats"], timing["duration"]
    endcard_s = cfg.get("video.endcard_seconds", 2.0)

    out_dir = cfg.root / "assets" / "generated" / "video"
    work = out_dir / str(video["id"])
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    renderer = subtitles.Renderer()

    used: set[str] = set(exclude or ())
    photos: dict[str, portrait.Photo | None] = {}
    manifest, clips_per_beat, credits = [], [], []
    for i, (beat, span) in enumerate(zip(beats_text, spans)):
        start = 0.0 if i == 0 else span["start"]
        end = spans[i + 1]["start"] if i + 1 < len(spans) else voice_s
        n = broll.clips_needed(end - start)
        paths: list[Path] = []
        # A beat about a public figure opens on their licensed photo; the rest is faceless stock.
        person = beat.get("person")
        if person and person not in photos:
            photos[person] = portrait.find(cfg, client, person)
        photo = photos.get(person) if person else None
        if photo:
            frame = portrait.compose(cfg.root / photo.path, photo.credit, work / f"photo_{i}.jpg")
            paths.append(frame.relative_to(cfg.root))
            manifest.append({"provider": "wikimedia", "id": photo.file, "page": photo.page, "author": photo.author,
                             "license": photo.license, "credit": photo.credit, "person": person,
                             "path": photo.path, "beat": i, "at": round(start, 3)})
            if photo.credit not in credits:
                credits.append(photo.credit)
        stock: list[broll.Clip] = []
        if not photo or n > 1:
            need = (end - start) * (n - 1) / n if photo else end - start
            stock = broll.choose(cfg, client, beat["broll_keywords"], need=need, used=used, recent=recent)
            for clip in stock:
                broll.download(cfg, client, clip)
                paths.append(Path(clip.path))
        for clip in stock:
            manifest.append(broll.manifest_entry(clip, i, start, (end - start) / len(paths)))
        clips_per_beat.append(paths)

    segments = render.segments_for(spans, clips_per_beat, voice_s)
    total = sum(s.seconds for s in segments) + endcard_s
    look = _brand(cfg, video["brand_id"])
    script_notes = json.loads(video.get("script_notes") or "{}")
    series = brand.series_name(look, video.get("category"), script_notes.get("series"))
    title = script_notes.get("hook_title")
    hook_s = float(cfg.get("video.hook_title_seconds", 2.5)) if title else 0.0
    subs_list = subtitles.render_sequence(timing["words"], spans, work, total, renderer, hide_until=hook_s)
    card = brand.endcard(look, work / "endcard.png", series)
    music = _music(cfg, video["id"])
    plan = render.Plan(segments, Path(video["voice_path"]), subs_list, renderer.style.top, card, endcard_s,
                       out_dir / f"{video['id']}.mp4", music=music,
                       hook_list=brand.hook_sequence(title, series, work, hook_s) if title else None,
                       logo=brand.logo_layer(look, work / "logo.png"),
                       transition=float(cfg.get("video.transition_seconds", 0.3)),
                       progress_bar=bool(cfg.get("video.progress_bar", True)))
    render.render(cfg, plan, run=run)

    srt_path = out_dir / f"{video['id']}.srt"
    srt_path.write_text(subtitles.srt(timing["words"], spans, renderer), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return {"video_path": str(plan.out.relative_to(cfg.root)), "subtitle_path": str(srt_path.relative_to(cfg.root)),
            "duration_s": plan.total, "manifest": manifest,
            "notes": {"voice_s": voice_s, "music": str(music) if music else None, "clips": len(manifest),
                      "credits": credits, "hook_title": title, "series": series}}


def _fail(conn: sqlite3.Connection, video: dict[str, Any], reason: str) -> None:
    notes = {**json.loads(video.get("notes") or "{}"), "reason": reason}
    conn.execute("UPDATE videos SET status = 'failed', notes = ? WHERE id = ?",
                 (json.dumps(notes, ensure_ascii=False), video["id"]))
    conn.commit()


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

    db.expire_stale(conn, cfg.get("pipeline.max_age_days", 2))
    max_attempts = int(cfg.get("pipeline.max_attempts", 3))
    pending = _pending(conn)
    run_id = db.start_run(conn, "assemble")
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
                row = assemble_video(cfg, v, client, run=run, recent=_recent_clips(conn))
            except (broll.BrollError, render.GuardrailError) as exc:
                log.warning("Video %d: %s", v["id"], exc)
                _fail(conn, v, str(exc))
                failed.append({"video_id": v["id"], "error": str(exc)})
                continue
            except Exception as exc:                    # network, ffmpeg, stock outage: retry next run
                msg = f"{type(exc).__name__}: {exc}"
                notes = json.loads(v.get("notes") or "{}")
                notes["attempts"] = int(notes.get("attempts") or 0) + 1
                if notes["attempts"] >= max_attempts:
                    log.warning("Video %d: assemble failed %d times — giving up: %s", v["id"], max_attempts, msg)
                    _fail(conn, {**v, "notes": json.dumps(notes)}, f"after {max_attempts} attempts: {msg}")
                    failed.append({"video_id": v["id"], "error": msg})
                    continue
                conn.execute("UPDATE videos SET notes = ? WHERE id = ?", (json.dumps(notes, ensure_ascii=False), v["id"]))
                conn.commit()
                log.error("Video %d: assemble failed (attempt %d/%d), will retry next run: %s", v["id"],
                          notes["attempts"], max_attempts, msg)
                retry.append({"video_id": v["id"], "error": msg})
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

    if rendered:
        removed = prune_stock(cfg, conn, cfg.get("video.stock_keep_days", 14))
        if removed:
            log.info("Pruned %d cached stock clip(s) no recent video uses", removed)
    total = len(rendered) + len(failed) + len(retry)
    status = "ok" if len(rendered) == total else ("partial" if rendered else "failed")
    notes = {"pending": total, "rendered": len(rendered), "failed": failed, "retry": retry}
    db.finish_run(conn, run_id, status, notes)
    level = logging.INFO if status == "ok" else logging.WARNING
    log.log(level, "Assemble %s: %d/%d rendered, %d failed, %d to retry", status, len(rendered), total,
            len(failed), len(retry))
    return 1 if status == "failed" else 0
