"""TikTok through the Content Posting API (owner 2026-09-24: "draft on app and me posting").

Two modes, `publish.tiktok.mode`:
- **inbox** (now; works before TikTok audits the app): the MP4 is uploaded to the owner's TikTok *inbox* with scope
  `video.upload`. TikTok notifies the owner in the app; they open it, paste the caption (the inbox call takes no
  text, so the caption is sent to Telegram) and tap Post. The post is recorded as 'exported'.
- **direct** (after TikTok's audit): `video.publish` posts straight to the profile with the caption and the privacy
  level the creator-info query allows. Before the audit TikTok forces SELF_ONLY — private forever — so direct mode
  refuses to post when the wanted level isn't offered instead of publishing a private video.

Auth: the portal only accepts **https** redirect URIs (Web platform), so the default flow is two steps —
`tiktok-auth` opens TikTok's consent page with `publish.tiktok.redirect_uri` (our site's /tiktok/callback, which shows
the code) and `tiktok-auth --code … --state …` exchanges it; the state is kept in data/tiktok.auth.json for 15 min.
With `redirect_uri` empty the Desktop flow is used instead: loopback redirect `http://127.0.0.1:<publish.tiktok.redirect_port>/callback/` (must be
registered as-is in the app) with PKCE — TikTok wants `code_challenge` as the **hex** SHA-256 of the verifier, not
base64url. Access tokens last 24 h, refresh tokens 365 d and rotate, so `data/tiktok.token.json` (0600) is rewritten
after every refresh. Client key/secret live in `.env` (`TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`); a sandbox app
has its own pair. TikTok answers HTTP 200 with `error.code != "ok"` on most failures.
Upload rules (media transfer guide): chunks 5–64 MB, the last one may be bigger (≤128 MB), files under 5 MB go
whole, `total_chunk_count = floor(size / chunk_size)`, chunks in order; 206 for a middle chunk, 201 for the last.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from src.config import Config
from src.discover.common import FetchError, request
from src.publish.common import Posted, PostText, PublishError, QuotaExhausted

log = logging.getLogger("raij.publish")

AUTH_URI = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URI = "https://open.tiktokapis.com/v2/oauth/token/"
API = "https://open.tiktokapis.com/v2"
INBOX_INIT = f"{API}/post/publish/inbox/video/init/"
DIRECT_INIT = f"{API}/post/publish/video/init/"
STATUS_URI = f"{API}/post/publish/status/fetch/"
CREATOR_URI = f"{API}/post/publish/creator_info/query/"
USER_URI = f"{API}/user/info/?fields=open_id,display_name,username"
DEFAULT_SCOPES = ("user.info.basic", "video.upload")           # + video.publish once the app passed the audit
MIB = 1024 * 1024
MIN_CHUNK, MAX_CHUNK, MAX_LAST = 5 * MIB, 64 * MIB, 128 * MIB
MAX_FILE = 4 * 1024 * MIB
INBOX_MAX_SECONDS = 600
DONE_STATES = ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE")
# error.code / fail_reason values that are TikTok's limits, not this video's fault (attempt given back).
LIMIT_CODES = ("rate_limit_exceeded", "spam_risk_too_many_pending_share", "spam_risk_too_many_posts",
               "spam_risk_user_banned_from_posting")
AUTH_CODES = ("access_token_invalid", "scope_not_authorized", "token_not_authorized_for_specified_publish_id",
              "auth_removed")
SLEEP: Callable[[float], None] = time.sleep


# --- configuration -------------------------------------------------------------

def token_file(cfg: Config) -> Path:
    return cfg.root / "data" / "tiktok.token.json"


def keys(cfg: Config) -> tuple[str, str]:
    return cfg.secret("TIKTOK_CLIENT_KEY") or "", cfg.secret("TIKTOK_CLIENT_SECRET") or ""


def missing(cfg: Config) -> str | None:
    key, secret = keys(cfg)
    if not key or not secret:
        return "TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET not set (SETUP.md §8)"
    if not token_file(cfg).exists():
        return "not authorized yet — run `uv run python -m src.main tiktok-auth`"
    return None


def connected(cfg: Config) -> bool:
    return missing(cfg) is None


def account(cfg: Config) -> str | None:
    """'@username' from the token file, for status lines."""
    try:
        data = json.loads(token_file(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = data.get("username") or data.get("display_name")
    return f"@{name}" if name else None


def mode(cfg: Config) -> str:
    return "direct" if str(cfg.get("publish.tiktok.mode", "inbox")).lower() == "direct" else "inbox"


# --- tokens ---------------------------------------------------------------------

def _save_token(cfg: Config, tok: dict[str, Any], extra: dict[str, Any] | None = None) -> Path:
    now = time.time()
    path = token_file(cfg)
    old: dict[str, Any] = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
    data = {**old, **(extra or {}),
            "access_token": tok["access_token"],
            "expires_at": now + float(tok["expires_in"] if tok.get("expires_in") is not None else 86400),
            "refresh_token": tok.get("refresh_token") or old.get("refresh_token"),
            "refresh_expires_at": now + float(tok.get("refresh_expires_in") or 31536000)
            if tok.get("refresh_token") else old.get("refresh_expires_at"),
            "open_id": tok.get("open_id") or old.get("open_id"), "scope": tok.get("scope") or old.get("scope")}
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)          # private from the first byte
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    os.chmod(path, 0o600)
    return path


def _token_call(cfg: Config, client: httpx.Client, data: dict[str, str]) -> dict[str, Any]:
    key, secret = keys(cfg)
    try:
        resp = request(client, "POST", TOKEN_URI, retries=1, data={"client_key": key, "client_secret": secret, **data},
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    except FetchError as exc:
        raise PublishError(f"TikTok token request failed: {exc}") from None
    body = resp.json()
    if body.get("error") and body.get("error") != "ok" or not body.get("access_token"):
        err = body.get("error") if isinstance(body.get("error"), str) else (body.get("error") or {}).get("code")
        raise PublishError(f"TikTok authorization expired or was revoked ({err or 'no access token'}: "
                           f"{str(body.get('error_description') or '')[:120]}) — run "
                           "`uv run python -m src.main tiktok-auth` again")
    return body


def access_token(cfg: Config, client: httpx.Client) -> str:
    """A valid access token, refreshed (and the rotated refresh token saved) when it has <5 min left."""
    data = json.loads(token_file(cfg).read_text(encoding="utf-8"))
    if data.get("access_token") and float(data.get("expires_at") or 0) - time.time() > 300:
        return str(data["access_token"])
    if not data.get("refresh_token"):
        raise PublishError("TikTok token file has no refresh token — run `uv run python -m src.main tiktok-auth`")
    tok = _token_call(cfg, client, {"grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
    _save_token(cfg, tok)
    return str(tok["access_token"])


# --- one-time authorization -------------------------------------------------------

PENDING_TTL = 900


def web_redirect(cfg: Config) -> str | None:
    uri = str(cfg.get("publish.tiktok.redirect_uri") or "").strip()
    return uri if uri.startswith("https://") else None


def pending_file(cfg: Config) -> Path:
    return cfg.root / "data" / "tiktok.auth.json"


def start_web(cfg: Config, open_browser: Callable[[str], object] = webbrowser.open) -> str:
    """Web platform, step 1: open TikTok's consent page; the state is remembered for `finish_web`."""
    key, secret = keys(cfg)
    if not key or not secret:
        raise PublishError("TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET are not set (SETUP.md §8)")
    redirect = web_redirect(cfg)
    if not redirect:
        raise PublishError("publish.tiktok.redirect_uri is not an https URL")
    state = secrets.token_urlsafe(16)
    url = AUTH_URI + "?" + urlencode({"client_key": key, "response_type": "code", "scope": ",".join(scopes(cfg)),
                                      "redirect_uri": redirect, "state": state})
    path = pending_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"state": state, "redirect": redirect, "at": time.time()}, f)
    print(f"Open this URL to connect TikTok (it should open by itself); the page you land on shows the command "
          f"to finish:\n{url}\n", flush=True)
    open_browser(url)
    return url


