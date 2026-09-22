"""Pack/unpack the pipeline's state as one encrypted file, for hosts without a persistent disk
(GitHub Actions: restored at the start of every job, saved at the end).

The bundle holds a consistent snapshot of the SQLite DB plus the media still needed later: videos
waiting for review or publishing, and their voice tracks (re-render / new b-roll). Published,
rejected and superseded videos are dropped; stock clips are a cache and re-download.
Encryption: `openssl enc -aes-256-cbc -pbkdf2` with RAIJ_STATE_KEY, so the bundle is safe in a
public repo's cache or release.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path

from src.config import Config

LIVE = ("pending", "voiced", "rendered", "in_review", "approved")
OPENSSL = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000"]


class StateError(RuntimeError):
    pass


def _key() -> str:
    key = os.environ.get("RAIJ_STATE_KEY", "")
    if len(key) < 32:
        raise StateError("RAIJ_STATE_KEY must be set (≥32 chars) to pack/unpack state")
    return key


def media(cfg: Config, conn: sqlite3.Connection) -> list[Path]:
    """Repo-relative files of videos that can still be reviewed, regenerated or published."""
    out: set[Path] = set()
    marks = ",".join("?" * len(LIVE))
    for r in conn.execute(f"SELECT video_path, subtitle_path, voice_path FROM videos WHERE status IN ({marks})", LIVE):
        for p in (r["video_path"], r["subtitle_path"], r["voice_path"]):
            if p:
                out.add(Path(p))
        if r["voice_path"]:
            out.add(Path(r["voice_path"]).with_suffix(".words.json"))
    return sorted(p for p in out if (cfg.root / p).is_file())


def pack(cfg: Config, out: Path, with_media: bool = True) -> Path:
    key = _key()
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "pipeline.db"
        src = sqlite3.connect(cfg.db_path)
        src.row_factory = sqlite3.Row
        dst = sqlite3.connect(snap)
        src.backup(dst)                                  # consistent copy even with WAL
        dst.close()
        files = media(cfg, src) if with_media else []
        src.close()
        tar = Path(tmp) / "state.tar.gz"
        with tarfile.open(tar, "w:gz") as t:
            t.add(snap, arcname="data/pipeline.db")
            for f in files:
                t.add(cfg.root / f, arcname=str(f))
            manifest = json.dumps({"files": [str(f) for f in files]}).encode()
            info = tarfile.TarInfo("state-manifest.json")
            info.size = len(manifest)
            t.addfile(info, io.BytesIO(manifest))
        out.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([*OPENSSL, "-salt", "-in", str(tar), "-out", str(out), "-pass", "env:RAIJ_STATE_KEY"],
                              env={**os.environ, "RAIJ_STATE_KEY": key}, capture_output=True, text=True)
        if proc.returncode:
            raise StateError(f"encrypt failed: {proc.stderr.strip()}")
    return out


def unpack(cfg: Config, archive: Path) -> list[str]:
    key = _key()
    with tempfile.TemporaryDirectory() as tmp:
        tar = Path(tmp) / "state.tar.gz"
        proc = subprocess.run([*OPENSSL, "-d", "-in", str(archive), "-out", str(tar), "-pass", "env:RAIJ_STATE_KEY"],
                              env={**os.environ, "RAIJ_STATE_KEY": key}, capture_output=True, text=True)
        if proc.returncode:
            raise StateError("decrypt failed — wrong RAIJ_STATE_KEY or corrupt bundle")
        names = []
        root = cfg.root.resolve()
        with tarfile.open(tar) as t:
            for m in t.getmembers():
                dest = (root / m.name).resolve()
                if not m.isfile() or not dest.is_relative_to(root) or m.name == "state-manifest.json":
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with t.extractfile(m) as f:
                    dest.write_bytes(f.read())
                names.append(m.name)
    for side in ("-wal", "-shm"):                          # a stale WAL must not be replayed onto the snapshot
        Path(str(cfg.db_path) + side).unlink(missing_ok=True)
    return names
