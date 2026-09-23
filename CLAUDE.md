# Ra'ij — trend-to-Arabic-shorts engine

The full spec is `CLAUDE_CODE_BUILD_BRIEF_v2_raij-shorts.md` (gitignored, local only). Read it before
starting a phase; this file tracks **status, decisions, and the plan**, and wins where they differ.
To continue work, use the `raij-phase` skill (`.claude/skills/raij-phase/SKILL.md`).

## Status (updated 2026-09-23)

| Phase | State | Commit | Notes |
|---|---|---|---|
| 0 Scaffold | ✅ done | `94c9c31` | config loader, SQLite schema, CLI, SETUP.md draft |
| 1 Discovery | ✅ done | `3c5728d`, `a877314` | live: 141 candidates/run keyless (Trends + 6 RSS). YouTube/Reddit coded + tested with mocks, **never run live** (no keys yet) |
| 2 Rank & Select | ✅ done | `40288ba`, `e59f351` | live with Gemini: 30 screened → 5 selected, political flagged |
| 3 Extract | ✅ done | `7672048`, `affe555` | live: 5/5 story cards (3 trends via news articles, 2 RSS articles). yt-dlp subs verified live on a real video; whisper fallback mocked only (`uv sync --group whisper` not installed) |
| 4 Script | ✅ done | `d860e76`, `dde9d8c` | live: 5/5 passed (3.5/3.6-flash); Arabic-source similarity 0.06–0.10; number gate caught 211→201, 953,531→995,000 |
| 5 Voice | ✅ done | `5aba7c1` | live: 5/5 voiced, 44–53s at +10%, −14.2 LUFS / −1.5 dBTP; Gemini transcription of a clip matched the script word for word |
| 6 Assemble | ✅ done | `7223ffd`, `e1e6dd9`, `cf0b0b3` | live with Pexels: 5/5 rendered, 45–54s, 7–10 clips, 18–37 MB; faceless b-roll + licensed Commons photos of public figures |
| 7 Telegram review | ✅ done | `d27ce54`, `47b385c` | live with @Raig88_bot: approve/reject/edit/new b-roll all used by the owner; #13 #17 #19 approved, #14 #16 rejected. Late-tap bug found live + fixed |
| 6.5 Visual polish | ✅ done | `398a58c` | Cairo Black via raqm, xfade transitions, hook title + series badge, logo, progress bar. #13/#17/#19 re-rendered as #20/#21/#22 → **awaiting owner re-approval** |
| 8 Publish | 🟡 YouTube + TikTok live | `191dbef` | 2026-09-22: #20 #21 #23 live as public Shorts on channel رائج (UCeLlvJwQe-uj4IEZEsO3YIw), processed OK, not locked to private; TikTok exported. **IG/FB not run live** — Meta keys pending |
| 9 Analytics + runner | ✅ done | `096c920` | live: YouTube metrics for 3 Shorts, weekly report sent; launchd bot/daily/publish installed and running. IG/FB insights mock-only |