def finish_web(cfg: Config, client: httpx.Client, code: str, state: str | None = None) -> tuple[Path, str | None]:
    """Web platform, step 2: exchange the code the callback page showed (`code` may be the whole callback URL)."""
    code = (code or "").strip()
    if code.startswith("http"):
        q = {k: v[0] for k, v in parse_qs(urlsplit(code).query).items()}
        code, state = q.get("code", ""), q.get("state", state)
    if not code:
        raise PublishError("no code given")
    path = pending_file(cfg)
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PublishError("no authorization in progress — run `uv run python -m src.main tiktok-auth` first") from None
    if time.time() - float(pending.get("at") or 0) > PENDING_TTL:
        path.unlink(missing_ok=True)
        raise PublishError("the authorization started more than 15 min ago — run `tiktok-auth` again")
    if (state or "") != pending.get("state"):
        raise PublishError("state mismatch — use the command shown on the callback page, or run `tiktok-auth` again")
    tok = _token_call(cfg, client, {"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": str(pending.get("redirect") or web_redirect(cfg) or "")})
    who = _whoami(client, tok["access_token"])
    saved = _save_token(cfg, tok, who)
    path.unlink(missing_ok=True)
    return saved, (f"@{who['username']}" if who.get("username") else None)


def redirect_uri(cfg: Config, port: int | None = None) -> str:
    p = port if port is not None else int(cfg.get("publish.tiktok.redirect_port", 8471) or 8471)
    return f"http://127.0.0.1:{p}/callback/"


