"""Keep the Mac awake while a pipeline command runs (macOS only; a no-op elsewhere).

launchd starts the daily run on time, but a MacBook left alone goes to sleep minutes later and the job freezes
with it: on 2026-09-24 the 10:30 run slept from 10:33 to 14:21 — Power Nap woke it for 45 s every ~16 min,
without network — lost 5 of 8 picks to connection errors and finished at 14:46. `caffeinate -i -s -w <pid>`
holds an idle-sleep assertion for the life of this process (`-s` also blocks system sleep while on AC power).
A closed lid on battery still sleeps; nothing in user space can stop that, so `main.catch_up` finishes the
leftovers later. The long-running bot never asks for this — it would keep the Mac awake around the clock.
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger("raij.power")

_proc: subprocess.Popen | None = None


def stay_awake(pid: int | None = None, platform: str = sys.platform) -> bool:
    """Start `caffeinate` tied to this process. Returns True when an assertion is held."""
    global _proc
    if platform != "darwin" or _proc is not None:
        return False
    exe = shutil.which("caffeinate") or "/usr/bin/caffeinate"
    if not os.path.exists(exe):
        return False
    try:
        _proc = subprocess.Popen([exe, "-i", "-s", "-w", str(pid or os.getpid())], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.debug("caffeinate unavailable: %s", exc)
        return False
    atexit.register(release)
    log.debug("Idle sleep blocked while this command runs (caffeinate pid %d)", _proc.pid)
    return True


def release() -> None:
    """Drop the assertion (also runs at exit; `-w` would end it anyway once this process is gone)."""
    global _proc
    proc, _proc = _proc, None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
