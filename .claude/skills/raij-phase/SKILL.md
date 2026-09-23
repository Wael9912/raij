---
name: raij-phase
description: Continue building the Ra'ij trend-to-Arabic-shorts pipeline — start/finish the next build phase or the pending polish task (discover, rank, extract, script, voice, assemble, review, publish, analytics). Use when the user says "start phase N", "next phase", "start next phase", "continue", "start", or "check" in this repo.
---

# Build the next Ra'ij phase

0. **"check"** means: report live state, don't build. The pipeline runs on GitHub Actions:
   `gh run list -R Wael9912/raij --workflow raij.yml --limit 10` (note the real gaps between scheduled runs)
   and `gh run view <id> --log | grep raij`. The live DB is only in the Actions cache; the local
   `data/pipeline.db` is stale unless restored from the weekly Telegram backup. Say what changed since the last
   report and what the owner still has to do. Never start the Mac bot/pipeline while Actions is enabled.

1. **Orient.** Read `CLAUDE.md`: the status table, the **"Next up"** phase table, conventions, decisions. For
   phases 10–15 read the matching section of `AUDIT_2026-09-23.md` (local, gitignored; findings carry ids like
   A1/S1/U1 — reference them in commits). For the original phases read
   `CLAUDE_CODE_BUILD_BRIEF_v2_raij-shorts.md`. Run `git log --oneline -5`, `git status`, `uv run pytest -q`.
   Check which keys exist: `grep -E "^[A-Z_]+=." .env | sed 's/=.*//'` (never print values).
   Phase 12 needs the owner's answers to audit section D (niche, market, dialect/voice, affiliate series) —
   ask for them first, don't assume.

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
- Live verification of bot/tick changes: run on a *copy* of a restored DB locally with `tick --dry-run` and the
  mocked tests, then push and watch the next Actions run (`workflow_dispatch` to force one). Never run a second
  Telegram poller against the live bot token while Actions is enabled.
- Code changes reach production by `git push` to main (CI tests run; the next tick uses the new code). Commit
  per phase or per fix, then update the phase table in `CLAUDE.md` and the project memory.
