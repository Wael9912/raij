"""assemble: turn each voiced script into a finished vertical video.

Per video: the story's real pictures/footage first (stories.media → assets/source, Phase 20), stock clips
to fill (Pexels/Pixabay, cached in assets/stock) → cover card → subtitle PNGs from the voice word timings →
end card → ffmpeg render with credited music to assets/generated/video/<video_id>.mp4, plus an .srt of the
same captions and a .cover.jpg. The videos row gets video_path, subtitle_path, broll_manifest and
status 'rendered'. No usable clips for a beat → 'failed'; ffmpeg trouble counts notes.attempts, so the
next run retries — until pipeline.max_attempts, then 'failed' (A6; each retry re-downloads 50–80 MB of
stock); a stock/network outage (BrollUnavailable, httpx transport errors) waits without counting.
Without a stock key the stage stops before touching anything. notes.timing = seconds per step.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from src.assemble import brand, broll, music, portrait, render, sourcemedia, subtitles, thumbnail
from src import db, formats
from src.config import Config
from src.discover.common import make_client

log = logging.getLogger("raij.assemble")



# A video row plus what assemble_video needs from its script and story.
VIDEO_SELECT = ("SELECT v.*, x.beats, x.brand_id, x.kind, x.notes AS script_notes, c.category, s.media, "
                "s.id AS story_id, s.sources, c.id AS candidate_id, c.source, c.title, c.canonical_url, c.raw_json, "
                "c.wanted FROM videos v "
                "JOIN scripts x ON x.id = v.script_id LEFT JOIN stories s ON s.id = x.story_id "
                "LEFT JOIN candidates c ON c.id = s.candidate_id")


def _pending(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(f"{VIDEO_SELECT} WHERE v.status = 'voiced' ORDER BY v.id").fetchall()
    return [dict(r) for r in rows]


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
    keep_stories = {Path(m["path"]).parts[2] for r in rows for m in json.loads(r[0])
                    if m.get("provider") == "source" and len(Path(m["path"]).parts) > 3}
    removed = 0
    for f in (cfg.root / "assets" / "stock").glob("*"):
        if f.suffix in (".mp4", ".jpg") and f.name not in keep:
            f.unlink()
            removed += 1
    for d in (cfg.root / "assets" / "source").glob("*"):     # fetched source media, per story (Phase 20)
        if d.is_dir() and d.name not in keep_stories:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def _brand(cfg: Config, brand_id: str) -> dict[str, Any]:
    return next((b for b in cfg.brands if b["id"] == brand_id), {"id": brand_id})


def assign_real(pool: list[sourcemedia.Item], slots: list[int], reuse: int = 1) -> list[list[sourcemedia.Item]]:
    """Spread the story's real media over the beats: every beat gets one while items last (the hook first),
    then second slots and so on. When the pool runs dry each item may come back up to `reuse` more times
    (a long video shows its few real pictures again rather than a stranger's stock clip); an item never repeats
    within one beat. Stock fills whatever is left (owner's rule, Phase 20)."""
    out: list[list[sourcemedia.Item]] = [[] for _ in slots]
    if not pool:
        return out
    queue = list(pool)
    refills = max(int(reuse), 0)
    for _round in range(max(slots, default=0)):
        for i, n in enumerate(slots):
            if not queue and refills:
                queue, refills = list(pool), refills - 1
            if queue and len(out[i]) < n:
                pick = next((it for it in queue if it not in out[i]), None)
                if pick is None:
                    continue
                queue.remove(pick)
                out[i].append(pick)
    return out


def ensure_media(cfg: Config, client: httpx.Client, video: dict[str, Any], conn: sqlite3.Connection | None = None,
                 run: sourcemedia.RunCmd = sourcemedia.run_cmd) -> None:
    """Stories extracted before Phase 20 have no `media` yet: look it up now (and store it when a connection is
    given), so the next render already shows real pictures. Never raises."""
    if video.get("media") is not None or not video.get("story_id") or not cfg.get("media.enabled", True):
        return
    from src.extract import media as extract_media
    row = {k: video.get(k) for k in ("id", "source", "title", "canonical_url", "raw_json", "wanted")}
    row["id"] = video.get("candidate_id")
    try:
        found = extract_media.collect(cfg, client, row, json.loads(video.get("sources") or "[]"), run=run)
    except Exception as exc:
        log.warning("Video %s: source media lookup failed: %s", video.get("id"), exc)
        return
    video["media"] = json.dumps(found, ensure_ascii=False)
    if conn is not None:
        conn.execute("UPDATE stories SET media = ? WHERE id = ?", (video["media"], video["story_id"]))
        conn.commit()


def assemble_video(cfg: Config, video: dict[str, Any], client: httpx.Client,
                   run: render.RunCmd = render.run_cmd, recent: set[str] | None = None,
                   exclude: set[str] | None = None) -> dict[str, Any]:
    """`exclude` (provider:id) clips are never picked — used by review's "new b-roll"."""
    beats_text = json.loads(video["beats"])
    timing = json.loads((cfg.root / video["voice_path"]).with_suffix(".words.json").read_text(encoding="utf-8"))
    fmt = formats.get(cfg, video.get("kind"))              # short: 1080×1920; long: 1920×1080 (Phase 15)
    # The cover card opens the video (Phase 20): everything voice-timed shifts by `lead`.
    lead = float(cfg.get("video.cover_seconds", 0.8))
    words = [{**w, "start": w["start"] + lead, "end": w["end"] + lead} for w in timing["words"]]
    spans = [{**b, "start": (b["start"] + lead) if b.get("start") is not None else None,
              "end": (b["end"] + lead) if b.get("end") is not None else None} for b in timing["beats"]]
    voice_s = timing["duration"] + lead
    endcard_s = cfg.get("video.endcard_seconds", 2.0)
    endcard_s = max(1.0, min(endcard_s, fmt.max_seconds - voice_s))   # the cover must not push a Short past 60 s

    out_dir = cfg.root / "assets" / "generated" / "video"
    work = out_dir / str(video["id"])
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    renderer = subtitles.Renderer(subtitles.Style.for_frame(*fmt.frame))
    look = _brand(cfg, video["brand_id"])
    script_notes = json.loads(video.get("script_notes") or "{}")
    series = brand.series_name(look, video.get("category"), script_notes.get("series"))
    title = script_notes.get("hook_title")

    used: set[str] = set(exclude or ())
    photos: dict[str, portrait.Photo | None] = {}
    manifest, clips_per_beat, credits = [], [], []
    timing_s: dict[str, float] = {}                        # seconds per step, for the log and notes.timing
    t0 = time.monotonic()
    # Real pictures/footage from the sources first (owner 2026-09-25), stock only to fill.
    ensure_media(cfg, client, video, run=run)               # stories from before Phase 20
    story_media = json.loads(video.get("media") or "[]") if video.get("media") else []
    real: list[sourcemedia.Item] = []
    if story_media and cfg.get("media.enabled", True):
        try:
            real = sourcemedia.prepare(cfg, client, story_media, int(video.get("story_id") or 0), fmt.frame,
                                       work / "source", run=run)
        except Exception as exc:                            # never lose the video over its source media
            log.warning("Video %d: source media unavailable, stock only: %s", video["id"], exc)
    slots: list[int] = []
    starts: list[tuple[float, float]] = []
    for i, span in enumerate(spans):
        start = lead if i == 0 else span["start"]
        end = spans[i + 1]["start"] if i + 1 < len(spans) else voice_s
        starts.append((start, end))
        slots.append(broll.clips_needed(end - start, fmt.cut_every))
    people = [b.get("person") for b in beats_text]
    real_per_beat = assign_real(real, [n - (1 if (people[i] and n > 1) else 0) for i, n in enumerate(slots)],
                                reuse=int(cfg.get("media.reuse", 1)))
    timing_s["media"] = round(time.monotonic() - t0, 1)
    t0 = time.monotonic()
    for i, (beat, span) in enumerate(zip(beats_text, spans)):
        start, end = starts[i]
        n = slots[i]
        paths: list[Path] = []
        # A beat about a public figure opens on their licensed photo; the rest is faceless stock.
        person = beat.get("person")
        if person and person not in photos:
            photos[person] = portrait.find(cfg, client, person)
        photo = photos.get(person) if person else None
        if photo:
            frame = portrait.compose(cfg.root / photo.path, photo.credit, work / f"photo_{i}.jpg", frame=fmt.frame)
            paths.append(frame.relative_to(cfg.root))
            manifest.append({"provider": "wikimedia", "id": photo.file, "page": photo.page, "author": photo.author,
                             "license": photo.license, "credit": photo.credit, "person": person,
                             "path": photo.path, "beat": i, "at": round(start, 3)})
            if photo.credit not in credits:
                credits.append(photo.credit)
        for item in real_per_beat[i]:
            paths.append(Path(item.path))
        stock: list[broll.Clip] = []
        missing = n - len(paths)
        if missing > 0 or not paths:
            need = (end - start) * max(missing, 1) / n
            stock = broll.choose(cfg, client, beat["broll_keywords"], need=need, used=used, recent=recent,
                                 orientation=fmt.orientation, cut_every=fmt.cut_every)
            for clip in stock:
                broll.download(cfg, client, clip)
                paths.append(Path(clip.path))
        per = (end - start) / len(paths)
        for item in real_per_beat[i]:
            manifest.append(sourcemedia.manifest_entry(item, i, start, per))
        for clip in stock:
            manifest.append(broll.manifest_entry(clip, i, start, per))
        clips_per_beat.append(paths)
    timing_s["broll"] = round(time.monotonic() - t0, 1)
    t0 = time.monotonic()

    # Cover card = first frame = thumbnail: the hero picture (first real still, else a Commons photo), title, badge.
    hero = next((cfg.root / it.extra["photo"] for it in real if it.still and it.extra.get("photo")), None)
    if hero is None and photos:
        hero = next((cfg.root / p.path for p in photos.values() if p), None)
    cover_path = brand.cover(look, work / "cover.jpg", title or (beats_text[0]["text"] if beats_text else None),
                             series, photo=hero, frame=fmt.frame, with_logo=False)
    cover_jpg = out_dir / f"{video['id']}.cover.jpg"
    brand.cover(look, cover_jpg, title or (beats_text[0]["text"] if beats_text else None), series, photo=hero,
                frame=fmt.frame, with_logo=True)
    segments = [render.Segment(cover_path.relative_to(cfg.root), lead, still=True)] if lead > 0 else []
    segments += render.segments_for(spans, clips_per_beat, voice_s, lead=lead)
    total = sum(s.seconds for s in segments) + endcard_s
    hook_s = fmt.hook_title_seconds if title else 0.0
    subs_list = subtitles.render_sequence(words, spans, work, total, renderer, hide_until=lead + hook_s)
    from src.script.write import cta_line
    cta = cta_line(look, series, video["id"])
    card = brand.endcard(look, work / "endcard.png", series, cta, frame=fmt.frame)
    music_path, music_credit = music.pick(cfg, video["id"], video.get("category"), fmt.kind, series)
    if music_credit and music_credit not in credits:
        credits.append(music_credit)
    media_domains = sorted({it.source for it in real if it.source})
    if media_domains:
        credits.append("Media: " + ", ".join(media_domains))
    # Chapters (long videos): the hook opens "المقدمة" at 0:00, then each beat carrying a title. Shown as cards
    # and written into the YouTube description as timestamps (publish).
    chapters = [{"at": round(float(span["start"] or 0), 3), "title": beat["chapter"]}
                for beat, span in zip(beats_text, spans) if beat.get("chapter") and span.get("start") is not None]
    if chapters:
        # The model sometimes repeats a section's title on its next beat: keep the first, and keep ≥10 s apart
        # (YouTube's rule for chapters; a card every few seconds would be noise anyway).
        kept: list[dict[str, Any]] = [{"at": 0.0, "title": "المقدمة"}]
        for c in chapters:
            if c["title"] != kept[-1]["title"] and c["at"] - kept[-1]["at"] >= 10:
                kept.append(c)
        chapters = kept
    overlays = []
    lst = brand.chapter_sequence(chapters, work, total, frame=fmt.frame) if chapters else None
    if lst:
        overlays.append(lst)
    plan = render.Plan(segments, Path(video["voice_path"]), subs_list, renderer.style.top, card, endcard_s,
                       out_dir / f"{video['id']}.mp4", music=music_path,
                       hook_list=brand.hook_sequence(title, series, work, hook_s, frame=fmt.frame, delay=lead)
                       if title else None,
                       logo=brand.logo_layer(look, work / "logo.png"),
                       transition=float(cfg.get("video.transition_seconds", 0.3)),
                       progress_bar=bool(cfg.get("video.progress_bar", True)),
                       width=fmt.width, height=fmt.height, max_seconds=fmt.max_seconds, overlays=overlays,
                       logo_xy=render.LOGO_XY if fmt.portrait else (48, 40), voice_delay=lead,
                       music_volume=float(cfg.get("video.music_volume", 0.28)))
    timing_s["graphics"] = round(time.monotonic() - t0, 1)  # subtitle PNGs, hook, cover, end card, logo, chapters
    t0 = time.monotonic()
    render.render(cfg, plan, run=run)
    timing_s["render"] = round(time.monotonic() - t0, 1)
    t0 = time.monotonic()

    srt_path = out_dir / f"{video['id']}.srt"
    srt_path.write_text(subtitles.srt(words, spans, renderer), encoding="utf-8")
    thumb = None
    if not fmt.portrait:                                   # Shorts can't take a custom thumbnail; long videos do
        thumb_path = out_dir / f"{video['id']}.thumb.jpg"
        if hero is not None:
            made = brand.cover(look, thumb_path, title or (beats_text[0]["text"] if beats_text else None), series,
                               photo=hero, frame=thumbnail.SIZE, with_logo=True)
        else:
            clean = [cfg.root / m["path"] for m in manifest if m.get("provider") not in ("wikimedia", "source")
                     and m.get("path")]
            made = thumbnail.make(cfg, plan.out, total - endcard_s,
                                  title or (beats_text[0]["text"] if beats_text else None),
                                  series, look, thumb_path, work / "thumb", run=run, clips=clean)
        thumb = str(made.relative_to(cfg.root)) if made else None
        timing_s["thumbnail"] = round(time.monotonic() - t0, 1)
    shutil.rmtree(work, ignore_errors=True)
    n_real = sum(len(r) for r in real_per_beat)
    media_notes = {"real": n_real, "photos": sum(1 for r in real_per_beat for it in r if it.still),
                   "clips": sum(1 for r in real_per_beat for it in r if not it.still),
                   "stock": sum(1 for m in manifest if m.get("provider") not in ("wikimedia", "source")),
                   "found": len(story_media), "domains": media_domains}
    return {"video_path": str(plan.out.relative_to(cfg.root)), "subtitle_path": str(srt_path.relative_to(cfg.root)),
            "duration_s": plan.total, "manifest": manifest,
            "notes": {"voice_s": voice_s, "music": str(music_path) if music_path else None, "clips": len(manifest),
                      "credits": credits, "hook_title": title, "series": series, "cta": cta, "kind": fmt.kind,
                      "chapters": chapters, "thumbnail": thumb, "cover": str(cover_jpg.relative_to(cfg.root)),
                      "media": media_notes, "timing": timing_s}}


def _outage(exc: BaseException) -> bool:
    """The stock provider or the network was down (not this video's fault): wait, don't count an attempt."""
    return isinstance(exc, (broll.BrollUnavailable, httpx.TransportError))


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
                 "yes" if music.pick(cfg, 0)[0] else "none (voice only)")
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
                ensure_media(cfg, client, v, conn=conn, run=run)
                row = assemble_video(cfg, v, client, run=run, recent=_recent_clips(conn))
            except (broll.BrollError, render.GuardrailError) as exc:
                log.warning("Video %d: %s", v["id"], exc)
                _fail(conn, v, str(exc))
                failed.append({"video_id": v["id"], "error": str(exc)})
                continue
            except Exception as exc:                    # network, ffmpeg, stock outage: retry next run
                msg = f"{type(exc).__name__}: {exc}"
                if _outage(exc):
                    log.error("Video %d: stock/network unreachable, left for the next run: %s", v["id"], msg)
                    retry.append({"video_id": v["id"], "error": msg})
                    continue
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
            log.info("Video %d → %s (%.1fs, %d clips, music: %s; %s)", v["id"], row["video_path"],
                     row["duration_s"], row["notes"]["clips"], row["notes"]["music"] or "none",
                     " ".join(f"{k} {s:.0f}s" for k, s in row["notes"]["timing"].items()))
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
