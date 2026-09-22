"""Config loader: merges config.yaml with secrets from .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# The assembler may only ingest media from these directories (transformation guardrail): licensed
# stock footage, our own generated voice/subtitles, and CC0 music. Never source videos.
ALLOWED_MEDIA_SUBDIRS = ("assets/stock", "assets/generated", "assets/music")
ALLOWED_MEDIA_DIRS = tuple(ROOT / d for d in ALLOWED_MEDIA_SUBDIRS)


@dataclass
class Config:
    data: dict[str, Any]
    root: Path = ROOT
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "pipeline.db")

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def brands(self) -> list[dict[str, Any]]:
        return self.data.get("brands", [])

    @staticmethod
    def secret(name: str, default: str | None = None) -> str | None:
        value = os.getenv(name)
        return value if value else default


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not data.get("brands"):
        raise ValueError(f"{cfg_path}: at least one brand must be configured")
    db_env = os.getenv("RAIJ_DB_PATH")
    db_path = Path(db_env) if db_env else ROOT / "data" / "pipeline.db"
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    return Config(data=data, db_path=db_path)
