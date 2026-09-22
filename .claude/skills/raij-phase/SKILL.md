---
name: raij-phase
description: Continue building the Ra'ij trend-to-Arabic-shorts pipeline — start/finish the next build phase or the pending polish task (discover, rank, extract, script, voice, assemble, review, publish, analytics). Use when the user says "start phase N", "next phase", "start next phase", "continue", "start", or "check" in this repo.
---

# Build the next Ra'ij phase

0. **"check"** means: report live state, don't build. Read the review bot's log if one is running
   (background task output), then `sqlite3 data/pipeline.db` for `videos` statuses, `approvals`, `control`
   flags, and say what changed since the last report and what the owner still has to do.

1. **Orient.** Read `CLAUDE.md`: the status table, the **"Next up"** section (owner feedback is done before
   the next numbered phase), conventions, decisions, and the phase plan. Read the matching phase in
   `CLAUDE_CODE_BUILD_BRIEF_v2_raij-shorts.md`. Run `git log --oneline -5`, `git status`, `uv run pytest -q`.
   Check which keys exist: `grep -E "^[A-Z_]+=." .env | sed 's/=.*//'` (never print values).

2. **Build** following the conventions in `CLAUDE.md`:
   - `src/<stage>/runner.py` + wire the handler in `src/main.py`; `--dry-run` = no network, no writes.
   - Record a `runs` row with status `ok|partial|failed` and JSON notes; per-item failures don't kill the stage.
     Deterministic failures get a `failed` status; network/LLM trouble writes nothing so the next run retries.
   - LLM via `src/llm.py` only; prompts in `src/prompts/<name>.txt`.
   - Schema changes go in both `SCHEMA` and `MIGRATIONS` (`src/db.py`).
   - Guardrails: no source media reaches output (`render.guard`); nothing publishes without an approval row for
     that exact video; faceless stock + only freely licensed Commons photos of public figures.

3. **Test** with `httpx.MockTransport` (tests must never hit the network or read real keys). Cover the
   happy path, graceful degradation, idempotent re-runs, and `--dry-run`.

4. **Verify live, and look at it.** Run the stage on `data/pipeline.db` with the keys that exist. Then inspect
   the real output critically: read scripts and card text; for video extract frames into a contact sheet
   (ffmpeg `hstack`; zsh needs an args array) and Read the image; for audio, transcribe a clip via Gemini.
   Most real bugs here were only visible this way (wrong numbers, stray glyphs, strangers' faces, huge clips).
   For flows that mutate state (review regeneration), test on a copy of the DB and delete the files it created.
   If a key is missing, build + test anyway and say exactly which key unblocks the live run.

5. **Commit** per phase or per fix (message says what and why, including what live testing found; end with the
   Co-Authored-By trailer from the system reminder). Update the `CLAUDE.md` status table, decisions, and the
   plan for what comes next. Update the project memory's status line.

6. **Report** briefly: what works (real numbers), what was found and fixed, what's untested live and why,
   what the owner must do (exact links/steps for keys), then ask before starting the next phase.

## Owner notes
- Terse messages. Keys get pasted into chat: validate each only against its own service, save it into `.env`
  without echoing it, and remind them it's in the chat log.
- The Telegram review bot is not a service yet — start `uv run python -m src.main bot` in the background
  when reviews are pending, and restart it after changing `src/review/`.
