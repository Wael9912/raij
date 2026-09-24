# SETUP — Ra'ij (draft, Phase 0)

Every key below goes into `.env` (copy from `.env.example`). Nothing costs money; all are free tiers.
Consoles change their menus often. If a button name here doesn't match what you see, look for the closest match.

---

## 0. Local prerequisites

```bash
brew install uv ffmpeg libraqm    # uv = Python manager; plain ffmpeg is enough (subs are drawn in Python);
                                  # libraqm = HarfBuzz Arabic shaping for Pillow
cd ~/Documents/Projects/social-media-automation
uv sync                           # creates .venv with Python 3.12 + deps
cp .env.example .env
uv run python -m src.main init-db
uv run python -m src.main --help
```

> Python is pinned to 3.12 (`.python-version`) because faster-whisper/ctranslate2 wheels lag behind the newest Python.

---

## 1. Google Cloud: YouTube Data API key + Gemini key

**YouTube Data API v3 (discovery, read-only):**
1. Go to https://console.cloud.google.com, then create a project (e.g. `raij`).
2. Open **APIs & Services → Library**, search **YouTube Data API v3**, and click **Enable**.
3. Open **APIs & Services → Credentials → Create credentials → API key**.
4. Restrict the key: **API restrictions → YouTube Data API v3**.
5. Put it in `.env` as `YOUTUBE_API_KEY`.
   - Quota: 10,000 units/day. A `search.list` call costs 100 units, a `videos.list` call costs 1. The budget is set in `config.yaml → discovery.youtube.daily_quota_budget`.

**Gemini (primary LLM):**
1. Go to https://aistudio.google.com/apikey and click **Create API key** (you can reuse the `raij` project).
2. Put it in `.env` as `GEMINI_API_KEY`.

**Groq (fallback LLM):** https://console.groq.com/keys → create a key → `GROQ_API_KEY`.

**Ollama (last-resort local LLM, optional):** `brew install ollama && ollama pull qwen2.5:7b`.

---

## 2. Reddit app

1. Log in and go to https://www.reddit.com/prefs/apps, then click **create another app**.
2. Set type to **script**. Name it `raij`. Set the redirect URI to `http://localhost:8080`.
3. The string under the app name is `REDDIT_CLIENT_ID`. The **secret** is `REDDIT_CLIENT_SECRET`.
4. Set `REDDIT_USER_AGENT=raij/0.1 by <your_username>`.
   - Reddit's free API tier is limited to non-commercial, low-volume use (~100 req/min with OAuth). Our usage stays well under that.

---

## 3. Pexels + Pixabay (free b-roll)

- **Pexels:** https://www.pexels.com/api/ → sign up → **Your API key** → `PEXELS_API_KEY`.
- **Pixabay:** https://pixabay.com/api/docs/ → log in → the key is shown in the "Parameters" section → `PIXABAY_API_KEY`.
- Both licenses allow commercial use without attribution. We still log every clip's source and ID in `videos.broll_manifest`.

---

## 4. Telegram bot + chat ID

1. In Telegram, message **@BotFather**, send `/newbot`, and pick a name. Copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot.
3. Get your chat id without pasting the token into a browser (browser history and sync services keep URLs):
   `curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" | python3 -c "import json,sys; print(json.load(sys.stdin)['result'][-1]['message']['chat']['id'])"`
   and put it in `TELEGRAM_CHAT_ID`. (Empty result → send the bot another message and rerun.) In a private chat this
   number is also your user id; the bot obeys only messages and taps *from* that user (set `TELEGRAM_OWNER_ID` if
   they ever differ, e.g. a group chat).
4. Send pending videos: `uv run python -m src.main review`
5. Keep the button handler running (a spare terminal; Phase 9 adds a launchd service):
   `uv run python -m src.main bot` — it only obeys `TELEGRAM_CHAT_ID`.
   Buttons: ✅ Approve · ❌ Reject · ✏️ Edit script (reply with a note) · 🔁 New b-roll · 🎙 Re-voice.
   Commands: `/status`, `/pause` (kill switch: no publishing), `/resume`.

---

## 5. Meta: Page → Instagram Business, app, Reels publishing tokens

