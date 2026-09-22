"""Run Ra'ij without a terminal: three macOS launchd agents (per-user, no sudo).

- com.raij.bot      — the Telegram review bot, always running, restarted if it dies.
- com.raij.daily    — `run-daily` every day at schedule.run_daily_at (local time); if the Mac was asleep
                      then, launchd runs it on wake.
- com.raij.publish  — `publish` every publish.every_minutes, so approvals go out soon after the tap.

Logs go to data/logs/<job>.log. Nothing runs while the Mac is asleep or off; Telegram taps queue
and are handled when it wakes.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from src.config import Config

AGENTS = Path.home() / "Library" / "LaunchAgents"
LABELS = ("com.raij.bot", "com.raij.daily", "com.raij.publish")
Run = Callable[[list[str]], subprocess.CompletedProcess]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def plists(cfg: Config) -> dict[str, dict]:
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    logs = cfg.root / "data" / "logs"
    hour, minute = (int(x) for x in str(cfg.get("schedule.run_daily_at", "07:00")).split(":"))

    def job(label: str, command: str, **extra) -> dict:
        name = label.rsplit(".", 1)[1]
        return {"Label": label, "ProgramArguments": [uv, "run", "python", "-m", "src.main", command],
                "WorkingDirectory": str(cfg.root),
                "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                                         "LANG": "en_US.UTF-8", "PYTHONUNBUFFERED": "1"},
                "StandardOutPath": str(logs / f"{name}.log"), "StandardErrorPath": str(logs / f"{name}.log"),
                "ProcessType": "Background", **extra}

    return {
        "com.raij.bot": job("com.raij.bot", "bot", RunAtLoad=True, KeepAlive=True, ThrottleInterval=30),
        "com.raij.daily": job("com.raij.daily", "run-daily", StartCalendarInterval={"Hour": hour, "Minute": minute}),
        "com.raij.publish": job("com.raij.publish", "publish",
                                StartInterval=int(cfg.get("publish.every_minutes", 30)) * 60),
    }


def install(cfg: Config, run: Run = _run, agents: Path = AGENTS) -> list[Path]:
    (cfg.root / "data" / "logs").mkdir(parents=True, exist_ok=True)
    agents.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    written = []
    for label, spec in plists(cfg).items():
        path = agents / f"{label}.plist"
        run(["launchctl", "bootout", f"{domain}/{label}"])          # reload cleanly if already installed
        with path.open("wb") as f:
            plistlib.dump(spec, f)
        proc = run(["launchctl", "bootstrap", domain, str(path)])
        if proc.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap {label} failed: {(proc.stderr or '').strip()}")
        written.append(path)
    return written


def uninstall(run: Run = _run, agents: Path = AGENTS) -> list[str]:
    removed = []
    for label in LABELS:
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        path = agents / f"{label}.plist"
        if path.exists():
            path.unlink()
            removed.append(label)
    return removed


def status(run: Run = _run) -> dict[str, str]:
    out = {}
    for label in LABELS:
        proc = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
        if proc.returncode != 0:
            out[label] = "not installed"
            continue
        info = {k.strip(): v.strip() for k, _, v in (line.partition("=") for line in proc.stdout.splitlines()) if v}
        state = info.get("state", "?")
        last = info.get("last exit code")
        out[label] = f"{state}" + (f" (last exit {last})" if last else "")
    return out
