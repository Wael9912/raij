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
| 13 Pick + produce from the bot | ✅ done | (this commit) | `/trending` pick list → format + platforms → `produce` job; `/topic`, `/script`, `/run`, `/jobs`; Wikipedia top-views source; manual picks outside the daily quota |
| 15 Long-form | ✅ done | (this commit) | `long` format: 1920×1080, 2–5 min, chapters (cards + YouTube timestamps), thumbnail, calmer voice; live: #30 «gold» 3 min from a /topic. `ranking.long_top_n` = 1 automatic long/day |
| 17 TikTok app | ✅ live (sandbox) 2026-09-24 04:13 | `24fe024`… | **first inbox drafts live:** #24 #25 #26 #31 #32 landed in the owner's TikTok inbox (captions in Telegram); TikTok's pending-drafts cap (`spam_risk_too_many_pending_share`, an HTTP **400**) stopped the 6th — now a limit (attempt given back). Sandbox keys in .env; production keys after the app review (SETUP.md §8 step 6) | `publish/tiktok_api.py`: Content Posting API, **inbox mode** (draft in the owner's TikTok inbox + caption to Telegram; owner posts from the app) now, `direct` after TikTok's audit; `tiktok-auth` (Desktop Login Kit, loopback 127.0.0.1:8471, hex-PKCE); public privacy/terms at https://raij.dafatir.workers.dev (`deploy/site/`); SETUP.md §8 |
| 18 Professional فصحى | ✅ 2026-09-24 | (this commit) | owner: "KSA accent and reading very bad — professional standard Arabic script and voice for all future products". Dialect gate (`script/fusha.py`), editor pass + full tashkeel for the TTS (`script/polish.py`, `script_polish.txt`), strict-MSA prompts, voice → `ar-JO-TaimNeural` (bench: 94.5 % words read right vs Hamed 93.6), daily run moved to 10:30 Cairo (after Gemini's quota reset — 07:00 runs got weak fallback models that wrote Egyptian dialect) |
| 13b Post now | ✅ done | (2026-09-24) | audit "approved videos never all post": windows allow 4/day vs ~9 made/day + Meta keys missing + zombie job blocked the bot queue. `/post_now [ids]` (confirm tap) → `videos.notes.post_now` → `publish` job; `publish --now`; `publish.windows.per_window`; zombie-safe `jobs.alive` |
| 19 Performance audit | ✅ 2026-09-24 | (this commit) | the 10:30 daily run took 4 h 16 min because the **Mac slept** (lid closed 10:33 → 14:21; Power Nap woke it 45 s every 16 min without network): 5/8 picks lost to ConnectError, a cut-off edge-tts stream (22/99 words) was rendered as #46 and sent for review. Fixes: `src/power.py` caffeinate while pipeline commands run, `tts.check_complete` (Truncated), outages never count as attempts, **same-day catch-up** (`publish --catch-up` every 30 min → `produce` on leftovers, `pipeline.catch_up_hours` 2), stale `running` runs closed, `videos.notes.timing` per assemble step. Encoder benchmark: libx264 medium ≈ 6× real time on the M3, videotoolbox no faster — not the bottleneck |

Keys in `.env`: `GEMINI_API_KEY`, `PEXELS_API_KEY`, `TELEGRAM_BOT_TOKEN` (@Raig88_bot), `TELEGRAM_CHAT_ID` (owner's private chat).
Missing: YouTube API key (discovery), Reddit, Groq, Pixabay, Meta. YouTube OAuth ✅ (`data/youtube.token.json`; re-auth
pending for the Phase 12 playlist scope).
**Runs on the owner's Mac again since 2026-09-23 17:47 Cairo** (launchd: `com.raij.bot` always on, `com.raij.daily`
10:30, `com.raij.publish --catch-up` every 30 min — `install-services`; **a closed lid on battery still pauses
everything** — pipeline commands only block *idle* sleep). GitHub Actions is the *fallback* only: workflow is
dispatch-only (no cron), Cloudflare Worker cron paused, `RAIJ_ENABLED` should be `false` (the auto-mode classifier
blocked `gh variable set` — owner runs it). Never run a second Telegram poller (two pollers fight over getUpdates).
"check" = `uv run python -m src.main services`, `tail data/logs/{bot,daily,publish,jobs}.log`, and the DB.
Ollama is not installed. Homebrew `ffmpeg` 8.1.2 here has **no libass/drawtext** — subtitles are drawn in Python instead.
`libraqm` is installed via Homebrew (Arabic shaping for Pillow, see `src/textshape.py`).

## Commands

```bash
uv run pytest -q                              # all tests (mocked network; must stay offline)
uv run python -m src.main <cmd> [--dry-run]   # discover | rank | extract | ... | run-daily | pause | resume
uv run python -m src.main trending            # discover + screen, then a Telegram pick list (no auto-selection)
uv run python -m src.main topic "…" [--long|--both] [--platforms youtube,tiktok_export]   # then `produce`
uv run python -m src.main from-script FILE    # voice an owner-written script as written; then `produce`
uv run python -m src.main produce             # extract → script → voice → assemble → review for everything selected
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
- Daily selection is idempotent per **local (schedule.timezone) day** (`candidates.selected_at` is local time since 10b/A16, the same clock as `daily_due`); report in `data/rank/<date>.json`.
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
  saved before handling (no replays). Only `TELEGRAM_CHAT_ID`, and only messages/taps *from* the owner's user id
  (= chat id, or `TELEGRAM_OWNER_ID`), are obeyed. Buttons are removed on the first tap.
  Approve/reject → `approvals` row + video status. Edit → ForceReply prompt, `control.pending_edit`; the next text
  is the note → `write_script(edit_note)` → new script version (old `superseded`, `edit_note` saved) → voice
  (`<script>_v<video>.wav`) → assemble → new video row (`parent_id`) → resent; old video `superseded`.
  New b-roll: same voice, `assemble_video(exclude=old stock ids)`. Re-voice: toggles brand `voice.alt`/`name`.
  Regeneration failure → message + original's buttons restored.
- **Phase 11 (2026-09-23, audit C / U2–U9; owner's calls: English bot UI, never auto-expire cards, bulk commands
  only behind a confirm tap):** caption = `🎬 #id · 48s · series` / `↩️ Replaces #N (what)` + edit note / Arabic
  `hook_title` / `🔎 Why: trending on <source> — rank_reason` (≤180 chars) / description / tags / sources /
  credits; version, similarity and voice sit at the end of the script message. `review` sends a **digest** first
  (titles, flagged candidates and failed/partial runs of the last 24 h). **Edit prompts are per video**
  (`control.pending_edits` = `{vid: {prompt, user, at}}`, 6 h each; the old `pending_edit` is still read once) and
  the card keeps its buttons while a prompt is open. Commands: `/queue` (in review with age; approved with
  `YouTube ✅ · Instagram 🔑 no keys · Facebook ⚠️ 2× · TikTok 📲`), `/status`, `/help`, `/approve_all` and
  `/skip` → list + `ba:<max id>`/`bs:<max id>`/`bx` buttons (cards arriving after the ask are untouched),
  `/report` (no backup — U7). Slash menu via `setMyCommands` once per `cards.COMMANDS` version
  (`control.commands_version`, first tick after a deploy). **Reminders** (`review.runner.remind`, every tick):
  cards in review ≥48 h and ≥`publish.max_age_hours` get one message per level, a `⌛ In review for …` caption
  line (buttons kept) and `videos.notes.reminded_h`; nothing is decided for the owner. Publish notices say
  `✅ #20 «title»` + platform name + link (TikTok: "copy sent above", no repo path); a final failure is its own
  message with a **🔁 Retry** button (`rt:<vid>` → new `approved` row with note `retry`, failed posts reset to
  queued/0 attempts). Weekly report: title line, then numbers line (RTL). Daily-failure notice links the Actions run.

- **Phase 12 (2026-09-23, audit D + E/12; owner's calls D1–D4 above):** `ranking.categories` = tech, money, wow-facts,
  life-hack, tools (selectable); `other_categories` news-lite/sports/culture are still labelled by the screen but
  stay `ranked`, never picked. `classify.txt` also returns `audience_fit` 1–5, `evergreen`, `ad_safe`, `format`
  (candidates columns; `ad_safe=false` → `rejected`); `ranking.audience` names who fit is judged for. **Screen pool
  is round-robin across sources** (`rank.screen_pool`, 45 items = 3 calls): RSS sits at a flat ≈0.59 and never
  reached the model behind 30 Trends terms. **Selection order** = score × `category_weights` × `region_weights`
  (max over "EG,SA"; `region_default_weight` for unknown) × (1 + `fit_weight`·(fit−3)) × (1 + `evergreen_bonus`)
  × winner boost (`rank.Weights`; stored `score` unchanged). Trends geos SA/AE/KW/EG (US dropped: NFL noise);
  feeds = Sky News Arabia ×3, Asharq Al-Awsat economy, AIT, BBC Arabic sci-tech (region AR) + TechCrunch, Verge,
  MIT TR, ScienceDaily, Live Science, Lifehacker, MakeUseOf — all fetched live 2026-09-23 (Al Arabiya/Argaam/CNBC
  Arabia/Maaal have no usable feed; **Arab News answers httpx with a Cloudflare challenge** — not worked around).
  Voice `ar-SA-HamedNeural` (alt Shakir); tone/prompt "MSA with a light Gulf touch". Series added `money`
  "أرقام تهمك", `tools` "أداة اليوم"; `brands[].cta` = closing lines per series, rotated by id (`write.cta_line`):
  the script prompt lists each series with its line, the end card draws it (`videos.notes.cta`). Script also
  returns `hook_title_alt` (A/B: YouTube/TikTok use A = the burnt-in title, Instagram/Facebook captions use B;
  `scripts.notes.hook_title_alt`, `PostText.caption_alt`). **Posting windows** (`publish.windows`: Asia/Riyadh
  08/13/18/21, 150 min): a video's first upload waits for an open window, one video per window (taken = any video's
  first successful post since the window start), oldest approval first; a partly-published video finishes its other
  platforms whenever due; `times: []` = immediate. **SEO**: YouTube title `{hook_title} | {series}` (≤100, no
  "#Shorts" — it's in the description), description = title / spoken Arabic hook / English / tags / sources /
  credits / #Shorts; tags = script tags + `publish.seo_tags[category]` + default (≤15, deduped); after upload the
  Short is added to a public playlist named after its series (`youtube.add_to_playlist`, listed once per process,
  created if missing, best effort). Needs the `youtube` scope (`SCOPES` changed) — see "Still pending". Live on
  the DB copy: 45 screened, picks money ×2 / wow-facts ×2 / life-hack, a crime trend rejected as not ad-safe,
  Saudi National Day labelled culture (fit 5) and therefore not picked — add `culture` to `categories` if wanted.
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
- **Phase 10c (2026-09-23, audit S1–S8):** workflows pin actions to commit SHAs (bump = replace SHA + comment);
  checkout keeps no git credentials (keep-alive pushes with a one-off `http.extraheader`); `GH_TOKEN` and
  `RAIJ_STATE_KEY` are step-scoped — the **Tick step has no repo token**; keys are written *after* the cache restore
  and `rm`'d before the cache save. **State bundle format 2** (`src/state.py`): `RAIJ-STATE-2\n` + HMAC-SHA256 over
  the openssl ciphertext (MAC key = PBKDF2 of RAIJ_STATE_KEY) — a tampered/foreign bundle is rejected before
  decryption; pre-10c bundles (`Salted__`) still unpack with a warning (old Telegram backups). Unpack writes only
  `data/pipeline.db` (→ `cfg.db_path`) and plain files under `assets/generated/`; everything else is skipped and
  logged. Stock download: provider/id must match `[A-Za-z0-9_-]{1,40}`, content-type must be video (or
  octet-stream), ≤200 MB (`broll.MAX_CLIP_BYTES`), `.part` removed on any failure. Bot: sender `from.id` must equal
  the chat id (private chat) or `TELEGRAM_OWNER_ID`; an edit note must be a *reply* to the ✏️ prompt (else a nudge)
  and the prompt expires after 6 h (`bot.EDIT_NOTE_TTL`); callback ids are ASCII digits only. `review.send_card`
  runs `render.guard()`. Rank/extract no longer print reports/cards (public Actions log) — data files only.
  YouTube token file is created 0600 from the first byte. `db.start_run/finish_run` replace the per-runner `runs`
  SQL. SETUP: chat id via curl, `RAIJ_ENV` = `.env` minus the state key.
- **Phase 10b (2026-09-23, audit A4–A8, A11–A16):** Numbers: a script figure must be the card's figure rounded to
  nearest at its own precision (≤ half its last place) **and** within 20 % of it (`facts.supported`) — "2 مليون" for
  1,500,001 and "20 ألف" for 12,000 now fail. Copy gate: besides trigram containment, `similarity.longest_run` ≥
  `script.max_shared_run` (8 consecutive non-number words; numbers extend a run but don't count — real script #27
  shares "في 28 ديسمبر 2025 عن عمر ناهز 91 عاما" with its source and must) → rewrite/reject, run text goes in the
  rewrite note and `notes.shared_run`. **Caps** (`pipeline.max_age_days` 2, `pipeline.max_attempts` 3):
  `db.expire_stale()` runs at the start of extract/script/voice/assemble — selected/extracted candidates older than
  2 days → `expired`, passed scripts never voiced → `expired`, voiced videos never rendered → `failed`; retryable
  failures count in `candidates.attempts` (extract → `extract_failed`, script → `script_rejected`),
  `scripts.notes.attempts` (voice → a `failed` videos row) and `videos.notes.attempts` (assemble → `failed`); extract
  and script now process the newest picks first. Stock: every search erroring raises `broll.BrollUnavailable`
  (retried next run) instead of failing the video; an empty answer is still `BrollError`. Publish backoff:
  `posts.last_attempt_at` + `publish.retry_after_hours` [1, 6, 24] — a failed post is skipped until its backoff
  passed (`runner.retry_due`), so 3 attempts span ~31 h instead of 30 min. edge-tts: 4 attempts with 2/4/8 s backoff
  (owner's call: no fallback voice). Gemma models aren't sent `responseMimeType` (400). YouTube upload: empty file
  refused, >3 consecutive 308s without progress → `PublishError`. Hook title: shrinks to 56 px, then keeps two lines
  + "…" (never a third over the subtitles); `clean_title` needs an Arabic letter and rejects a title equal to the
  spoken hook. Subtitles cope with a voice of zero words. Rank day = Cairo day (see Decisions).

- **Phase 13 + 15 (2026-09-23, owner's ask: "get back to my device", more videos, trigger production from the bot,
  shorts *and* long videos up to 5 min, pick topics + where to post, give a topic or a script):**
  - **Formats** (`src/formats.py`, config `formats:`): `short` = the classic 1080×1920 ≤60 s; `long` = **1920×1080**,
    240–520 words at `+0%` (≈2.2–4.7 min), `max_seconds` 300, cut every 8 s. Landscape on purpose: YouTube files any
    vertical video ≤3 min as a Short. `scripts.kind` carries it; voice/assemble/publish read the format from the
    script (legacy `script.*/voice.*/video.*` keys still define `short`).
  - **`candidates.wanted`** (JSON: formats, platforms, kind trend|topic|script, text, by owner|auto) = the ask.
    `formats.wanted_platforms()` → `publish.wanted_platforms()` (long → `publish.long_platforms`, YouTube + TikTok
    export; Reels APIs cap at 90 s). Owner picks (`by: owner`) don't count against `ranking.top_n` (8 now);
    `ranking.long_top_n` (1) of the automatic picks also gets a long version (`rank.pick_long`: evergreen, fit,
    explainer/list/story).
  - **Long script** (`script_write_long.txt`): hook → 3–6 sections, each opening on a beat with `chapter` (2–5
    words) → payoff → cta; `validate(kind="long")` needs ≥2 chapter beats. Extract reads more for long
    (`extract.long_articles` 6, plus a news search) and asks the card for 8–14 facts (`{{depth}}`).
  - **Assemble**: `subtitles.Style.for_frame`, `brand.endcard/hook_sequence(frame=)`, `portrait.compose(frame=)`,
    `render.Plan(width, height, max_seconds, overlays)`; **chapter cards** (`brand.chapter_sequence`, yellow pill
    top-centre 3 s at each chapter; `videos.notes.chapters` = [{at, title}], "المقدمة" at 0:00); **thumbnail**
    (`assemble/thumbnail.py`: frames from the *clean stock clips* — the finished render carries subtitles/logo and
    mid-xfade blends — scored on brightness/contrast/edges, title + series + logo, 1280×720 JPEG ≤2 MB, only for
    long); b-roll searched in the frame's orientation, **Pexels photos as the fallback** when no faceless clip
    matches (`video.photo_fallback`, Ken Burns via the existing still path, provider `pexels-photo`, .jpg in
    assets/stock); faces: landscape uses `faces.MIN_AREA_WIDE` 0.5 % (1.5 % let crowd shots with clear faces through
    on the first live long render).
  - **Publish**: long → regular upload (`youtu.be/<id>`), description = caption + chapter timestamps
    (`common.chapter_lines`: first 00:00, ≥3, ≥10 s apart), no "#Shorts"; `youtube.set_thumbnail` (50 units, best
    effort; the channel may need phone verification once). Cards show `🎬 #id · 4:32 · series · 🎬 Long`.
  - **Bot production** (`review/picks.py`, `src/jobs.py`): `/trending` queues the `trending` job (discover + screen
    with `rank(select=False)`, then a numbered list with ☑ buttons `pk:n`, ➡️ Next → format `pf:i` + platform `pp:i`
    toggles → 🚀 Make `pg` / ✖ `px` / ⬅️ `pb`; one flow at a time in `control.pick_flow`, 12 h TTL, every tap edits
    the same message). `/topic <text>` and `/script <text>` go straight to the options step (script: format from the
    word count, ≤115 → short, ≤520 → long). 🚀 → `picks.commit` (trending items → `selected` + wanted; topic/script →
    `discover/manual.py` candidate, source `manual`) and queues `produce`. `/run` queues `run-daily`; `/jobs` shows the
    running/queued job + the tail of `data/logs/jobs.log`. Jobs are detached `python -m src.main <cmd>` children
    started by the bot's maintenance pass (`poll(maintenance=)`, also hourly reminders); `run-daily`, `produce` and
    `trending` share the `pipeline` file lock; on Actions `tick` runs the queue inline. `produce` loops up to 3 rounds
    while new picks appear.
  - **Manual research** (`discover/manual.py`): Bing News RSS (publisher URLs in `url=`) with the topic reduced to
    content words (`keywords()`), Google News RSS only for headlines (its links are JS interstitials — the first live
    run read nothing and built a card from an off-topic Wikipedia hit about Yemen); Wikipedia extract only when the
    title names the topic (`relevant()`), descriptive UA; distill prompt: "off-topic → usable: false", plus
    `category` for manual items. A re-sent topic/script is a new row (`:2`) unless the first is still in progress.
    Owner scripts: extract builds the card from the text itself (no LLM), `write.segment_script` splits it into beats
    verbatim (`same_text` ≥ 0.9, one retry; an edit note allows rewording), no fact/similarity gate.
  - **Wikipedia source** (`discover/wikipedia.py`, `discovery.wikipedia`): yesterday's top-40 viewed ar.wikipedia
    articles (Wikimedia REST, keyless); extract reads the article extract + news about it.
  - Extract's own client gets a 150 s read timeout (a long-form card timed out at 20 s on a busy free model).
  - Repo survey (2026-09-23, 18 repos — MoneyPrinterTurbo, OpenMontage, purffle-shorts, ai-marketing-factory,
    hadi-hani/arabic-shorts-generator …): taken = landscape + 3-min rule, chapters in the description, frame-scored
    thumbnails, photo fallback with Ken Burns, keyword-reduced search, Wikipedia pageviews as a trend source, jobs
    queue. Not taken (yet): libass karaoke captions (no libass here), CC-BY music pools (needs credits + the owner's
    pick), Gemini TTS as an alternate voice (free tier exists; owner chose no fallback voice), tashkeel for TTS,
    upload-post.com for TikTok (TikTok's direct API stays private-only until audited).

- **Post now (2026-09-24, owner's ask "approved vids never all post"):** the posting windows (4/day × `per_window` 1)
  can't drain ~9 approved/day, and `max_age_hours` 72 then expires the rest; Meta keys are still missing (IG/FB
  never post, videos stay `approved` until finalize). Controls: **`/post_now`** (button bar 🚀 Post now; optional ids
  `/post_now 31 32`) lists approved videos with a connected platform still to post, confirm `nw:<max id>` →
  `runner.rush()` writes `videos.notes.post_now = {at, by}` and queues a `publish` job (`jobs.ALLOWED` now includes
  `publish`; `main.JOBS` too). A rushed video ignores the window gate and **doesn't use the window's slot**
  (`windows.used()` excludes `post_now` videos via `json_extract`). CLI: `publish --now` rushes every eligible video.
  `publish.windows.per_window` (default 1) raises the scheduled throughput. `/queue` ends with the window state
  (`cards.window_text`) and the /post_now hint. **Bug fixed:** `jobs.alive()` used `kill(pid, 0)`, which succeeds for
  a zombie child, so a finished `/trending` job (pid 22950, 17:26→) looked "running" for 6.5 h and blocked every
  queued `produce`/`run-daily`; it now asks the Popen (`poll`) and probes `ps` for foreign pids.

- **No limits (owner 2026-09-24 03:00 Cairo, "make posting with no limits"):** `publish.windows.times: []` — approved
  videos post on the next pass (launchd every 30 min, or a `/post_now` job at once). The first 🚀 Post now run put
  10 videos out in 7 min (#26, #30–#37, #39; 20 posts, 0 failures, no quota error). Two things it exposed, both
  fixed: **(1)** YouTube quota — `youtube.quota_error()` (403 "exceeded your quota" / "number of videos") raises
  `QuotaExhausted`; the runner gives the attempt back (post stays `queued`, attempts −1), skips that platform for
  the rest of the pass and sends one notice per platform per day (`control.quota_notice_<platform>`). **(2)** The
  duplicate check adopted the wrong video: long #36 and Short #35 share the hook title, so `existing()` matched
  #35's Short and #36 was never uploaded. `existing(kind=)` now requires the same format (a Short's description
  ends with "#Shorts"), and the runner refuses an external id that already belongs to another video (post →
  `failed`, retried). #36 was reset and re-uploaded. Consider a distinct long title (SEO) later.

- **TikTok app (2026-09-24, owner: "draft on app and me posting"):** platform `tiktok` = `publish/tiktok_api.py`
  (Content Posting API). `publish.tiktok.mode: inbox` uploads the MP4 to the owner's TikTok **inbox**
  (`/v2/post/publish/inbox/video/init/`, FILE_UPLOAD chunks 5–64 MB / last ≤128 MB / `floor(size/chunk)` count, then
  `status/fetch` until `SEND_TO_USER_INBOX`); the inbox call takes no text, so the caption goes to Telegram and the
  owner pastes it in the app → post `exported`. `direct` mode (`video.publish`, after TikTok's audit) queries
  creator-info and **refuses** when the wanted privacy isn't offered (unaudited apps = SELF_ONLY forever) instead of
  posting private; `is_aigc` label on. Auth: the portal rejects non-https redirects, so the default is the **Web** flow in two steps:
  `tiktok-auth` opens consent with `publish.tiktok.redirect_uri` = https://raij.dafatir.workers.dev/tiktok/callback
  (the page shows `tiktok-auth --code … --state …`; state in `data/tiktok.auth.json`, 15 min); `finish_web` exchanges
  it. Desktop loopback `http://127.0.0.1:8471/callback/` + hex-SHA-256 PKCE remains when `redirect_uri` is empty; tokens in
  `data/tiktok.token.json` (0600; access 24 h, refresh 365 d and rotating — file rewritten after each refresh;
  `username` kept for status). TikTok's errors are HTTP 200 + `error.code`: limit codes
  (`rate_limit_exceeded`, `spam_risk_too_many_pending_share`, `spam_risk_too_many_posts`, banned) → `QuotaExhausted`
  (attempt given back), auth codes → "run tiktok-auth". Wiring: `tiktok_export` in a wanted list implies `tiktok`;
  once the app is connected `wanted_platforms()` drops the export and `tiktok.missing()` steps aside; brand platforms
  and `long_platforms` list both. Public pages for the developer portal: `deploy/site/` worker →
  https://raij.dafatir.workers.dev/{privacy,terms}. **Owner's part:** developer app (Desktop platform, Login Kit +
  Content Posting API, sandbox target user = own account) → paste `TIKTOK_CLIENT_KEY`/`SECRET` → `tiktok-auth`.

- **Phase 18 — professional فصحى (2026-09-24, owner: "the KSA Arabic accent and reading is very bad … get a
  professional Arabic standard script and voice for all future products"; replaces D3's "light Gulf touch"):**
  *Diagnosis:* video #39's script (#45) was pure Egyptian dialect ("ليه الدولار مولع اليومين دول … ما هداش") and
  #41 opened with Gulf "زين" — the strong Gemini models were 429 (quota) when they were written, so flash-lite/Gemma
  wrote them; free quotas reset 07:00 UTC = 10:00 Cairo, i.e. *after* the 07:00 daily run. Scripts didn't record
  their model. *Fixes:* (1) `schedule.run_daily_at` **10:30** Cairo; `llm.last_model()` → `scripts.notes.model`.
  (2) Prompts: professional MSA, pan-Arab news register, **no dialect anywhere**, hamza/ة/ى orthography; brand
  `tone` updated. (3) **Dialect gate** `fusha.dialect_words()` (Egyptian/Gulf/Levantine markers + the ش-negation,
  clitics stripped; `_SAFE` words that also read as MSA never trigger) inside `write.draft()` → `DraftError` naming
  the words → one rewrite. (4) **Editor pass** `polish.polish()` after the copy gate (`script.polish: true`, one
  LLM call, prompt `script_polish`): each beat rewritten in professional فصحى **and fully vocalized** for the TTS;
  accepted only if the beat count matches, digits unchanged and card-supported, no dialect, ≤ +30 % words; the
  vocalized text must be the final text plus marks only (`fusha.same_letters`) → `beats[i].tts`; polished body
  re-checked against the copy gate. LLM failure leaves the draft (`notes.polish.error`). Live on #45 (flash-lite,
  strong models exhausted): all 5 beats converted, hamzas fixed, full tashkeel, dialect gone. (5) Voice:
  `tts.speech_text()` speaks `tts` when present; `voice_script` strips diacritics from the returned words so
  subtitles/SRT stay plain (`notes.tashkeel` = vocalized beats). (6) **Voice bench** `tools/voice_bench.py
  --script N --voices … [--tashkeel] [--send]`: edge-tts → Gemini transcription → word accuracy, clips to Telegram.
  Script 43 at +10 %: Taim (JO) 0.945 / 52 s, Shakir (EG) 0.944 / 47 s, Hamed (SA) 0.936 / 55 s, Laith (SY)
  0.907 / 44 s; polished #45: Taim plain 0.913 → tashkeel 0.93, Hamed 0.93 both. Default → **ar-JO-TaimNeural**
  (alt Shakir); the owner picks by ear from the clips sent. Owner-written scripts (`segment_script`) are not
  polished (their text is verbatim). Videos #40–#43 (in review) predate the change.

- **Performance audit (2026-09-24, owner: "audit pipeline performance and fix any issues"):** *Findings* from
  `runs` + logs + `pmset -g log`: the 10:30 daily run lasted 4 h 16 min — the lid was closed at 10:33 (clamshell
  sleep on battery) until 14:21; Power Nap DarkWakes (45 s every ~16 min, no network) let it crawl: extract lost
  5/8 picks to ConnectError (and burnt an attempt each), script wrote 0/5, edge-tts DNS-failed twice and returned a
  **cut-off stream** for script 58 (22 of 99 words, 12 s) which was rendered as #46 and sent for review; the 07:00
  `script` run (#87) stayed `running` for ever (killed by `install-services`' bootout); nothing retried leftovers
  before the next day's run. Render cost on the M3: ~4–5 min per Short, of which encoding is small (bench: 30 s of
  1080×1920 in 5 s with libx264 medium; `h264_videotoolbox` no faster, veryfast 2.3 s) — the rest is stock
  search/download, face checks, ~100 subtitle PNGs and the 10-input xfade graph; `videos.notes.timing` now
  records broll/graphics/render/thumbnail seconds per video so the next audit has numbers. *Fixes:* (1)
  `src/power.py`: `main()` starts `caffeinate -i -s -w <pid>` for every pipeline command (`main.AWAKE_COMMANDS`,
  never the bot; `schedule.keep_awake`); a closed lid on battery can't be overridden from user space. (2) Outages
  don't count as attempts: `LLMError.outage` (every provider unreachable/429/503/unconfigured), `tts.Unreachable`
  (ClientConnector*/timeouts), `assemble._outage` (BrollUnavailable, httpx transport errors) — the item waits for
  the next run; `pipeline.max_age_days` still bounds it. (3) `tts.check_complete`: voice with < 60 % of the
  script's words → `Truncated`, retried, never rendered. (4) **Same-day catch-up:** the launchd/systemd publish job
  runs `publish --catch-up` → `main.catch_up` → `produce` on `main.leftovers` (selected without story, extracted
  without script, passed without voice, voiced, rendered), at most once per `pipeline.catch_up_hours` (2,
  `control.last_catch_up`); `tick` does the same on Actions. Live: the first pass at 15:11 picked up 5+4+2 items.
  (5) `db.start_run` closes `running` rows of the same command older than 6 h as failed `{"interrupted": 1}`.
  Owner options not done here: `sudo pmset repeat wakeorpoweron MTWRFSU 10:28:00` (wake for the daily run),
  keep the lid open/on AC while it runs, or a hosted fallback.

## Next up

**Runs on the owner's Mac (launchd) since 2026-09-23 17:47 Cairo.** GitHub Actions (repo Wael9912/raij) is the
fallback: workflow dispatch-only, Worker cron paused (`deploy/cloudflare-trigger/wrangler.toml` `crons = []`).
Going hosted again = `gh workflow run raij.yml -f command=tick -f export_state=true` is *not* the direction; instead:
pack the Mac state (`state pack state.enc` → `gh release upload state-bootstrap state.enc --clobber`), uninstall the
Mac services, restore both crons, set `RAIJ_ENABLED=true`.

**Old hosted setup (kept for the fallback):** live on GitHub Actions 2026-09-22 → 2026-09-23 (repo Wael9912/raij, public).
"check" now = `gh run list -R Wael9912/raij --workflow raij.yml` + `gh run view <id> --log`; the live DB is in the
Actions cache — to inspect it: download the newest cache isn't possible via gh, so run with `workflow_dispatch`
and read the log, or use the weekly encrypted backup from Telegram (`state unpack` with RAIJ_STATE_KEY from .env).
**Never run the Mac pipeline/bot while Actions is enabled** (two Telegram pollers, two diverging DBs). To move back:
`gh variable set RAIJ_ENABLED --body false`, restore the latest state on the Mac, `install-services`.
Code changes: commit + `git push` (CI runs the tests; the next tick uses the new code).

**Known (2026-09-23): GitHub runs the `*/10` cron only every ~2.5 h on this repo** (3 runs in 5.5 h), so taps and the
07:00 daily run lag hours. Phase 10a added a Cloudflare Worker cron (`deploy/cloudflare-trigger/`) that dispatches the
workflow every 10 min — **deployed 2026-09-23** as Worker `raij-trigger` (https://raij-trigger.dafatir.workers.dev, secret
`GH_TOKEN` = fine-grained PAT "raij-trigger", Actions read/write, expires 2027-09). Redeploy: `npx wrangler deploy` in that dir;
logs: `npx wrangler tail`. Runs now show as `workflow_dispatch` every 10 min.
`workflow_dispatch` takes a `command` input (default `tick`): `gh workflow run raij.yml -f command=finalize`.

**Full audit + improvement plan: `AUDIT_2026-09-23.md`** (local only, gitignored — read it before starting a phase).
Sections A code, B security, C bot UX, D content strategy (with the owner decisions needed), E phases 10–15.
"start" / "next phase" now means the next unfinished phase in that plan.

| Phase | Scope | State |
|---|---|---|
| 10a | Ticks & state: A1 tick try/finally, A2 orphan row, A3 expire approved, A9 pause once, A10 bootstrap refresh, external cron trigger | ✅ 2026-09-23 (trigger deployed, dispatching every 10 min) |
| 10b | Gates & retries: A4 number tolerance, A5 shared-run check, A6 attempt/age caps, A7 transient Pexels, A8 backoff, A11–A16 | ✅ 2026-09-23 |
| 10c | Security: S1 SHA pins/credential scoping, S2 unpack paths + authenticated bundle, S3 id regex/size cap, S4 owner id/reply check/expiry, S5–S8; `db.start_run/finish_run`; 20 new tests | ✅ 2026-09-23 |
| 11 | Bot UX: /queue, digest, why-picked + Arabic title in caption, parent line, 48/72 h reminders (warn only), retry button, setMyCommands, per-video pending edit, /approve_all + /skip with confirm | ✅ 2026-09-23 |
| 12 | Content I (owner: D1 tech/money/wow-facts/life-hack + tools, D2 Gulf-first, D3 MSA + `ar-SA-HamedNeural`, D4 tools series): niche/region/fit weights, round-robin screen, ad-safe gate, Gulf feeds, posting windows, SEO title/description/tags, playlists, CTA rotation, A/B titles | ✅ 2026-09-23 (playlists need owner re-auth) |
| 13 | Pick-before-render via Telegram (`/trending` → pick → format → platforms → make), `/topic`, `/script`, `/run`, `/jobs`, jobs queue, Wikipedia source | ✅ 2026-09-23 |
| 15 | Long format (landscape 2–5 min, chapters, thumbnail), per-format script/voice/render/publish, automatic 1 long/day | ✅ 2026-09-23 (live: #30 from /topic) |
| 14 | Topic performance memory, traffic-source metrics, `tools`/affiliate series, second brand; real series objects (templates, quotas, evergreen backlog) from the old 13 | ⬜ next |
| 16 | Music: CC-BY/CC0 pool by mood with credits (assets/music is empty — long videos are voice-only); libass-free karaoke polish; weekly compile of the week's Shorts | ⬜ |

Still pending from before:
0. **Owner: `! gh variable set RAIJ_ENABLED --body false -R Wael9912/raij`** (blocked for Claude by the auto-mode
   classifier; the workflow has no cron anymore, so this is belt-and-braces).
1. **Owner: YouTube re-auth for playlists** — `uv run python -m src.main youtube-auth` (new `youtube` scope; the token
   file is local now, no secret to update while on the Mac). Until then Shorts upload fine, playlists log a hint.
2. **Owner: Meta keys** — paste App ID, App Secret, short-lived token → exchange → add to .env. IG/FB start with the
   first videos approved after the keys exist.
3. Watch the first Mac daily run (07:00 Cairo, `data/logs/daily.log`): Gemini quota with top_n 8 + 1 long
   (≈ 3 screen + 9 cards + 10 scripts ≈ 22 calls/day across the model chain), render time on this Mac.
4. Later: YouTube Data API key (discovery), Pixabay key, music pool.

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