1. **Instagram account → Professional (Business or Creator).** In the IG app: Settings → Account type and tools → Switch to professional account.
2. **Facebook Page.** Create one if needed, then link IG to it: Page → Settings → Linked accounts → Instagram.
3. **Meta app.** At https://developers.facebook.com/apps, click **Create app** → use case **Other** → type **Business**.
   Add these products: **Instagram Graph API** (via "Instagram API with Facebook Login") and **Facebook Login for Business**.
4. **Permissions** (fine in Development mode for your own accounts): `pages_show_list`, `pages_read_engagement`,
   `pages_manage_posts`, `instagram_basic`, `instagram_content_publish`, `business_management`, `read_insights`, `instagram_manage_insights`.
5. **Token.** In **Graph API Explorer**, pick your app, request the permissions above, and generate a **User token**. Exchange it for a long-lived token:
   `GET /oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN`
   Then `GET /me/accounts` returns a **Page access token** (it never expires when derived from a long-lived user token). Save it as `META_PAGE_ACCESS_TOKEN`, and the page `id` as `META_PAGE_ID`.
6. **IG user ID:** `GET /{page-id}?fields=instagram_business_account` → `META_IG_USER_ID`.
   - IG publishing limit: 50 API-published posts per 24h. We post 3–5.
7. Put the three values in `.env`, then check with `uv run python -m src.main publish --dry-run` (it says `ready`
   or which key is missing per platform).

---

## 6. YouTube channel + OAuth for uploads

