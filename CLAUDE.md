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
| 5 Voice | ✅ done | `5aba7c1` | live: 5/5 voiced, 44–53s at +10%, −14.2 LUFS / −1.5 dBTP; Gemini transcription of a clip matched the script word for word |
| 6 Assemble | ⏭ next | | see plan below — **needs** `ffmpeg-full` (libass) + a Pexels or Pixabay key |
| 7 Telegram review | ⬜ | | |
| 8 Publish | ⬜ | | |
| 9 Analytics + runner | ⬜ | | |

Keys in `.env`: `GEMINI_API_KEY` only. Missing: YouTube, Reddit, Groq, Pexels, Pixabay, Telegram, Meta, YouTube OAuth.
Ollama is not installed. Homebrew `ffmpeg` 8.1.2 here has **no libass/drawtext** (subtitles impossible) → Phase 6.

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
- **Script length 85–115 words, not the brief's 110–150**: measured Arabic neural TTS speaks ~1.85 words/s at +0%
  (~2.05 at the brand's +10%), so 150 words ≈ 73s. 95–105 words ≈ 45–53s.
- Voice: edge-tts 7.x (no custom SSML; one sentence per beat for pauses; `boundary="WordBoundary"`). Arabic voices
  read digits correctly (verified by transcription), so no digits→words step. Dates like "28 ديسمبر 2025" come
  back as ONE timing token — subtitle code must not assume one token per whitespace word. Output
  `assets/generated/voice/<script_id>.wav` + `.words.json` (`words[{text,start,end}]`, `beats[{role,start,end}]`);
  `videos.voice_path` is repo-relative. >58s → one re-synthesis at a computed faster rate (≤ +25%), else `failed`.

## Plan — Phase 6 (Assemble)

**Prerequisites (user):** `brew install ffmpeg-full` (keg-only; set `FFMPEG_BIN` to its `bin/ffmpeg`) — the
current ffmpeg can't render subtitles. And `PEXELS_API_KEY` and/or `PIXABAY_API_KEY` (both free) for b-roll.
Fallback if ffmpeg-full is refused: render each subtitle line to a transparent PNG with Pillow (+ raqm for
Arabic shaping) and `overlay` it — more code, same result.

Input: `videos` with `status='voiced'`. Output: `assets/generated/video/<video_id>.mp4`, H.264 1080×1920 30fps,
≤ `video.max_seconds`, status `rendered`, `subtitle_path`, `broll_manifest` (clip ids, URLs, licenses).
- **B-roll** (`src/assemble/broll.py`): per beat, search Pexels (then Pixabay) videos with the beat's
  `broll_keywords`, portrait first, else landscape center-cropped; 1–2 clips per beat, cut to the beat span from
  `.words.json`. Cache downloads in `assets/stock/` keyed by provider+id; never re-download. Record license/URL.
- **Subtitles** (`subtitles.py`): ASS, Noto Naskh Arabic (download OFL font into `assets/fonts/`, committed),
  2 lines max, bottom safe area (~y 1400–1650), RTL, word-by-word highlight from word timings. Group timing
  tokens into lines of ~4–6 words, breaking at beat boundaries.
- **Render** (`render.py`): concat b-roll → scale/crop 1080×1920 → burn ASS → voice + optional CC0 music from
  `assets/music/` ducked under voice (sidechaincompress) → 2s brand end-card (placeholder logo/text).
- **Guardrail**: the renderer resolves every input path and refuses anything outside `config.ALLOWED_MEDIA_DIRS`
  (`assets/stock`, `assets/generated`) — plus fonts/music, which need adding to an allow-list for non-footage.
- Tests: ASS builder (RTL text, line grouping, timing), guardrail rejects outside paths, broll search/cache with
  MockTransport, ffmpeg command construction; one tiny real render (2s, color source) as the snapshot test.

## Later phases — watch-outs

- Phase 6 guardrail: assembler may only read from `config.ALLOWED_MEDIA_DIRS` (`assets/stock`, `assets/generated`).
- Phase 8: no publish without an `approvals` row; respect `db.publishing_paused()`.
- YouTube quota (10k/day) is shared between discovery (~1,620/day) and Shorts uploads (~1,600 each).
