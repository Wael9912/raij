---
name: raij-phase
description: Continue building the Ra'ij trend-to-Arabic-shorts pipeline — start/finish the next build phase (discover, rank, extract, script, voice, assemble, review, publish, analytics). Use when the user says "start phase N", "next phase", "continue", or "start" in this repo.
---

# Build the next Ra'ij phase

1. **Orient.** Read `CLAUDE.md` (status table, conventions, decisions, plan for the next phase) and the
   matching Phase section of `CLAUDE_CODE_BUILD_BRIEF_v2_raij-shorts.md`. Run `git log --oneline -5`,
   `git status`, and `uv run pytest -q`. If the requested phase is already committed, check whether its
   ✅ acceptance criterion was actually met on real data before building anything new.

2. **Build** following the conventions in `CLAUDE.md`:
   - `src/<stage>/runner.py` + wire the handler in `src/main.py`; `--dry-run` = no network, no writes.
   - Record a `runs` row with status `ok|partial|failed` and JSON notes; per-item failures don't kill the stage.
   - LLM via `src/llm.py` only; prompts in `src/prompts/<name>.txt`.
   - Schema changes go in both `SCHEMA` and `MIGRATIONS` (`src/db.py`).
   - Respect guardrails: no source media persists or reaches output; nothing publishes without an approval row.

3. **Test** with `httpx.MockTransport` (tests must never hit the network or read real keys). Cover the
   happy path, graceful degradation, idempotent re-runs, and `--dry-run`.

4. **Verify live** on `data/pipeline.db` with whatever keys exist in `.env`. Read the real output
   critically (duplicates, junk items, wrong categories, quota/rate errors) and fix what it reveals.
   If a needed key is missing, build + test anyway and say exactly which key unblocks the live run.

5. **Commit** per phase (message lists what/why; end with the Co-Authored-By trailer from the system
   reminder). Then update the `CLAUDE.md` status table, decisions, and write the plan for the next phase.

6. **Report** to the user: what works (with real numbers), what's untested live and why, what they need
   to do (keys, installs), and ask before starting the next phase.
