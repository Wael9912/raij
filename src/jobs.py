"""Background jobs started from the Telegram bot (Phase 13): `trending`, `produce`, `run-daily`.

The bot must stay responsive to taps, so production runs in a separate `python -m src.main <cmd>` process
(same venv, cwd = repo, output appended to data/logs/jobs.log). Requests queue in `control.job_queue`; the bot's
maintenance pass starts the next one when nothing is running. Every pipeline job takes the `pipeline` file lock
(src/lock.py), so a scheduled `run-daily` and a bot-started `produce` can't work the same rows at once — the
one that finds the lock busy simply skips and the queued request is retried on the next pass.

On GitHub Actions (`tick`) there is no long-lived bot: `run_queued_inline` runs the queue synchronously.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from src import db
from src.config import Config

log = logging.getLogger("raij.jobs")

ALLOWED = ("trending", "produce", "run-daily")
QUEUE_KEY = "job_queue"
CURRENT_KEY = "job_current"
_procs: dict[int, subprocess.Popen] = {}          # this process's children, to read exit codes


def queue(conn: sqlite3.Connection) -> list[str]:
    try:
        q = json.loads(db.get_flag(conn, QUEUE_KEY) or "[]")
    except ValueError:
        q = []
    return [j for j in q if j in ALLOWED]


def request(conn: sqlite3.Connection, name: str) -> bool:
    """Queue a job (deduplicated). Returns False if it was already queued or is the running job."""
    if name not in ALLOWED:
        raise ValueError(f"unknown job {name!r}")
    q = queue(conn)
    cur = current(conn)
    if name in q or (cur and cur.get("name") == name and alive(cur)):
        return False
    q.append(name)
    db.set_flag(conn, QUEUE_KEY, json.dumps(q))
    return True


def current(conn: sqlite3.Connection) -> dict[str, Any] | None:
    try:
        cur = json.loads(db.get_flag(conn, CURRENT_KEY) or "null")
    except ValueError:
        cur = None
    return cur if isinstance(cur, dict) and cur.get("pid") else None


def alive(cur: dict[str, Any] | None) -> bool:
    if not cur or not cur.get("pid"):
        return False
    try:
        os.kill(int(cur["pid"]), 0)
    except (OSError, ValueError):
        return False
    return True


def _python() -> list[str]:
    return [sys.executable, "-m", "src.main"]


def spawn(cfg: Config, conn: sqlite3.Connection, name: str) -> int:
    """Start `name` detached; record it as the current job. Returns the pid."""
    logs = cfg.root / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / "jobs.log", "ab")                     # noqa: SIM115 — the child owns it
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(_python() + [name], cwd=str(cfg.root), stdout=out, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True)
    out.close()
    _procs[proc.pid] = proc
    db.set_flag(conn, CURRENT_KEY, json.dumps({"name": name, "pid": proc.pid, "started": time.time()}))
    log.info("Job %s started (pid %d)", name, proc.pid)
    return proc.pid


def tick(cfg: Config, conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Maintenance pass for the bot: report a finished job, start the next queued one. Returns the event, if any:
    {"finished": name, "code": int} or {"started": name}."""
    cur = current(conn)
    if cur:
        if alive(cur):
            return None
        code = None
        proc = _procs.pop(int(cur["pid"]), None)
        if proc is not None:
            code = proc.poll()
        db.set_flag(conn, CURRENT_KEY, "null")
        return {"finished": cur["name"], "code": code, "seconds": time.time() - float(cur.get("started") or time.time())}
    q = queue(conn)
    if not q:
        return None
    name = q.pop(0)
    db.set_flag(conn, QUEUE_KEY, json.dumps(q))
    spawn(cfg, conn, name)
    return {"started": name}


def run_queued_inline(cfg: Config, conn: sqlite3.Connection, run_command) -> list[str]:
    """No long-lived bot (GitHub Actions `tick`): run every queued job now, in this process."""
    done = []
    while True:
        q = queue(conn)
        if not q:
            return done
        name = q.pop(0)
        db.set_flag(conn, QUEUE_KEY, json.dumps(q))
        try:
            run_command(name)
        except Exception:                                    # noqa: BLE001 — one job must not stop the tick
            log.exception("Queued job %s failed", name)
        done.append(name)


def status_line(conn: sqlite3.Connection) -> str:
    cur = current(conn)
    q = queue(conn)
    parts = []
    if cur and alive(cur):
        mins = int((time.time() - float(cur.get("started") or time.time())) // 60)
        parts.append(f"⚙️ Running: {cur['name']} ({mins} min)")
    if q:
        parts.append("⏳ Queued: " + ", ".join(q))
    return "\n".join(parts) if parts else "💤 No job running."


def log_tail(cfg: Config, lines: int = 12) -> str:
    path = cfg.root / "data" / "logs" / "jobs.log"
    if not path.exists():
        return ""
    try:
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return ""
    return "\n".join(line[-160:] for line in tail)


__all__ = ["ALLOWED", "Path", "alive", "current", "log_tail", "queue", "request", "run_queued_inline", "spawn",
           "status_line", "tick"]
