"""Populate assets/music/ with a credited background-music pool (Phase 20).

Kevin MacLeod's catalogue (incompetech.com) is CC BY 4.0 — free for commercial use with credit — and his
albums are mirrored on archive.org, which serves direct MP3 links keyless. This script downloads the tracks
listed in TRACKS, normalises them to a consistent loudness (ffmpeg loudnorm, so the render's fixed music
volume behaves the same for every track) and writes assets/music/pool.json for `assemble.music`.

    uv run python tools/fetch_music.py            # downloads what's missing, rewrites pool.json
    uv run python tools/fetch_music.py --list     # show the pool

The credit line each video gets: "Music: <title> by Kevin MacLeod (incompetech.com), CC BY 4.0".
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parent.parent
MUSIC = ROOT / "assets" / "music"
ARCHIVE = "https://archive.org"
ARTIST = "Kevin MacLeod"
SITE = "incompetech.com"
LICENSE = "CC BY 4.0"

# (archive.org item, track title as in the album's file names, moods)
TRACKS = [
    ("Kevin-MacLeod_Light-Electronic_2014_FullAlbum", "Cipher", ["tech", "upbeat", "default"]),
    ("Kevin-MacLeod_Light-Electronic_2014_FullAlbum", "Electrodoodle", ["tech", "upbeat", "tools"]),
    ("Kevin-MacLeod_Light-Electronic_2014_FullAlbum", "Klockworx", ["tech", "default"]),
    ("Kevin-MacLeod_Light-Electronic_2014_FullAlbum", "Deliberate Thought", ["calm", "money", "long"]),
    ("Kevin-MacLeod_Light-Electronic_2014_FullAlbum", "Wallpaper", ["calm", "life-hack", "long"]),
    ("Kevin-MacLeod_Light-Electronic_2014_FullAlbum", "Pamgaea", ["upbeat", "life-hack", "tools"]),
    ("Kevin-MacLeod_Light-Electronic_2014_FullAlbum", "New Friendly", ["upbeat", "life-hack", "default"]),
    ("Kevin-MacLeod_Light-Electronic_2014_FullAlbum", "Presenterator", ["money", "upbeat", "default"]),
    ("Kevin-MacLeod_Exhilarate_2014_FullAlbum", "Tech Talk", ["tech", "energetic"]),
    ("Kevin-MacLeod_Exhilarate_2014_FullAlbum", "Cool Hard Facts", ["wow-facts", "money", "energetic"]),
    ("Kevin-MacLeod_Exhilarate_2014_FullAlbum", "Pulse", ["wow-facts", "energetic"]),
    ("Kevin-MacLeod_Mystery_2014_FullAlbum", "Invariance", ["wow-facts", "calm", "long"]),
    ("Kevin-MacLeod_Mystery_2014_FullAlbum", "Industrial Revolution", ["wow-facts", "money"]),
    ("Kevin-MacLeod_Mystery_2014_FullAlbum", "Smoking Gun", ["wow-facts", "energetic"]),
    ("Kevin-MacLeod_Wonders_2014_FullAlbum", "Lasting Hope", ["calm", "long"]),
    ("Kevin-MacLeod_Wonders_2014_FullAlbum", "Frozen Star", ["calm", "long", "tech"]),
]


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def album_files(client: httpx.Client, item: str) -> list[dict]:
    data = client.get(f"{ARCHIVE}/metadata/{item}", timeout=30).json()
    return [f for f in data.get("files", []) if f.get("name", "").lower().endswith(".mp3")]


def find_file(files: list[dict], title: str) -> dict | None:
    want = title.lower()
    for f in files:
        name = Path(f["name"]).stem.lower()
        if name.endswith(f"- {want}") or name == want or name.endswith(f" {want}"):
            return f
    return None


def normalise(src: Path, out: Path) -> bool:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", str(src), "-af",
                           "loudnorm=I=-20:TP=-1.5:LRA=11", "-ar", "44100", "-c:a", "libmp3lame", "-q:a", "4",
                           str(out)], capture_output=True, text=True, timeout=300)
    return proc.returncode == 0 and out.exists()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    MUSIC.mkdir(parents=True, exist_ok=True)
    pool_path = MUSIC / "pool.json"
    pool = json.loads(pool_path.read_text()) if pool_path.exists() else []
    if args.list:
        for t in pool:
            print(f"{t['file']:32} {t['title']:24} {', '.join(t['moods'])}")
        return 0
    by_file = {t["file"]: t for t in pool}
    with httpx.Client(follow_redirects=True, headers={"User-Agent": "raij/0.1 music pool"}) as client:
        cache: dict[str, list[dict]] = {}
        for item, title, moods in TRACKS:
            file = f"{slug(title)}.mp3"
            dest = MUSIC / file
            if dest.exists() and dest.stat().st_size > 0:
                by_file[file] = {**by_file.get(file, {}), "file": file, "title": title, "artist": ARTIST, "site": SITE,
                                 "license": LICENSE, "moods": moods, "source": f"{ARCHIVE}/details/{item}"}
                print(f"= {title} (have)")
                continue
            if item not in cache:
                cache[item] = album_files(client, item)
            f = find_file(cache[item], title)
            if not f:
                print(f"! {title}: not in {item}", file=sys.stderr)
                continue
            url = f"{ARCHIVE}/download/{item}/{quote(f['name'])}"
            raw = MUSIC / f".{file}.raw"
            try:
                with client.stream("GET", url, timeout=120) as resp:
                    resp.raise_for_status()
                    with open(raw, "wb") as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
                if not normalise(raw, dest):
                    raw.rename(dest)                     # keep the original if ffmpeg isn't around
            except Exception as exc:
                print(f"! {title}: {exc}", file=sys.stderr)
                continue
            finally:
                raw.unlink(missing_ok=True)
            by_file[file] = {"file": file, "title": title, "artist": ARTIST, "site": SITE, "license": LICENSE,
                             "moods": moods, "source": f"{ARCHIVE}/details/{item}"}
            print(f"+ {title} ({dest.stat().st_size >> 10} KB)")
    pool = [by_file[f] for f in sorted(by_file)]
    pool_path.write_text(json.dumps(pool, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(pool)} track(s) in {pool_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
