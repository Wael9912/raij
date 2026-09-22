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
| 3 Extract | ✅ done | `7672048`, `affe555` | live: 5/5 story cards (3 trends via news articles, 2 RSS articles). yt-dlp subs verified live on a real video; whisper fallback mocked only (`uv sync --group whisper` not installed) |
| 4 Script | ✅ done | `d860e76`, `dde9d8c` | live: 5/5 passed (3.5/3.6-flash); Arabic-source similarity 0.06–0.10; number gate caught 211→201, 953,531→995,000 |
| 5 Voice | ⏭ next | | see plan below |
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
- Gemini free quotas are **per model**, and gemini-flash-latest (→ 3.8-flash) allows only 20 req/day. `_gemini`
  falls through `llm.gemini_fallback_models` (flash-lite, gemma-4-31b-it) on 429/503; daily-cap 429s aren't retried.
- Extract: story cards are **English** (Arabic is written in Phase 4). Source text: trafilatura (default mode —
  `favor_precision` dropped real articles), browser UA; trends read up to 3 linked articles that yield ≥400 chars,
  else headlines. Candidate → `extracted`; no usable text / LLM says unusable → `extract_failed`; LLM outage
  leaves it `selected` for the next run. `stories.sources` = URLs used; cards also in `data/extract/<date>.json`.
- faster-whisper is an optional dep group (heavy for 8 GB); only used when a video has no subs and ≤900s.
- LLM chain: GEMINI_MODEL → `llm.gemini_fallback_models` (full flash 3.7/3.6/3.5/3-preview, then lite, Gemma).
  404/429 → model skipped for the run; 503 → 1 retry unless last; unparseable output → same provider re-asked once.
  **flash-lite writes poor Arabic** (typos, stray katakana, wrong numbers) — keep it late in the chain.
- Script: card-only prompt (`script_write.txt`); model asked for 120–140 words, 110–150 accepted (models undershoot).
  Numbers as **digits** so `script/facts.py` can check each against the card (rounding allowed only below the
  figure's last non-zero digit; ≤12 unchecked). Spelled-out figures ("مليون") are not checked — Phase 7 review.
  Similarity = normalized Arabic word-trigram containment (`script/similarity.py`); real retellings score
  0.05–0.15 vs threshold 0.35; English sources skip the gate (similarity NULL). One retry per failed check that
  names the problem; a gate-failed draft is kept as `superseded` beside its rewrite. `write_script(edit_note=)`
  is the hook for Phase 7 "✏️ Edit script".

## Plan — Phase 5 (Voice)

Input: `scripts` with `status='passed'` and no `videos` row. Output: a `videos` row per script
(`voice_path`, `duration_s`, `status='voiced'`) + audio in `assets/generated/voice/<script_id>.mp3` and word
timings in `…/<script_id>.words.json` (Phase 6 needs them for karaoke subtitles).
- `edge-tts` (free, keyless, needs network): brand voice/rate/pitch from `config.brands[].voice`. Custom SSML
  is blocked by the service now — get pauses from punctuation / one beat per sentence break instead. Request
  `WordBoundary` events for timings (newer edge-tts defaults to SentenceBoundary).
- Loudness: ffmpeg `loudnorm` two-pass to `voice.target_lufs` (−14), then measure duration with ffprobe.
- Duration gate 40–60s (`video.max_seconds`): too long → re-synthesize once at a faster rate (+10%); still too
  long → `failed` with reason. Too short → keep, warn.
- Check live how the Arabic voice reads digits ("17,000", "01:11", "953,531"); if badly, add a digits→words
  step for TTS only (the script text in the DB and subtitles keep digits).
- Tests: mock the synthesizer (no network), loudnorm command construction, duration gate paths, idempotent,
  dry run. Output must land only under `assets/generated/` (guardrail for Phase 6).

## Later phases — watch-outs

- Phase 6 guardrail: assembler may only read from `config.ALLOWED_MEDIA_DIRS` (`assets/stock`, `assets/generated`).
- Phase 8: no publish without an `approvals` row; respect `db.publishing_paused()`.
- YouTube quota (10k/day) is shared between discovery (~1,620/day) and Shorts uploads (~1,600 each).
