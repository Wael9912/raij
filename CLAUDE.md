# Ra'ij — trend-to-Arabic-shorts engine

The full spec is `CLAUDE_CODE_BUILD_BRIEF_v2_raij-shorts.md` (gitignored, local only). Read it before
starting a phase; this file tracks **status, decisions, and the plan**, and wins where they differ.
To continue work, use the `raij-phase` skill (`.claude/skills/raij-phase/SKILL.md`).

## Status (updated 2026-09-22)

| Phase | State | Commit | Notes |
|---|---|---|---|
| 0 Scaffold | ✅ done | `94c9c31` | config loader, SQLite schema, CLI, SETUP.md draft |
| 1 Discovery | ✅ done | `3c5728d`, `a877314` | live: 141 candidates/run keyless (Trends + 6 RSS). YouTube/Reddit coded + tested with mocks, **never run live** (no keys yet) |
| 2 Rank & Select | ✅ done | `40288ba`, `e59f351` | live with Gemini: 30 screened → 5 selected, political flagged |
| 3 Extract | ⏭ next | | see plan below |
| 4 Script | ⬜ | | |
| 5 Voice | ⬜ | | |
| 6 Assemble | ⬜ | | |
| 7 Telegram review | ⬜ | | |
| 8 Publish | ⬜ | | |
| 9 Analytics + runner | ⬜ | | |

Keys in `.env`: `GEMINI_API_KEY` only. Missing: YouTube, Reddit, Groq, Pexels, Pixabay, Telegram, Meta, YouTube OAuth.
Ollama is not installed.

## Commands

```bash
uv run pytest -q                              # all tests (mocked network; must stay offline)
uv run python -m src.main <cmd> [--dry-run]   # discover | rank | extract | ... | run-daily | pause | resume
sqlite3 data/pipeline.db "select source, status, count(*) from candidates group by 1,2;"
```

## Conventions

- Every stage: `--dry-run` makes no network calls and no writes; records a row in `runs`
  (`ok|partial|failed`, JSON `notes`); one source/item failing never kills the stage.
- Stage code lives in `src/<stage>/runner.py`, wired in `src/main.py` via `HANDLERS`.
- HTTP goes through `src.discover.common.request()` (retries 429/5xx, never logs query strings/keys).
- All LLM calls go through `src/llm.py` (`complete`, `complete_json`, `load_prompt`); prompts are
  editable `src/prompts/*.txt` with `{{placeholders}}`. Gemini → Groq → Ollama fallback.
- Schema changes: add to `SCHEMA` **and** `MIGRATIONS` in `src/db.py` (existing DB is migrated by ALTER).
- Tests use `httpx.MockTransport`; fixtures blank LLM keys (`setenv(key, "")`) so the real `.env` never leaks in.
- Verify each phase **live on real data** after tests pass, fix what that reveals, then commit per phase
  with the Co-Authored-By trailer. Update the status table above.

## Decisions so far

- pytrends is archived → Google Trends via public trending RSS (`trends.google.com/trending/rss`); no SD feed.
- Default RSS feeds (BBC Arabic, Sky News Arabia, AIT News, BBC Tech, Ars Technica, ScienceDaily) so
  discovery works keyless.
- Score parts are **per-source percentiles**; sources without a signal (RSS views) get neutral 0.5.
  Recency halves every 24h.
- Retellability + category + `topic` slug in one batched LLM call; one daily pick per topic
  (same story from two outlets isn't picked twice); max 2 per category; `political` → `flagged`.
- Daily selection is idempotent per UTC day (`candidates.selected_at`); report in `data/rank/<date>.json`.
- `GEMINI_MODEL=gemini-flash-latest` (gemini-2.5-flash is closed to new keys). Cloud LLMs retry 4× with backoff.

## Plan — Phase 3 (Extract)

Input: today's `status='selected'` candidates. Output: one `stories` row each (hook, 3–5 key facts,
claims list, why-trending); candidate status → `extracted`. **No media file may persist.**

Source text differs by candidate type — most picks are articles/trends, not videos:
- **youtube**: yt-dlp `--write-auto-subs --skip-download` (ar/en) → parse VTT to plain text.
  Fallback: audio to a temp dir → faster-whisper (CPU, int8, `small`) → delete audio in `finally`.
- **rss**: fetch the article URL, extract main text (e.g. `trafilatura`), fall back to feed summary.
- **trends**: fetch the linked news articles in `raw.news` (top 2–3), concatenate.
- **reddit**: selftext, else the linked URL's article text.

Then `src/prompts/story_distill.txt` → `complete_json` → story card. Store source text in
`stories.transcript` (for Phase 4's similarity check) and `transcript_src` (`autosubs|whisper|article|news|selftext`).
Tests: VTT parsing, temp-audio deletion even on error, per-type routing with mocks, dry run.
Open question: story cards should be written in Arabic or English? (default: English card, Arabic script in Phase 4).

## Later phases — watch-outs

- Phase 4 similarity gate compares script vs `stories.transcript`; the transcript must never be in the script prompt.
- Phase 6 guardrail: assembler may only read from `config.ALLOWED_MEDIA_DIRS` (`assets/stock`, `assets/generated`).
- Phase 8: no publish without an `approvals` row; respect `db.publishing_paused()`.
- YouTube quota (10k/day) is shared between discovery (~1,620/day) and Shorts uploads (~1,600 each).
