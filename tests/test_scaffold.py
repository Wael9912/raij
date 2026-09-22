import subprocess
import sys

from src import db
from src.config import ROOT, load_config
from src.main import main


def test_help_runs():
    result = subprocess.run(
        [sys.executable, "-m", "src.main", "--help"], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "run-daily" in result.stdout


def test_config_loads():
    cfg = load_config()
    assert cfg.brands[0]["id"] == "raij"
    assert cfg.get("ranking.weights.view_velocity") == 0.5


def test_schema_and_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    assert main(["init-db"]) == 0
    assert main(["pause"]) == 0
    conn = db.connect(tmp_path / "t.db")
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "candidates", "stories", "scripts", "videos", "approvals", "posts", "metrics"} <= tables
    assert db.publishing_paused(conn)
    conn.close()


def test_every_stage_has_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIJ_DB_PATH", str(tmp_path / "t.db"))
    assert main(["run-daily", "--dry-run"]) == 0
