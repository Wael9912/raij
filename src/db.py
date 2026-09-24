"""SQLite storage: schema + connection helper."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

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
    source        TEXT NOT NULL,                    -- youtube|reddit|trends|rss|wiki|manual
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
    selected_at   TEXT,                             -- schedule.timezone local time; when rank picked it for the day
    topic         TEXT,                             -- LLM story slug; one pick per topic
    audience_fit  INTEGER,                          -- 1–5 from the screen: how much the target audience cares (Phase 12)
    evergreen     INTEGER,                          -- 0/1: still interesting in a month
    ad_safe       INTEGER,                          -- 0/1: advertiser-friendly; 0 → rejected
    format        TEXT,                             -- story|list|howto|explainer|fact
    attempts      INTEGER NOT NULL DEFAULT 0,       -- retryable extract/script failures so far (A6)
    wanted        TEXT,                             -- JSON: owner's ask (formats, platforms, kind, text) — src/formats.py
    status        TEXT NOT NULL DEFAULT 'new',      -- new|ranked|selected|rejected|flagged|extracted|extract_failed|scripted|script_rejected|expired
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS stories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id),
    transcript      TEXT,                           -- kept only for similarity check
    transcript_src  TEXT,                           -- autosubs|whisper|article|news|summary|headlines|selftext
    sources         TEXT,                           -- JSON array of URLs the text came from
    hook            TEXT,
    key_facts       TEXT,                           -- JSON array
    claims          TEXT,                           -- JSON array
    why_trending    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS stories_candidate ON stories (candidate_id);

CREATE TABLE IF NOT EXISTS scripts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        INTEGER NOT NULL REFERENCES stories(id),
    brand_id        TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    kind            TEXT NOT NULL DEFAULT 'short',  -- short|long (src/formats.py)
    body_ar         TEXT NOT NULL,
    beats           TEXT,                           -- JSON: [{text, broll_keywords}]
    description_en  TEXT,
    hashtags        TEXT,
    similarity      REAL,
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft|passed|rejected|superseded|expired
    edit_note       TEXT,
    notes           TEXT,                           -- JSON: words, gate result, reject reason, shared phrases, attempts
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
    status          TEXT NOT NULL DEFAULT 'pending',-- pending|voiced|rendered|in_review|approved|published|expired|rejected|superseded|failed
    notes           TEXT,                           -- JSON: voice, rate, LUFS, warnings, failure reason
    parent_id       INTEGER,                        -- video this one regenerates (edit/new b-roll/re-voice)
    review_msg_id   INTEGER,                        -- Telegram message holding the review buttons
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
    last_attempt_at TEXT,                           -- UTC; retries back off from here (A8)
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

-- Daily API unit spend, e.g. YouTube Data API (10,000 units/day per Google Cloud project).
CREATE TABLE IF NOT EXISTS api_quota (
    day     TEXT NOT NULL,                              -- in the API's reset timezone
    api     TEXT NOT NULL,
    units   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, api)
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
    conn = sqlite3.connect(db_path, timeout=30)          # bot, daily run and publish job share the DB
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added after a table first shipped: (table, column, definition).
# SQLite can't ALTER ADD a column with a non-constant default, so these use a constant.
MIGRATIONS = [
    ("candidates", "last_seen_at", "TEXT"),
    ("candidates", "selected_at", "TEXT"),
    ("candidates", "topic", "TEXT"),
    ("stories", "sources", "TEXT"),
    ("scripts", "notes", "TEXT"),
    ("videos", "notes", "TEXT"),
    ("videos", "parent_id", "INTEGER"),
    ("videos", "review_msg_id", "INTEGER"),
    ("candidates", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("posts", "last_attempt_at", "TEXT"),
    ("candidates", "audience_fit", "INTEGER"),
    ("candidates", "evergreen", "INTEGER"),
    ("candidates", "ad_safe", "INTEGER"),
    ("candidates", "format", "TEXT"),
    ("candidates", "wanted", "TEXT"),
    ("scripts", "kind", "TEXT NOT NULL DEFAULT 'short'"),
]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, definition in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    # last_seen_at arrived via ALTER (no default), so older rows may lack it.
    conn.execute("UPDATE candidates SET last_seen_at = discovered_at WHERE last_seen_at IS NULL")
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


def expire_stale(conn: sqlite3.Connection, max_age_days: float) -> dict[str, int]:
    """Retire work that has waited longer than `max_age_days` at any stage before review (A6): a trend that
    old is no longer worth the 20 Gemini calls, a voice or render retry, or the stock re-download — and
    after an outage the fresh picks should get the quota, not the backlog. Candidates still selected/extracted
    → 'expired'; passed scripts never voiced → 'expired'; voiced videos never rendered → 'failed'."""
    cutoff = (f"-{float(max_age_days) * 24:.0f} hours",)
    with conn:
        cands = conn.execute(
            "UPDATE candidates SET status = 'expired' WHERE status IN ('selected', 'extracted') "
            "AND selected_at < datetime('now', ?)", cutoff).rowcount
        scripts = conn.execute(
            "UPDATE scripts SET status = 'expired' WHERE status = 'passed' AND created_at < datetime('now', ?) "
            "AND NOT EXISTS (SELECT 1 FROM videos v WHERE v.script_id = scripts.id)", cutoff).rowcount
        videos = conn.execute(
            "UPDATE videos SET status = 'failed', notes = json_set(coalesce(notes, '{}'), '$.reason', 'expired') "
            "WHERE status = 'voiced' AND created_at < datetime('now', ?)", cutoff).rowcount
    return {"candidates": cands, "scripts": scripts, "videos": videos}


def start_run(conn: sqlite3.Connection, command: str) -> int:
    """Open the `runs` row every stage records (status `running`); `finish_run` closes it. A run killed
    half-way (`install-services` reloading the job, a crash, the Mac shut down) leaves its row `running` for
    ever — such rows of this command are closed as failed/interrupted first, so the digest reports them."""
    conn.execute("UPDATE runs SET status = 'failed', finished_at = datetime('now'), "
                 "notes = json_set(coalesce(notes, '{}'), '$.interrupted', 1) "
                 "WHERE command = ? AND status = 'running' AND started_at < datetime('now', '-6 hours')", (command,))
    run_id = conn.execute("INSERT INTO runs (command) VALUES (?)", (command,)).lastrowid
    conn.commit()
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, notes: Any) -> None:
    """Close a stage's `runs` row: status `ok|partial|failed`, notes as JSON."""
    conn.execute("UPDATE runs SET finished_at = datetime('now'), status = ?, notes = ? WHERE id = ?",
                 (status, json.dumps(notes, ensure_ascii=False), run_id))
    conn.commit()