Keys in `.env`: `GEMINI_API_KEY`, `PEXELS_API_KEY`, `TELEGRAM_BOT_TOKEN` (@Raig88_bot), `TELEGRAM_CHAT_ID` (owner's private chat).
Missing: YouTube API key (discovery), Reddit, Groq, Pixabay, Meta. YouTube OAuth ✅ (`data/youtube.token.json`).
**Runs on GitHub Actions** (`.github/workflows/raij.yml`, `tick` command); Mac launchd services are uninstalled
(`install-services` still exists for a fallback). Never run a second Telegram poller (two pollers fight over getUpdates).
Ollama is not installed. Homebrew `ffmpeg` 8.1.2 here has **no libass/drawtext** — subtitles are drawn in Python instead.
`libraqm` is installed via Homebrew (Arabic shaping for Pillow, see `src/textshape.py`).

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
- Script: card-only prompt (`script_write.txt`); model asked for min+10..max−10 words, `script.min/max_words` accepted
  (models undershoot; see the 85–115 bullet below).
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
- Assemble: **no libass** — subtitles are PNGs from Pillow, shaped by **HarfBuzz (raqm)**: Pillow's wheel bundles raqm
  but dlopens `libfribidi.dylib` by bare name, which dyld doesn't find in /opt/homebrew/lib, so
  `textshape.ensure()` imports `PIL._imagingft` once from a temp cwd holding a symlink (dyld searches cwd for leaf
  names; DYLD_* env re-exec broke pytest's fd capture). Must run before any `PIL.ImageFont` import — `brand.py`,
  `main()` and `tests/conftest.py` call it. arabic-reshaper/python-bidi removed (presentation forms lacked glyphs
  in Cairo/Tajawal/Almarai). Per-word RTL layout with LTR runs for Latin/digits. One PNG per spoken word, played
  via the concat demuxer as a single overlay. Cues ≤2 balanced lines, ≤7 words, never across a beat or a >0.6s
  pause. Subtitle band top y=1250 (clear of platform UI). B-roll: Pexels→Pixabay, portrait first, ≤2 clips/beat,
  `-stream_loop` so short clips loop. Guardrail = `render.guard()` against `config.ALLOWED_MEDIA_SUBDIRS`
  (stock, generated, music), symlinks resolved. Output `assets/generated/video/<video_id>.mp4` + `.srt`.
  Real videos are 23–38 MB; Telegram bots can only send ≤50 MB → preview copy in Phase 7.
- **Look (Phase 6.5, owner-picked):** font **Cairo Black** (variable font at wght 900, `assets/fonts/Cairo-Variable.ttf`)
  for everything (`brand.font`); subtitles 92px, stroke 7, spoken word on a **yellow pill** (#FFD400, dark text).
  Cuts are an `xfade` chain (0.3s, `video.transition_seconds`, types cycle through `render.TRANSITIONS`, fade into
  the end card): every segment is trimmed `seconds + d`, xfade k's offset = sum of the first k segments' nominal
  lengths, so cut times and total length are unchanged. **Hook title**: `scripts.notes.hook_title` (≤6 Arabic words,
  from `script_write.txt`; `write.clean_title` drops a bad one rather than failing the draft), pops in (6-frame
  scale/alpha) with the **series badge** above it, center y≈820, for `video.hook_title_seconds` (2.5s); subtitles
  stay hidden until it's gone. Title shrinks from 118px until it fits 2 lines (a 3rd line was silently dropped at
  first — caught on the contact sheet). Series = script's pick if it's in `brands[].series`, else by category.
  `script/titles.py` backfills titles for older passed scripts with live videos (one batched LLM call, run at the
  end of the `script` stage). **Logo**: `assets/brand/logo.png` if present, else a drawn "رائج" wordmark pill, 75%
  opacity at (48,150), hidden on the end card. Thin yellow **progress bar** at y=0. End card: wordmark + series
  badge + "تابعنا للمزيد".
- B-roll selection: one clip per ~7s of a beat (≤4), clips >60s skipped, shortest-that-fits first, clips used in
  the last 7 days sort last; cache pruned to clips used in the last 14 days (~6 MB/clip).
- **Faceless b-roll + real photos (owner's decision):** stock strangers read as the story's real person. Keywords
  naming people are dropped (`write._PERSON_WORD`); candidate clips' preview frames go through OpenCV YuNet
  (`assemble/faces.py`, score 0.45, face ≥1.5% of frame → skip). A beat with `"person"` opens on that public
  figure's **Wikimedia Commons** lead photo — only PD/CC0/CC BY/CC BY-SA, never Wikipedia fair-use/local files or
  news-agency photos (copyright). Credit drawn on the frame and kept in `videos.notes.credits` →
  **Phase 7/8 must append credits to the caption** (CC BY requirement). No free photo → faceless (e.g. Accorsi).
  Known gap: occluded faces (inside a helmet) can pass the detector — human review.
- Working with the owner: terse commands ("start", "check" = poll the bot/DB and report). They paste API keys
  into chat — validate each against its own service only, save to `.env` without echoing, never print keys.
  Always *look* at real output (extract frames → contact sheet → Read the image; transcribe audio via Gemini)
  before calling a stage done — most real bugs this project had were only visible that way.
- Review: plain Bot API over httpx (`review/telegram.py`; token only in the URL path, never in errors). `review`
  sends video (preview re-encode if >50 MB) + caption (hook, description, tags, sources, credits) + script
  message; video → `in_review` with `review_msg_id`. `bot` long-polls; offset in `control.telegram_offset`,
  saved before handling (no replays). Only `TELEGRAM_CHAT_ID` is obeyed. Buttons are removed on the first tap.
  Approve/reject → `approvals` row + video status. Edit → ForceReply prompt, `control.pending_edit`; the next text
  is the note → `write_script(edit_note)` → new script version (old `superseded`, `edit_note` saved) → voice
  (`<script>_v<video>.wav`) → assemble → new video row (`parent_id`) → resent; old video `superseded`.
  New b-roll: same voice, `assemble_video(exclude=old stock ids)`. Re-voice: toggles brand `voice.alt`/`name`.
  Regeneration failure → message + original's buttons restored.

- **Phase 10a (2026-09-23, audit A1/A2/A3/A9/A10):** `main()` touches `data/.changed` in a `finally` for every
  writing command, and the workflow packs state whenever the Tick step didn't succeed (step timeout 150 min), so a
  crash never replays the day. Regeneration creates the replacement `videos` row *before* building and marks it
  `failed` (notes.failed) on any error. **Closing approved videos:** `publish.finalize()` runs after every publish
  pass — approved + older than `publish.max_age_hours` → `published` if any platform went out, else `expired`
  (new terminal status); what was skipped and why is in `videos.notes.publish`. The `finalize` command does it
  now regardless of age, but only for videos whose wanted platforms are each done, unconfigured or out of
  attempts (owner's decision: YouTube done = done; missing Meta keys don't hold a video). Pause notice is sent once
  per pause (`control.paused_notice_sent`, cleared by /resume). YouTube upload first lists the channel's last 25
  uploads (2 quota units) and adopts a ≤7-day-old video with the identical title instead of re-uploading. The
  workflow creates/refreshes the `state-bootstrap` release weekly (it had never existed — a cache eviction would
  have halted the pipeline). `workflow_dispatch` input `command` runs any CLI command on the live state.

## Next up

**Live on GitHub Actions since 2026-09-22 19:07 Cairo** (repo Wael9912/raij, public; Mac services uninstalled).
"check" now = `gh run list -R Wael9912/raij --workflow raij.yml` + `gh run view <id> --log`; the live DB is in the
Actions cache — to inspect it: download the newest cache isn't possible via gh, so run with `workflow_dispatch`
and read the log, or use the weekly encrypted backup from Telegram (`state unpack` with RAIJ_STATE_KEY from .env).
**Never run the Mac pipeline/bot while Actions is enabled** (two Telegram pollers, two diverging DBs). To move back:
`gh variable set RAIJ_ENABLED --body false`, restore the latest state on the Mac, `install-services`.
Code changes: commit + `git push` (CI runs the tests; the next tick uses the new code).

**Known (2026-09-23): GitHub runs the `*/10` cron only every ~2.5 h on this repo** (3 runs in 5.5 h), so taps and the
07:00 daily run lag hours. Phase 10a added a Cloudflare Worker cron (`deploy/cloudflare-trigger/`) that dispatches the
workflow every 10 min — deployed by the owner once (`wrangler login`, `wrangler deploy`, `secret put GH_TOKEN`).
`workflow_dispatch` takes a `command` input (default `tick`): `gh workflow run raij.yml -f command=finalize`.

**Full audit + improvement plan: `AUDIT_2026-09-23.md`** (local only, gitignored — read it before starting a phase).
Sections A code, B security, C bot UX, D content strategy (with the owner decisions needed), E phases 10–15.
"start" / "next phase" now means the next unfinished phase in that plan.

| Phase | Scope | State |
|---|---|---|
| 10a | Ticks & state: A1 tick try/finally, A2 orphan row, A3 expire approved, A9 pause once, A10 bootstrap refresh, external cron trigger | 🟡 code done 2026-09-23; Cloudflare trigger needs owner's `wrangler login` + GitHub token |
| 10b | Gates & retries: A4 number tolerance, A5 shared-run check, A6 attempt/age caps, A7 transient Pexels, A8 backoff, A11–A16 | ⬜ |
| 10c | Security: S1 SHA pins/credential scoping, S2 unpack paths, S3 id regex, S4 owner id/reply check, S5–S8; `stage_run()` dedupe; missing tests | ⬜ next |
| 11 | Bot UX: /queue, digest, why-picked + Arabic title in caption, parent line, expiry, retry button, setMyCommands, per-video pending edit | ⬜ |
| 12 | Content I (needs owner decisions D1–D4): niche/region weights, fit score in classify, posting windows, SEO + playlists, CTA rotation, A/B titles | ⬜ |
| 13 | Pick-before-render via Telegram; real series (templates, quotas, evergreen source) | ⬜ |
| 14 | Topic performance memory, traffic-source metrics, `tools`/affiliate series, second brand | ⬜ |
| 15 | >60 s variants for TikTok/FB, weekly long-form compile | ⬜ |

Still pending from before:
1. **Owner: Meta keys** — paste App ID, App Secret, short-lived token → exchange → add to .env →
   `grep -vE '^(RAIJ_STATE_KEY)=' .env | gh secret set RAIJ_ENV -R Wael9912/raij`. IG/FB start with the first videos
   approved after the keys exist (#20 #21 #23 were closed as `published` on 2026-09-23, owner's call).
2. Watch the first unattended daily run's log: Gemini quota, edge-tts from GitHub IPs, render time.
3. Later: YouTube Data API key (discovery), Pixabay key, CC0 music.

## Phase 8 (Publish) — as built

- `src/publish/runner.py`: `eligible()` = `videos.status='approved'` AND the latest approve/reject row for that exact
  video id is `approved` (edit/new_broll/revoice rows aren't verdicts), approved within `publish.max_age_hours` (72).
  Paused → nothing, plus a Telegram note. Video file goes through `render.guard()`.
- `posts` row per (video, platform), created only when the platform is configured (`missing(cfg)` → reason or None),
  so adding keys later picks up approved videos. `attempts` incremented before each try; failure → `failed` + error,
  retried next runs until `publish.max_attempts` (3), then one Telegram alert. Published/exported never redone.
  All platforms of the brand done → video `published`. Successes and final failures → one Telegram summary.
- Caption (`publish/common.post_text`): hook title (or spoken hook) + description_en + hashtags + "المصادر: domains"
  + "📷 credit" lines (CC BY requirement).
- YouTube (`publish/youtube.py`, plain httpx): `youtube-auth` = loopback OAuth + PKCE, scopes upload + readonly +
  yt-analytics.readonly (Phase 9 needs no re-auth); refresh token in `data/youtube.token.json` (0600). Upload =
  resumable session, 8 MiB `Content-Range` chunks, 308 → resume from server's `Range`. Title = hook title + " #Shorts",
  categoryId by story category, `defaultLanguage=ar`, `selfDeclaredMadeForKids=false`, privacy from config.
- Instagram/Facebook (`publish/meta.py`): IG `/{ig}/media` REELS `upload_type=resumable` → bytes to rupload
  (`Authorization: OAuth`, `offset`, `file_size`) → poll `status_code` → `media_publish` → permalink. FB
  `/{page}/video_reels` start → rupload → finish `video_state=PUBLISHED`. Final publish/finish calls use `retries=0`
  so a flaky 5xx can't double-post (the next run retries the whole post instead). Tokens only in POST bodies/headers.
- TikTok: MP4 + caption .txt copied to `data/export/tiktok/<date>/<video_id>.*` → `exported`.

## Later phases — watch-outs

- Phase 6 guardrail: assembler may only read from `config.ALLOWED_MEDIA_SUBDIRS` (`assets/stock`, `assets/generated`,
  `assets/music`).
- Phase 8: no publish without an `approvals` row; respect `db.publishing_paused()`.
- YouTube quota (10k/day) is shared between discovery (~1,620/day) and Shorts uploads (~1,600 each).
