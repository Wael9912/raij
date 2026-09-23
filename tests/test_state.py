"""Encrypted state bundle (src/state.py): authenticated format, legacy bundles, and what unpack may write (S2)."""
import io
import json
import os
import subprocess
import tarfile

import pytest

from src import db, state
from src.config import load_config

KEY = "k" * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "data/pipeline.db"))
    monkeypatch.setenv("RAIJ_STATE_KEY", KEY)
    cfg = load_config()
    cfg.root = tmp_path
    (tmp_path / "data").mkdir()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO control (key, value) VALUES ('marker', 'live')")
    conn.commit()
    conn.close()
    yield cfg, tmp_path


def _fresh(cfg, tmp_path, name="fresh"):
    other = tmp_path / name
    cfg.root = other
    cfg.db_path = other / "data/pipeline.db"
    return other


def _marker(cfg):
    conn = db.connect(cfg.db_path)
    try:
        return db.get_flag(conn, "marker")
    finally:
        conn.close()


def test_bundle_is_authenticated_and_round_trips(env):
    cfg, tmp = env
    bundle = state.pack(cfg, tmp / "s.enc")
    raw = bundle.read_bytes()
    assert raw.startswith(state.MAGIC) and b"SQLite" not in raw
    _fresh(cfg, tmp)
    assert state.unpack(cfg, bundle) == ["data/pipeline.db"]
    assert _marker(cfg) == "live"


def test_tampered_bundle_is_rejected_before_decryption(env, monkeypatch):
    cfg, tmp = env
    bundle = state.pack(cfg, tmp / "s.enc")
    raw = bytearray(bundle.read_bytes())
    raw[-1] ^= 0x01                                        # flip one ciphertext bit
    bundle.write_bytes(bytes(raw))
    calls = []
    monkeypatch.setattr(state.subprocess, "run", lambda *a, **k: calls.append(a) or pytest.fail("openssl ran"))
    _fresh(cfg, tmp)
    with pytest.raises(state.StateError, match="failed authentication"):
        state.unpack(cfg, bundle)
    assert calls == [] and not cfg.db_path.exists()


def test_wrong_key_is_reported_as_such(env, monkeypatch):
    cfg, tmp = env
    bundle = state.pack(cfg, tmp / "s.enc")
    monkeypatch.setenv("RAIJ_STATE_KEY", "x" * 40)
    _fresh(cfg, tmp)
    with pytest.raises(state.StateError, match="wrong RAIJ_STATE_KEY"):
        state.unpack(cfg, bundle)


def _legacy_bundle(tmp, members):
    """A pre-10c bundle: plain `openssl enc` output over a tar with the given {name: bytes}."""
    tar = tmp / "legacy.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    out = tmp / "legacy.enc"
    subprocess.run([*state.OPENSSL, "-salt", "-in", str(tar), "-out", str(out), "-pass", "env:RAIJ_STATE_KEY"],
                   env={**os.environ, "RAIJ_STATE_KEY": KEY}, check=True)
    return out


def _db_bytes(tmp):
    return (tmp / "data/pipeline.db").read_bytes()


def test_legacy_unauthenticated_bundle_still_unpacks(env, caplog):
    cfg, tmp = env
    bundle = _legacy_bundle(tmp, {"data/pipeline.db": _db_bytes(tmp), "state-manifest.json": b"{}"})
    assert bundle.read_bytes().startswith(b"Salted__")
    _fresh(cfg, tmp)
    with caplog.at_level("WARNING", logger="raij.state"):
        assert state.unpack(cfg, bundle) == ["data/pipeline.db"]
    assert "Unauthenticated" in caplog.text and _marker(cfg) == "live"


def test_unpack_writes_only_the_db_and_generated_media(env, caplog):
    """A bundle (from a stolen key + cache write, S1→S2) must not be able to drop code, keys or workflows."""
    cfg, tmp = env
    members = {
        "data/pipeline.db": _db_bytes(tmp),
        "assets/generated/video/7.mp4": b"mp4",
        "assets/generated/voice/7.words.json": b"{}",
        "src/main.py": b"import os; os.system('evil')",
        ".env": b"GEMINI_API_KEY=stolen",
        ".github/workflows/raij.yml": b"on: push",
        "data/youtube.token.json": b"{}",
        "assets/generated/../../.env": b"x",
        "assets/generated": b"not a dir",
        "assets/stock/x.mp4": b"cache",
    }
    bundle = _legacy_bundle(tmp, members)
    other = _fresh(cfg, tmp)
    with caplog.at_level("WARNING", logger="raij.state"):
        names = state.unpack(cfg, bundle)
    assert sorted(names) == ["assets/generated/video/7.mp4", "assets/generated/voice/7.words.json", "data/pipeline.db"]
    written = sorted(str(p.relative_to(other)) for p in other.rglob("*") if p.is_file())
    assert written == ["assets/generated/video/7.mp4", "assets/generated/voice/7.words.json", "data/pipeline.db"]
    assert not (tmp / ".env").exists()
    assert caplog.text.count("Skipping bundle member") == 7


def test_symlink_member_is_skipped(env):
    cfg, tmp = env
    tar = tmp / "l.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        info = tarfile.TarInfo("assets/generated/video/link.mp4")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        t.addfile(info)
        data = _db_bytes(tmp)
        info = tarfile.TarInfo("data/pipeline.db")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    out = tmp / "l.enc"
    subprocess.run([*state.OPENSSL, "-salt", "-in", str(tar), "-out", str(out), "-pass", "env:RAIJ_STATE_KEY"],
                   env={**os.environ, "RAIJ_STATE_KEY": KEY}, check=True)
    other = _fresh(cfg, tmp)
    assert state.unpack(cfg, out) == ["data/pipeline.db"]
    assert not (other / "assets").exists()


def test_allowed_members():
    ok = ["data/pipeline.db", "assets/generated/video/1.mp4", "assets/generated/voice/1_v2.words.json"]
    bad = ["data/other.db", "data/.changed", "assets/generated", "assets/generated/", "assets/stock/a.mp4",
           "assets/generated/../x", "/assets/generated/video/1.mp4", "src/state.py", "state-manifest.json"]
    assert all(state.allowed(n) for n in ok)
    assert not any(state.allowed(n) for n in bad)


def test_pack_needs_a_real_key(env, monkeypatch):
    cfg, tmp = env
    monkeypatch.setenv("RAIJ_STATE_KEY", "short")
    with pytest.raises(state.StateError, match="32 chars"):
        state.pack(cfg, tmp / "s.enc")
    assert json.loads("{}") == {}
