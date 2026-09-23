"""Pack/unpack the pipeline's state as one encrypted file, for hosts without a persistent disk
(GitHub Actions: restored at the start of every job, saved at the end).

The bundle holds a consistent snapshot of the SQLite DB plus the media still needed later: videos
waiting for review or publishing, and their voice tracks (re-render / new b-roll). Published,
rejected and superseded videos are dropped; stock clips are a cache and re-download.
Encryption: `openssl enc -aes-256-cbc -pbkdf2` with RAIJ_STATE_KEY, so the bundle is safe in a
public repo's cache or release. Since Phase 10c (S2) the ciphertext is authenticated: the file is
`MAGIC + HMAC-SHA256(ciphertext) + ciphertext`, the MAC key derived from RAIJ_STATE_KEY by PBKDF2, so a
bundle nobody with the key produced is rejected before openssl sees it. Older bundles (plain openssl
output, `Salted__` header) still unpack, with a warning. Unpack only ever writes the DB file and files
under `assets/generated/` — never code, `.env`, tokens or workflows.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path

from src.config import Config

log = logging.getLogger("raij.state")

LIVE = ("pending", "voiced", "rendered", "in_review", "approved")
OPENSSL = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000"]
MAGIC = b"RAIJ-STATE-2\n"
MAC_SALT = b"raij-state-mac"
MAC_LEN = 32
DB_MEMBER = "data/pipeline.db"
MEDIA_PREFIX = "assets/generated/"
MANIFEST = "state-manifest.json"


class StateError(RuntimeError):
    pass


def _key() -> str:
    key = os.environ.get("RAIJ_STATE_KEY", "")
    if len(key) < 32:
        raise StateError("RAIJ_STATE_KEY must be set (≥32 chars) to pack/unpack state")
    return key


def _mac(key: str, ciphertext: bytes) -> bytes:
    mac_key = hashlib.pbkdf2_hmac("sha256", key.encode(), MAC_SALT, 200_000)
    return hmac.new(mac_key, ciphertext, "sha256").digest()


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
            t.add(snap, arcname=DB_MEMBER)
            for f in files:
                t.add(cfg.root / f, arcname=str(f))
            manifest = json.dumps({"files": [str(f) for f in files]}).encode()
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(manifest)
            t.addfile(info, io.BytesIO(manifest))
        enc = Path(tmp) / "state.enc"
        proc = subprocess.run([*OPENSSL, "-salt", "-in", str(tar), "-out", str(enc), "-pass", "env:RAIJ_STATE_KEY"],
                              env={**os.environ, "RAIJ_STATE_KEY": key}, capture_output=True, text=True)
        if proc.returncode:
            raise StateError(f"encrypt failed: {proc.stderr.strip()}")
        ciphertext = enc.read_bytes()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(MAGIC + _mac(key, ciphertext) + ciphertext)
    return out


def _ciphertext(key: str, archive: Path) -> bytes:
    """The openssl blob, after checking the MAC on an authenticated bundle."""
    data = archive.read_bytes()
    if data.startswith(MAGIC):
        mac, ciphertext = data[len(MAGIC):len(MAGIC) + MAC_LEN], data[len(MAGIC) + MAC_LEN:]
        if not hmac.compare_digest(mac, _mac(key, ciphertext)):
            raise StateError("bundle failed authentication — wrong RAIJ_STATE_KEY or tampered bundle")
        return ciphertext
    log.warning("Unauthenticated (pre-10c) state bundle — it will be re-saved in the authenticated format")
    return data


def allowed(name: str) -> bool:
    """Members unpack may write: the DB snapshot and generated media, nothing else (S2)."""
    if name == DB_MEMBER:
        return True
    parts = Path(name).parts
    return (name.startswith(MEDIA_PREFIX) and ".." not in parts and not Path(name).is_absolute()
            and len(parts) > 2)


def unpack(cfg: Config, archive: Path) -> list[str]:
    key = _key()
    with tempfile.TemporaryDirectory() as tmp:
        enc = Path(tmp) / "state.enc"
        enc.write_bytes(_ciphertext(key, archive))
        tar = Path(tmp) / "state.tar.gz"
        proc = subprocess.run([*OPENSSL, "-d", "-in", str(enc), "-out", str(tar), "-pass", "env:RAIJ_STATE_KEY"],
                              env={**os.environ, "RAIJ_STATE_KEY": key}, capture_output=True, text=True)
        if proc.returncode:
            raise StateError("decrypt failed — wrong RAIJ_STATE_KEY or corrupt bundle")
        names = []
        root = cfg.root.resolve()
        with tarfile.open(tar) as t:
            for m in t.getmembers():
                if m.name == MANIFEST:
                    continue
                if not m.isfile() or not allowed(m.name):
                    log.warning("Skipping bundle member %r (not a plain file under %s)", m.name, MEDIA_PREFIX)
                    continue
                dest = Path(cfg.db_path) if m.name == DB_MEMBER else (root / m.name).resolve()
                if m.name != DB_MEMBER and not dest.is_relative_to(root / MEDIA_PREFIX.rstrip("/")):
                    log.warning("Skipping bundle member %r (escapes %s)", m.name, MEDIA_PREFIX)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with t.extractfile(m) as f:
                    dest.write_bytes(f.read())
                names.append(m.name)
    for side in ("-wal", "-shm"):                          # a stale WAL must not be replayed onto the snapshot
        Path(str(cfg.db_path) + side).unlink(missing_ok=True)
    return names
