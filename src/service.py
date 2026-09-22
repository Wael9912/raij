"""Run Ra'ij without a terminal: three background jobs — macOS launchd agents (per-user, no sudo) on
the Mac, systemd units + timers on a Linux server (installed with sudo). Same jobs either way:

- com.raij.bot      — the Telegram review bot, always running, restarted if it dies.
- com.raij.daily    — `run-daily` every day at schedule.run_daily_at (local time); if the Mac was asleep
                      then, launchd runs it on wake.
- com.raij.publish  — `publish` every publish.every_minutes, so approvals go out soon after the tap.

Logs go to data/logs/<job>.log. On a Mac nothing runs while it's asleep or off; Telegram taps queue
and are handled when it wakes. On Linux the daily timer is Persistent (a missed run happens at boot).
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
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


# --- Linux (systemd) ------------------------------------------------------------

SYSTEMD = Path("/etc/systemd/system")


def units(cfg: Config, user: str | None = None) -> dict[str, str]:
    """systemd unit files by filename: a long-running bot service and two timer-driven oneshots."""
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    user = user or os.environ.get("USER", "ubuntu")
    logs = cfg.root / "data" / "logs"
    tz = cfg.get("schedule.timezone", "Africa/Cairo")
    at = str(cfg.get("schedule.run_daily_at", "07:00"))
    every = int(cfg.get("publish.every_minutes", 30))

    def service(name: str, command: str, extra: str = "") -> str:
        return (f"[Unit]\nDescription=Ra'ij {command}\nAfter=network-online.target\nWants=network-online.target\n\n"
                f"[Service]\nUser={user}\nWorkingDirectory={cfg.root}\n"
                f"Environment=PATH={Path(uv).parent}:/usr/local/bin:/usr/bin:/bin\nEnvironment=LANG=C.UTF-8\n"
                f"Environment=PYTHONUNBUFFERED=1\nExecStart={uv} run python -m src.main {command}\n"
                f"StandardOutput=append:{logs / name}.log\nStandardError=append:{logs / name}.log\n{extra}")

    return {
        "raij-bot.service": service("bot", "bot", "Restart=always\nRestartSec=30\n\n[Install]\nWantedBy=multi-user.target\n"),
        "raij-daily.service": service("daily", "run-daily", "Type=oneshot\nTimeoutStartSec=3h\n"),
        "raij-daily.timer": (f"[Unit]\nDescription=Ra'ij daily run\n\n[Timer]\nOnCalendar=*-*-* {at}:00 {tz}\n"
                             f"Persistent=true\n\n[Install]\nWantedBy=timers.target\n"),
        "raij-publish.service": service("publish", "publish", "Type=oneshot\nTimeoutStartSec=1h\n"),
        "raij-publish.timer": (f"[Unit]\nDescription=Ra'ij publish every {every} min\n\n[Timer]\nOnBootSec=5min\n"
                               f"OnUnitInactiveSec={every}min\n\n[Install]\nWantedBy=timers.target\n"),
    }


def _install_systemd(cfg: Config, run: Run, unit_dir: Path) -> list[Path]:
    written = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in units(cfg).items():
            src = Path(tmp) / name
            src.write_text(body)
            dest = unit_dir / name
            proc = run(["sudo", "install", "-m", "644", str(src), str(dest)])
            if proc.returncode != 0:
                raise RuntimeError(f"installing {name} failed: {(proc.stderr or '').strip()}")
            written.append(dest)
    for cmd in (["sudo", "systemctl", "daemon-reload"],
                ["sudo", "systemctl", "enable", "--now", "raij-bot.service", "raij-daily.timer", "raij-publish.timer"],
                ["sudo", "systemctl", "restart", "raij-bot.service"]):
        proc = run(cmd)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[1:])} failed: {(proc.stderr or '').strip()}")
    return written


def _uninstall_systemd(run: Run, unit_dir: Path) -> list[str]:
    run(["sudo", "systemctl", "disable", "--now", "raij-bot.service", "raij-daily.timer", "raij-publish.timer"])
    removed = []
    for name in units_names():
        if (unit_dir / name).exists():
            run(["sudo", "rm", "-f", str(unit_dir / name)])
            removed.append(name)
    run(["sudo", "systemctl", "daemon-reload"])
    return removed


def units_names() -> list[str]:
    return ["raij-bot.service", "raij-daily.service", "raij-daily.timer", "raij-publish.service", "raij-publish.timer"]


def _status_systemd(run: Run) -> dict[str, str]:
    out = {}
    for name in ("raij-bot.service", "raij-daily.timer", "raij-publish.timer"):
        proc = run(["systemctl", "show", name, "--property=ActiveState,SubState,NextElapseUSecRealtime"])
        info = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        state = f"{info.get('ActiveState', '?')} ({info.get('SubState', '?')})"
        nxt = info.get("NextElapseUSecRealtime")
        out[name] = state + (f", next {nxt}" if nxt else "")
    return out


def linux() -> bool:
    return sys.platform.startswith("linux")


# --- macOS (launchd) -------------------------------------------------------------

def install(cfg: Config, run: Run = _run, agents: Path = AGENTS) -> list[Path]:
    if linux():
        (cfg.root / "data" / "logs").mkdir(parents=True, exist_ok=True)
        return _install_systemd(cfg, run, SYSTEMD)
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
    if linux():
        return _uninstall_systemd(run, SYSTEMD)
    removed = []
    for label in LABELS:
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        path = agents / f"{label}.plist"
        if path.exists():
            path.unlink()
            removed.append(label)
    return removed


def status(run: Run = _run) -> dict[str, str]:
    if linux():
        return _status_systemd(run)
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
