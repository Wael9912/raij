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
| 6 Assemble | 🟡 built | `7223ffd` | real render verified (live voice + generated test clips, subs in sync). **Live b-roll blocked: no Pexels/Pixabay key** — run `assemble` once a key is in `.env` |
| 7 Telegram review | ⏭ next | | see plan below — needs `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` |
| 8 Publish | ⬜ | | |
| 9 Analytics + runner | ⬜ | | |

Keys in `.env`: `GEMINI_API_KEY` only. Missing: YouTube, Reddit, Groq, Pexels, Pixabay, Telegram, Meta, YouTube OAuth.
Ollama is not installed. Homebrew `ffmpeg` 8.1.2 here has **no libass/drawtext** — subtitles are drawn in Python instead.

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
- Assemble: **no libass** — subtitles are PNGs from Pillow with pure-Python shaping (arabic-reshaper +
  python-bidi; Pillow's wheel lacks raqm), per-word RTL layout with LTR runs for Latin/digits, Noto Naskh Arabic
  Bold + Noto Sans Bold fallback (bundled, OFL). One PNG per spoken word (highlight), played via the concat
  demuxer as a single overlay. Cues ≤2 balanced lines, ≤7 words, never across a beat or a >0.6s pause.
  Subtitle band top y=1250 (clear of platform UI). B-roll: Pexels→Pixabay, portrait first, ≤2 clips/beat,
  `-stream_loop` so short clips loop. Guardrail = `render.guard()` against `config.ALLOWED_MEDIA_SUBDIRS`
  (stock, generated, music), symlinks resolved. Output `assets/generated/video/<video_id>.mp4` + `.srt`.
  Test-pattern footage made a 60 MB file; real footage is smaller, but Telegram bots can only send ≤50 MB.

## Plan — Phase 7 (Telegram review)

**Prerequisites (user):** `TELEGRAM_BOT_TOKEN` (@BotFather) + `TELEGRAM_CHAT_ID` (SETUP.md §4). First, once a
stock key exists, run `assemble` live and look at the 5 real videos (b-roll relevance, crop, file size).

- Plain Bot API over httpx (`src/review/telegram.py`) instead of python-telegram-bot: one less dependency, and
  MockTransport tests like every other stage. Long polling (`getUpdates`), no webhook/server needed.
- `review`: for each `rendered` video with no approval → `sendVideo` (≤50 MB; if bigger, re-encode a preview
  copy with `-maxrate 5M` into `assets/generated/video/<id>.preview.mp4`) with caption = hook + description +
  hashtags + sources, and the full Arabic script as a follow-up message; inline keyboard
  [✅ Approve] [❌ Reject] [✏️ Edit script] [🔁 New b-roll] [🎙 Re-voice]. Store `telegram_msg_id`.
- `bot` (long-running) handles callbacks — **only from `TELEGRAM_CHAT_ID`**, everything else ignored:
  approve/reject → `approvals` row (`decided_by` = Telegram user id); edit → ForceReply for the note →
  `write_script(edit_note=…)` → voice → assemble → resend; new b-roll → assemble again excluding the previous
  manifest's clip ids; re-voice → alternate brand voice (e.g. ar-SA-HamedNeural) → assemble → resend.
  Commands `/pause`, `/resume`, `/status` (counts per stage). Every state in the DB, so restarts lose nothing.
- Regenerated versions supersede the old video row (keep history); approvals point at the exact video shown.
- Tests: callback auth rejects other chats, each button's DB transition, edit-note flow, oversized-video preview
  path, pause/resume, dry run (prints what would be sent).

## Later phases — watch-outs

- Phase 6 guardrail: assembler may only read from `config.ALLOWED_MEDIA_DIRS` (`assets/stock`, `assets/generated`).
- Phase 8: no publish without an `approvals` row; respect `db.publishing_paused()`.
- YouTube quota (10k/day) is shared between discovery (~1,620/day) and Shorts uploads (~1,600 each).
