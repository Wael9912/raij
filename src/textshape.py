"""Real Arabic text shaping for Pillow (HarfBuzz via raqm).

Pillow's macOS wheel bundles raqm but loads libfribidi at runtime by bare name, which dyld only looks
for in the default library paths and the current directory — not Apple-silicon Homebrew's
/opt/homebrew/lib. So before Pillow's font module first loads, `ensure()` briefly changes into a temp
dir holding a symlink to Homebrew's fribidi. Setup: `brew install libraqm`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

FRIBIDI = Path("/opt/homebrew/lib/libfribidi.dylib")


def available() -> bool:
    from PIL import features
    return bool(features.check("raqm"))


def ensure() -> bool:
    """Load Pillow's font engine with fribidi reachable; True if raqm layout works. Call before any
    `PIL.ImageFont` import (the fribidi lookup happens once, when the module loads)."""
    if "PIL._imagingft" not in sys.modules and sys.platform == "darwin" and FRIBIDI.exists():
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(FRIBIDI, Path(tmp) / FRIBIDI.name)
            os.chdir(tmp)
            try:
                import PIL._imagingft  # noqa: F401
            finally:
                os.chdir(cwd)
    return available()
