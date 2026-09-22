# SETUP — Ra'ij (draft, Phase 0)

Every key below goes into `.env` (copy from `.env.example`). Nothing costs money; all are free tiers.
Consoles change their menus often. If a button name here doesn't match what you see, look for the closest match.

---

## 0. Local prerequisites

```bash
brew install uv ffmpeg-full       # uv = Python manager, ffmpeg-full = ffmpeg with libass (Arabic subs)
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
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id` into `TELEGRAM_CHAT_ID`.

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

---

## 6. YouTube channel + OAuth for uploads

1. Create the channel you'll post to at https://www.youtube.com/account.
2. In the same Google Cloud project: **APIs & Services → OAuth consent screen**. Choose External and add yourself as a **Test user**.
3. **Credentials → Create credentials → OAuth client ID → Desktop app.** Download the JSON as `client_secret.json` into the repo root (it's gitignored).
4. The first `publish` run opens a browser to authorize and stores a refresh token locally (also gitignored).
   - An upload costs ~1,600 quota units, so 3/day plus discovery fits in 10,000.
   - Apps in "Testing" status get refresh tokens that expire after 7 days. Publish the consent screen (no verification is needed for personal use under 100 users) to avoid re-auth.
   - Unverified API projects may have uploads locked to **private**. If that happens, request an audit via the YouTube API Services form.

---

## 7. FFmpeg + fonts check

```bash
ffmpeg -version | head -1
ffmpeg -hide_banner -filters | grep -E " ass | subtitles "   # libass must be present
```
- If `ass` is missing (the slim Homebrew `ffmpeg` formula has dropped libass), install the full build:
  `brew install ffmpeg-full`. It's keg-only, so point the pipeline at it with
  `FFMPEG_BIN=$(brew --prefix ffmpeg-full)/bin/ffmpeg` in `.env`.
- **Noto Naskh Arabic** gets bundled into `assets/fonts/` in Phase 6 (OFL license). You don't need to install it.

---

## Daily run (Phase 9)

```cron
0 7 * * *  cd ~/Documents/Projects/social-media-automation && uv run python -m src.main run-daily >> data/cron.log 2>&1
```

Kill switch: `/pause` in Telegram, or `uv run python -m src.main pause`.
