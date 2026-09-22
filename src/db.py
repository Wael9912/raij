"""SQLite storage: schema + connection helper."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    command      TEXT NOT NULL,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',   -- running|ok|partial|failed
    dry_run      INTEGER NOT NULL DEFAULT 0,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,                    -- youtube|reddit|trends|rss
    external_id   TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    title         TEXT,
    thumb_url     TEXT,
    views         INTEGER,
    likes         INTEGER,
    comments      INTEGER,
    duration_s    INTEGER,
    published_at  TEXT,
    region        TEXT,
    raw_json      TEXT,
    score         REAL,
    category      TEXT,
    retellable    INTEGER,                          -- NULL=unchecked, 0/1
    rank_reason   TEXT,
    status        TEXT NOT NULL DEFAULT 'new',      -- new|ranked|selected|rejected|flagged
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS stories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id),
    transcript      TEXT,                           -- kept only for similarity check
    transcript_src  TEXT,                           -- autosubs|whisper
    hook            TEXT,
    key_facts       TEXT,                           -- JSON array
    claims          TEXT,                           -- JSON array
    why_trending    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scripts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        INTEGER NOT NULL REFERENCES stories(id),
    brand_id        TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    body_ar         TEXT NOT NULL,
    beats           TEXT,                           -- JSON: [{text, broll_keywords}]
    description_en  TEXT,
    hashtags        TEXT,
    similarity      REAL,
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft|passed|rejected|superseded
    edit_note       TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS videos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id       INTEGER NOT NULL REFERENCES scripts(id),
    voice_path      TEXT,
    subtitle_path   TEXT,
    video_path      TEXT,
    duration_s      REAL,
    broll_manifest  TEXT,                           -- JSON: clip ids/urls/licenses
    status          TEXT NOT NULL DEFAULT 'pending',-- pending|voiced|rendered|failed
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        INTEGER NOT NULL REFERENCES videos(id),
    decision        TEXT NOT NULL,                  -- approved|rejected|edit|new_broll|revoice
    note            TEXT,
    decided_by      TEXT,
    telegram_msg_id INTEGER,
    decided_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        INTEGER NOT NULL REFERENCES videos(id),
    approval_id     INTEGER NOT NULL REFERENCES approvals(id),
    platform        TEXT NOT NULL,                  -- instagram|facebook|youtube|tiktok_export
    external_id     TEXT,
    url             TEXT,
    status          TEXT NOT NULL DEFAULT 'queued', -- queued|published|failed|exported
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    published_at    TEXT,
    UNIQUE (video_id, platform)
);

CREATE TABLE IF NOT EXISTS metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         INTEGER NOT NULL REFERENCES posts(id),
    collected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER,
    likes           INTEGER,
    comments        INTEGER,
    shares          INTEGER,
    avg_watch_s     REAL,
    retention_pct   REAL
);

-- Key/value control flags, e.g. publishing_paused (Telegram /pause kill switch).
CREATE TABLE IF NOT EXISTS control (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_flag(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM control WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_flag(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO control (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
        (key, value),
    )
    conn.commit()


def publishing_paused(conn: sqlite3.Connection) -> bool:
    return get_flag(conn, "publishing_paused", "0") == "1"