def scopes(cfg: Config) -> list[str]:
    want = [str(s) for s in (cfg.get("publish.tiktok.scopes") or DEFAULT_SCOPES)]
    if mode(cfg) == "direct" and "video.publish" not in want:
        want.append("video.publish")
    return want


def authorize(cfg: Config, client: httpx.Client, open_browser: Callable[[str], object] = webbrowser.open,
              timeout: float = 300) -> tuple[Path, str | None]:
    """Browser consent (Desktop Login Kit, PKCE) → tokens saved to data/tiktok.token.json. Returns (path, @user)."""
    key, secret = keys(cfg)
    if not key or not secret:
        raise PublishError("TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET are not set (SETUP.md §8)")
    verifier = secrets.token_urlsafe(48)[:96]
    challenge = hashlib.sha256(verifier.encode()).hexdigest()                    # hex, per TikTok's desktop guide
    state = secrets.token_urlsafe(16)
    got: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            if "code" in q or "error" in q:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Ra'ij: TikTok authorized — you can close this tab.</h2>".encode())

        def log_message(self, *args):
            pass

    port = int(cfg.get("publish.tiktok.redirect_port", 8471) or 8471)
    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    redirect = redirect_uri(cfg, server.server_port)
    url = AUTH_URI + "?" + urlencode({
        "client_key": key, "response_type": "code", "scope": ",".join(scopes(cfg)), "redirect_uri": redirect,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Open this URL to connect TikTok (it should open by itself):\n{url}\n", flush=True)
    open_browser(url)
    stop = threading.Event()
    timer = threading.Timer(timeout, stop.set)
    timer.start()
    try:
        while not got and not stop.is_set():
            server.handle_request()
    finally:
        timer.cancel()
        server.server_close()
    if not got:
        raise PublishError("authorization timed out")
    if got.get("error") or got.get("state") != state:
        raise PublishError(f"authorization refused: {got.get('error_description') or got.get('error') or 'state mismatch'}")
    tok = _token_call(cfg, client, {"grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
                                    "code_verifier": verifier})
    who = _whoami(client, tok["access_token"])
    path = _save_token(cfg, tok, who)
    return path, (f"@{who['username']}" if who.get("username") else None)


def _whoami(client: httpx.Client, token: str) -> dict[str, Any]:
    try:
        body = request(client, "GET", USER_URI, retries=0, headers={"Authorization": f"Bearer {token}"}).json()
        user = (body.get("data") or {}).get("user") or {}
        return {k: user[k] for k in ("username", "display_name", "open_id") if user.get(k)}
    except (FetchError, ValueError, AttributeError) as exc:
        log.info("TikTok user info not read (%s)", exc)
        return {}


# --- upload -----------------------------------------------------------------------

def chunk_plan(size: int, chunk_mb: float = 10) -> tuple[int, int, list[tuple[int, int]]]:
    """(chunk_size, total_chunk_count, [(first_byte, last_byte), …]) following TikTok's rules."""
    if size <= 0:
        raise PublishError("empty video file")
    if size > MAX_FILE:
        raise PublishError(f"video is {size / MIB:.0f} MB — TikTok takes at most 4 GB")
    if size < MIN_CHUNK:
        return size, 1, [(0, size - 1)]
    chunk = min(max(int(float(chunk_mb) * MIB), MIN_CHUNK), MAX_CHUNK, size)      # never bigger than the file
    count = size // chunk
    while count > 1000:                                       # never: 1000 × 64 MB > 4 GB, kept for the invariant
        chunk = min(chunk * 2, MAX_CHUNK)
        count = size // chunk
    ranges = [(i * chunk, (i + 1) * chunk - 1 if i < count - 1 else size - 1) for i in range(count)]
    if ranges[-1][1] - ranges[-1][0] + 1 > MAX_LAST:
        raise PublishError("last chunk would exceed 128 MB — raise publish.tiktok.chunk_mb")
    return chunk, count, ranges


def _api(client: httpx.Client, url: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST JSON, unwrap TikTok's envelope: HTTP 200 + error.code is how failures come back."""
    try:
        resp = request(client, "POST", url, json=body,
                       headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
    except FetchError as exc:
        # Some errors come as HTTP 400 with the same envelope (live 2026-09-24: the pending-drafts cap
        # `spam_risk_too_many_pending_share` was a 400) — classify them the same way as the 200-envelope ones.
        text = str(exc)
        hit = next((c for c in LIMIT_CODES if c in text), None)
        if hit:
            raise QuotaExhausted(f"TikTok limit: {hit} — post or delete the drafts waiting in your TikTok inbox; "
                                 "the queue continues by itself") from None
        if any(c in text for c in AUTH_CODES) or "HTTP 401" in text or "HTTP 403" in text:
            raise PublishError(f"TikTok refused the token ({text[:160]}) — run `uv run python -m src.main tiktok-auth`") from None
        raise
    try:
        payload = resp.json()
    except ValueError:
        raise PublishError(f"TikTok answered non-JSON ({resp.status_code})") from None
    err = payload.get("error") or {}
    code = str(err.get("code") or "ok")
    if code != "ok":
        msg = str(err.get("message") or "")[:200]
        if code in LIMIT_CODES:
            raise QuotaExhausted(f"TikTok limit: {code} — {msg or 'try again later'}")
        if code in AUTH_CODES:
            raise PublishError(f"TikTok authorization problem ({code}: {msg}) — run `uv run python -m src.main tiktok-auth`")
        raise PublishError(f"TikTok {code}: {msg}")
    return payload.get("data") or {}


def _upload(client: httpx.Client, upload_url: str, video: Path, size: int, ranges: list[tuple[int, int]]) -> None:
    with video.open("rb") as f:
        for start, end in ranges:
            f.seek(start)
            blob = f.read(end - start + 1)
            try:
                resp = request(client, "PUT", upload_url, content=blob, timeout=300, retries=1,
                               headers={"Content-Type": "video/mp4", "Content-Length": str(len(blob)),
                                        "Content-Range": f"bytes {start}-{end}/{size}"})
            except FetchError as exc:
                raise PublishError(f"TikTok chunk {start}-{end} failed: {exc}") from None
            if resp.status_code not in (200, 201, 206):
                raise PublishError(f"TikTok chunk {start}-{end}: HTTP {resp.status_code}")


def _wait(cfg: Config, client: httpx.Client, token: str, publish_id: str) -> dict[str, Any]:
    every = float(cfg.get("publish.poll_seconds", 5))
    deadline = time.time() + float(cfg.get("publish.poll_timeout_seconds", 600))
    while True:
        data = _api(client, STATUS_URI, token, {"publish_id": publish_id})
        status = str(data.get("status") or "")
        if status in DONE_STATES:
            return data
        if status == "FAILED":
            reason = str(data.get("fail_reason") or "unknown")
            if reason in LIMIT_CODES:
                raise QuotaExhausted(f"TikTok limit: {reason}")
            raise PublishError(f"TikTok rejected the video: {reason}")
        if time.time() >= deadline:
            raise PublishError(f"TikTok still {status or 'processing'} after {int(cfg.get('publish.poll_timeout_seconds', 600))} s")
        SLEEP(every)


def _caption(text: PostText) -> str:
    return text.caption[:2200]


def _send_caption(cfg: Config, client: httpx.Client, text: PostText, video_id: int) -> None:
    """The inbox API takes no text: the owner pastes the caption in the app. Best effort — the video is in already."""
    from src.review.runner import make_bot
    from src.review.telegram import TelegramError
    try:
        bot, chat = make_bot(cfg, client)
        bot.send_message(chat, f"📲 TikTok inbox — #{video_id}: open the TikTok notification, paste this caption, "
                               f"post:\n\n{_caption(text)}", disable_web_page_preview=True)
    except TelegramError as exc:
        log.warning("TikTok caption for #%d not sent to Telegram: %s", video_id, exc)


def _creator(cfg: Config, client: httpx.Client, token: str) -> dict[str, Any]:
    return _api(client, CREATOR_URI, token, {})


def publish(cfg: Config, client: httpx.Client, video: Path, text: PostText, video_id: int) -> Posted:
    token = access_token(cfg, client)
    size = video.stat().st_size
    if text.duration_s and float(text.duration_s) > INBOX_MAX_SECONDS:
        raise PublishError(f"video is {float(text.duration_s):.0f} s — TikTok uploads take at most {INBOX_MAX_SECONDS} s")
    chunk, count, ranges = chunk_plan(size, cfg.get("publish.tiktok.chunk_mb", 10))
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
    which = mode(cfg)
    if which == "direct":
        info = _creator(cfg, client, token)
        levels = [str(x) for x in (info.get("privacy_level_options") or [])]
        want = str(cfg.get("publish.tiktok.privacy", "PUBLIC_TO_EVERYONE"))
        if want not in levels:
            raise PublishError(f"TikTok offers only {', '.join(levels) or 'no'} privacy for this app (wanted {want}) — "
                               "the app isn't audited yet; keep publish.tiktok.mode: inbox")
        max_s = info.get("max_video_post_duration_sec")
        if text.duration_s and max_s and float(text.duration_s) > float(max_s):
            raise PublishError(f"video is {float(text.duration_s):.0f} s — this creator may post at most {max_s} s")
        post_info = {"title": _caption(text), "privacy_level": want,
                     "disable_duet": bool(info.get("duet_disabled")), "disable_comment": bool(info.get("comment_disabled")),
                     "disable_stitch": bool(info.get("stitch_disabled")),
                     "is_aigc": bool(cfg.get("publish.tiktok.is_aigc", True))}
        data = _api(client, DIRECT_INIT, token, {"post_info": post_info, "source_info": source})
    else:
        data = _api(client, INBOX_INIT, token, {"source_info": source})
    publish_id, upload_url = str(data.get("publish_id") or ""), str(data.get("upload_url") or "")
    if not publish_id or not upload_url:
        raise PublishError(f"TikTok init gave no upload URL: {str(data)[:200]}")
    _upload(client, upload_url, video, size, ranges)
    final = _wait(cfg, client, token, publish_id)
    who = account(cfg)
    if which == "direct":
        ids = final.get("publicaly_available_post_id") or []
        url = f"https://www.tiktok.com/{who}/video/{ids[0]}" if ids and who else f"https://www.tiktok.com/{who or ''}"
        return Posted(publish_id, url, "published")
    _send_caption(cfg, client, text, video_id)
    return Posted(publish_id, f"tiktok://inbox/{who or 'me'}", "exported")


__all__ = ["account", "authorize", "chunk_plan", "connected", "finish_web", "missing", "mode", "publish",
           "redirect_uri", "scopes", "start_web", "token_file", "web_redirect"]
