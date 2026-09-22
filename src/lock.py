"""Cross-process locks (flock on data/locks/<name>.lock), so scheduled jobs never overlap — e.g. the
30-minute publish job and run-daily's publish step can't both upload the same video."""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Busy(RuntimeError):
    """Another process holds the lock."""


@contextmanager
def single(root: Path, name: str) -> Iterator[None]:
    path = root / "data" / "locks" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy(f"another '{name}' is already running") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