1. Create the channel you'll post to at https://www.youtube.com/account.
2. In the same Google Cloud project: **APIs & Services → OAuth consent screen**. Choose External and add yourself as a **Test user**.
3. **Credentials → Create credentials → OAuth client ID → Desktop app.** Download the JSON as `client_secret.json` into the repo root (it's gitignored).
4. Run `uv run python -m src.main youtube-auth` once: it opens Google's consent page, then stores a refresh token in
   `data/youtube.token.json` (gitignored). Scheduled `publish` runs never open a browser; if the token is revoked
   or expires, publish fails with a message telling you to run `youtube-auth` again.
   - An upload costs ~1,600 quota units, so 3/day plus discovery fits in 10,000.
   - Apps in "Testing" status get refresh tokens that expire after 7 days. Publish the consent screen (no verification is needed for personal use under 100 users) to avoid re-auth.
   - Unverified API projects may have uploads locked to **private**. If that happens, request an audit via the YouTube API Services form.
   - **Playlists (Phase 12)** need the `youtube` (manage) scope. A token authorized before 2026-09-23 has only
     `youtube.upload`: uploads still work, playlists are skipped with a log hint. Run `youtube-auth` again, then
     on GitHub Actions refresh the secret: `gh secret set RAIJ_YOUTUBE_TOKEN -R Wael9912/raij < data/youtube.token.json`.

---

## 7. FFmpeg + fonts check

```bash
ffmpeg -version | head -1
ffmpeg -hide_banner -encoders | grep libx264      # H.264 encoder must be present
```
- libass is **not** needed: Arabic subtitles and brand graphics are drawn in Python (Pillow with raqm/HarfBuzz
  shaping — needs `brew install libraqm`; `src/textshape.py` makes Pillow find Homebrew's fribidi) and
  overlaid by ffmpeg, so the slim Homebrew `ffmpeg` works. Check: `uv run pytest -q -k shaping`.
- Channel font Cairo (variable, used at Black) is bundled in `assets/fonts/` (OFL — `OFL-Cairo.txt`).
  Optional: drop your own logo at `assets/brand/logo.png` (transparent PNG); otherwise a "رائج" wordmark is drawn.
- Optional background music: drop CC0 tracks (mp3/m4a/wav) into `assets/music/`; they're ducked under the
  voice automatically. With none, videos are voice-only.

---

## 8. TikTok: developer app → drafts in your inbox (now) → direct posting (after TikTok's audit)

TikTok's Content Posting API has two stages. **Unaudited apps can only create private posts**, and a post made
private stays private, so Ra'ij starts in **inbox mode**: it uploads the finished MP4 into your TikTok *inbox*
(scope `video.upload`); TikTok notifies you in the app, you open it, paste the caption the bot sends to Telegram,
and tap Post. After TikTok audits the app, switch `publish.tiktok.mode` to `direct` and it posts public by itself.

1. **Public pages (already deployed):** https://raij.dafatir.workers.dev/privacy and …/terms
   (`deploy/site/`, `npx wrangler deploy`). The portal asks for both URLs.
2. **Developer account + app:** log in at https://developers.tiktok.com with the TikTok account you post from
   (رائج) → *Manage apps* → *Connect an app*. Name "Ra'ij", category e.g. *Entertainment*, description
   "Personal tool: uploads the owner's own Arabic explainer videos to the owner's account", icon = the channel logo,
   Terms of Service URL and Privacy Policy URL from step 1.
3. **Platform:** add **Web** (the portal only accepts https redirects) with redirect URI exactly
   `https://raij.dafatir.workers.dev/tiktok/callback` (`publish.tiktok.redirect_uri`; the domain is the verified URL
   property from step 1). That page just shows the code TikTok sends back and the command to finish.
   (Desktop with `http://127.0.0.1:8471/callback/` is still supported when `redirect_uri` is empty.)
4. **Products:** add **Login Kit** and **Content Posting API**. Scopes: `user.info.basic`, `video.upload`; add
   `video.publish` too if you want it covered by the review from the start (direct mode needs it later).
   For Content Posting API answer *FILE_UPLOAD* (no URL property / domain verification needed for uploads from disk).
5. **Sandbox (works before any review):** in the app, create a *Sandbox*, add your own TikTok account as a
   **target user** (accept the invitation in the TikTok app), and copy the sandbox **Client key** and **Client secret**.
   Paste both into the chat (or write them yourself):
   ```
   TIKTOK_CLIENT_KEY=…
   TIKTOK_CLIENT_SECRET=…
   ```
   Then `uv run python -m src.main tiktok-auth`: the browser opens TikTok's consent page; after you approve, our
   callback page shows a `tiktok-auth --code … --state …` command — run it within 15 min and the tokens land in
   `data/tiktok.token.json` (gitignored, 0600); the log says `TikTok authorized as @…`. From the next publish
   pass every approved video goes to your TikTok inbox; the old Telegram copy (`tiktok_export`) steps aside by itself.
6. **Production keys:** *Submit for review* in the portal (basic app review: description + a short screen recording of
   the login → upload → post flow). When approved, replace the two `.env` values with the production key/secret and
   run `tiktok-auth` again. Inbox mode keeps working the same way.
7. **Direct posting (later):** apply for the Content Posting API audit (demo video showing the privacy picker fed by
   creator-info, the AI-content label, no hard-coded "public"). When TikTok lifts the private-only restriction:
   `publish.tiktok.mode: direct`, `scopes` + `video.publish`, `tiktok-auth` again. Before that, direct mode refuses to
   post (it would be private forever) and tells you so.

Limits worth knowing: uploads ≤ 10 min and ≤ 4 GB; 6 upload starts/min; TikTok caps *unposted* inbox drafts
(`spam_risk_too_many_pending_share`) — post or delete the drafts and the queue continues; those limits don't count
as failed attempts. Revoking the app in TikTok → Settings → Security → *Apps and websites* disconnects it.

---

## GitHub Actions — the hosted fallback (paused 2026-09-23; the Mac runs the pipeline now)

Paused because runs were slow to start and the bot couldn't trigger production there. The workflow is
dispatch-only (no cron) and the Cloudflare Worker's cron is `[]`. To go hosted again: pack the Mac's state
(`uv run python -m src.main state pack state.enc` → `gh release upload state-bootstrap state.enc --clobber`),
`uninstall-services` on the Mac, restore `crons` in the workflow and the Worker, set `RAIJ_ENABLED=true`.
The rest of this section describes that setup.

No server and no card: `.github/workflows/raij.yml` runs every ~10 min on GitHub's machines (public repo =
free, unlimited minutes). Each run restores the encrypted state (DB + media still needed) from the Actions
cache, runs `tick` — the daily pipeline once a day after 07:00 Cairo, every queued Telegram tap, publishing —
and saves the state again if anything changed. Taps are handled within ~10–15 min (GitHub's schedule drifts).

- Secrets (repo → Settings → Secrets and variables → Actions): `RAIJ_ENV` (the .env contents), `RAIJ_STATE_KEY`
  (random, also in the Mac's .env — needed to open backups), `RAIJ_YOUTUBE_TOKEN` (data/youtube.token.json),
  `RAIJ_CLIENT_SECRET` (client_secret.json). Variable `RAIJ_ENABLED=true` is the master switch.
- Adding keys later (e.g. Meta): update `RAIJ_ENV` **without** the state key (it is its own secret and must not
  sit in the `.env` the pipeline reads): `grep -vE '^RAIJ_STATE_KEY=' .env | gh secret set RAIJ_ENV`.
- Actions are pinned to commit SHAs in the workflows; when bumping a version, replace the SHA and its comment
  together (`gh api repos/<owner>/<repo>/git/ref/tags/<tag>`).
- Logs: the repo's **Actions** tab. TikTok copies arrive in Telegram. Weekly: the report + an encrypted DB backup.
- Only one run at a time (`concurrency`); a run whose state restore failed never saves (an empty DB would re-post
  everything). A keep-alive commit every ~45 days stops GitHub pausing the schedule.
- **Trigger:** GitHub's own `*/10` schedule fires only every 2–3 h on a small repo, so a free **Cloudflare Worker**
  (`deploy/cloudflare-trigger/`) dispatches the workflow every 10 min. One-time setup from that directory:
  `npx wrangler login` → `npx wrangler deploy` → `npx wrangler secret put GH_TOKEN` and paste a fine-grained GitHub
  token (github.com → Settings → Developer settings → Fine-grained tokens: only the raij repo, permission
  **Actions: Read and write**, expiry 1 year — put the renewal date in your calendar). Check with
  `gh run list --workflow raij.yml`: runs ~10 min apart, event `workflow_dispatch`.
- **Run a command on the live state:** Actions → raij → *Run workflow* → `command` (e.g. `finalize`,
  `publish --dry-run`); or `gh workflow run raij.yml -f command=finalize`. The state is saved if it changed.
- A failed or killed tick still saves its state (nothing is replayed the next tick), and every saved state older
  than a week is also uploaded to the `state-bootstrap` release — the fallback if the cache is ever evicted.
- Terms: GitHub intends Actions for software projects; if it ever disables the workflow, fall back to the Mac
  (`install-services` below) — restore the latest state first (`state unpack` on the weekly backup).

## Running on the Mac (current setup)

```bash
uv run python -m src.main install-services     # bot + daily pipeline + publish job, as macOS launchd agents
uv run python -m src.main services             # status of the three jobs
uv run python -m src.main uninstall-services   # stop and remove them
```

- `com.raij.bot` — Telegram review bot, always on, restarted automatically if it crashes.
- `com.raij.daily` — `run-daily` at `schedule.run_daily_at` (07:00 local): discover → rank → extract → script →
  voice → assemble → review (cards to Telegram) → publish → report. Problems → a Telegram notice.
- `com.raij.publish` — `publish` every 30 min (`publish.every_minutes`), so an approval goes live within ~30 min.
- Logs: `data/logs/{bot,daily,publish}.log`. Services start at login; after changing code, re-run
  `install-services` (or `launchctl kickstart -k gui/$(id -u)/com.raij.bot`) so the bot reloads.
- The Mac must be **on and awake** for jobs to run. Asleep at 07:00 → the daily run starts on wake; Telegram
  taps made meanwhile are handled when it wakes. To keep it awake while plugged in: System Settings → Displays
  → Advanced → "Prevent automatic sleeping when the display is off" (or Battery → Options on laptops).
- Cron alternative (if you don't want launchd):
  `0 7 * * *  cd ~/Documents/Projects/social-media-automation && /opt/homebrew/bin/uv run python -m src.main run-daily >> data/logs/daily.log 2>&1`

Telegram commands: `/trending` (pick topics → 📱 Short / 🎬 Long → platforms → 🚀 Make), `/topic <text>` (research
and make a video about anything), `/script <text>` (voice your own script as written; ≤115 words → Short, else Long
up to ~520 words), `/run` (the whole daily pipeline now), `/jobs` (what's producing + log tail), `/queue`, `/status`,
`/pause` (kill switch — nothing publishes), `/resume`, `/report` (weekly report now), `/help`.
Long videos (2–5 min, landscape, chapters, thumbnail) go to YouTube as regular videos and to the TikTok export
folder; Reels APIs cap at 90 s. Logs for bot-started jobs: `data/logs/jobs.log`.
