"""YouTube Shorts upload over plain httpx (Data API v3, resumable upload).

Auth: `python -m src.main youtube-auth` runs the installed-app OAuth flow once (loopback redirect +
PKCE) and stores the refresh token in data/youtube.token.json (gitignored). Every publish trades it
for a short-lived access token. Scopes include read-only analytics for Phase 9, so no re-auth then.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from src.config import Config
from src.discover.common import FetchError, request
from src.publish.common import Posted, PostText, PublishError, PublishSkipped

log = logging.getLogger("raij.publish")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
UPLOAD_URI = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URI = "https://www.googleapis.com/youtube/v3"
CHUNK = 8 * 1024 * 1024                    # multiple of 256 KiB, as the API requires
MAX_STALLS = 3                              # consecutive 308s with no progress before giving up (A12)
# YouTube category ids by story category.
CATEGORY_IDS = {"news-lite": "25", "tech": "28", "sports": "17", "culture": "24", "wow-facts": "27",
                "life-hack": "26"}


def secret_file(cfg: Config) -> Path:
    return cfg.root / (cfg.secret("YOUTUBE_OAUTH_CLIENT_SECRET_FILE") or "client_secret.json")


def token_file(cfg: Config) -> Path:
    return cfg.root / "data" / "youtube.token.json"


def missing(cfg: Config) -> str | None:
    if not secret_file(cfg).exists():
        return f"no OAuth client file at {secret_file(cfg).name} (SETUP.md §6)"
    if not token_file(cfg).exists():
        return "not authorized yet — run `uv run python -m src.main youtube-auth`"
    return None


def _client_info(cfg: Config) -> dict:
    data = json.loads(secret_file(cfg).read_text(encoding="utf-8"))
    info = data.get("installed") or data.get("web")
    if not info or not info.get("client_id"):
        raise PublishSkipped(f"{secret_file(cfg).name} is not a Desktop-app OAuth client")
    return info


# --- one-time authorization -------------------------------------------------

def authorize(cfg: Config, client: httpx.Client, open_browser: Callable[[str], object] = webbrowser.open,
              timeout: float = 300) -> Path:
    """Browser consent → refresh token saved to data/youtube.token.json."""
    info = _client_info(cfg)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
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
            self.wfile.write("<h2>Ra'ij: authorized — you can close this tab.</h2>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URI + "?" + urlencode({
        "client_id": info["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Open this URL to authorize YouTube uploads (it should open by itself):\n{url}\n", flush=True)
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
        raise PublishError(f"authorization refused: {got.get('error') or 'state mismatch'}")
    resp = request(client, "POST", info.get("token_uri") or TOKEN_URI, retries=1, data={
        "code": got["code"], "client_id": info["client_id"], "client_secret": info.get("client_secret", ""),
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
    token = resp.json()
    if not token.get("refresh_token"):
        raise PublishError("Google returned no refresh token — remove the app's access at "
                           "myaccount.google.com/permissions and run youtube-auth again")
    path = token_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"refresh_token": token["refresh_token"], "scope": token.get("scope")}).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # private from the first byte (S6)
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    os.chmod(path, 0o600)                                                  # in case the file pre-existed
    return path


def access_token(cfg: Config, client: httpx.Client) -> str:
    info = _client_info(cfg)
    refresh = json.loads(token_file(cfg).read_text(encoding="utf-8"))["refresh_token"]
    try:
        resp = request(client, "POST", info.get("token_uri") or TOKEN_URI, retries=1, data={
            "client_id": info["client_id"], "client_secret": info.get("client_secret", ""),
            "refresh_token": refresh, "grant_type": "refresh_token"})
    except FetchError as exc:
        if "HTTP 400" in str(exc) or "HTTP 401" in str(exc):
            raise PublishError("YouTube authorization expired or was revoked — run "
                               "`uv run python -m src.main youtube-auth` again") from None
        raise
    return resp.json()["access_token"]


# --- upload --------------------------------------------------------------------

def metadata(cfg: Config, text: PostText) -> dict:
    title = text.title if len(text.title) <= 90 else text.title[:89] + "…"
    return {
        "snippet": {"title": f"{title} #Shorts", "description": f"{text.caption}\n\n#Shorts"[:4900],
                    "tags": [t.lstrip("#") for t in text.hashtags][:15],
                    "categoryId": CATEGORY_IDS.get(text.category or "", "24"),
                    "defaultLanguage": "ar", "defaultAudioLanguage": "ar"},
        "status": {"privacyStatus": cfg.get("publish.youtube_privacy", "public"),
                   "selfDeclaredMadeForKids": False},
    }


def existing(client: httpx.Client, auth: dict, title: str, days: int = 7) -> str | None:
    """Id of a recent upload on the channel with exactly this title, if any (2 quota units). Guards against a
    second upload when the state saved after the first one was lost (cache save failed, stale bootstrap)."""
    from datetime import datetime, timedelta, timezone
    try:
        ch = request(client, "GET", f"{API_URI}/channels?part=contentDetails&mine=true", headers=auth).json()
        uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        items = request(client, "GET", f"{API_URI}/playlistItems?part=snippet&maxResults=25&playlistId={uploads}",
                        headers=auth).json().get("items", [])
    except (FetchError, KeyError, IndexError, ValueError) as exc:
        log.warning("Couldn't list recent YouTube uploads (%s) — uploading without the duplicate check", exc)
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for it in items:
        s = it.get("snippet") or {}
        if s.get("title") == title and (s.get("publishedAt") or "") >= since:
            return (s.get("resourceId") or {}).get("videoId")
    return None


def publish(cfg: Config, client: httpx.Client, video: Path, text: PostText, video_id: int) -> Posted:
    token = access_token(cfg, client)
    auth = {"Authorization": f"Bearer {token}"}
    dup = existing(client, auth, metadata(cfg, text)["snippet"]["title"])
    if dup:
        log.warning("Video %d is already on YouTube as %s (same title, last 7 days) — adopting it, not re-uploading",
                    video_id, dup)
        return Posted(dup, f"https://youtube.com/shorts/{dup}")
    size = video.stat().st_size
    if not size:
        raise PublishError(f"{video.name} is empty — nothing to upload")
    start = request(client, "POST", f"{UPLOAD_URI}?uploadType=resumable&part=snippet,status", json=metadata(cfg, text),
                    headers={**auth, "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
    session = start.headers.get("location")
    if not session:
        raise PublishError("YouTube gave no upload session URL")
    with video.open("rb") as f:
        offset, stalled = 0, 0
        while offset < size:
            chunk = f.read(CHUNK)
            end = offset + len(chunk) - 1
            resp = request(client, "PUT", session, content=chunk, timeout=300,
                           headers={**auth, "Content-Length": str(len(chunk)),
                                    "Content-Range": f"bytes {offset}-{end}/{size}"})
            if resp.status_code == 308:                   # resume incomplete: server says how much it has
                have = resp.headers.get("range")
                new_offset = int(have.rsplit("-", 1)[1]) + 1 if have else offset
                # A 308 that reports no progress means the chunk was dropped; a few resends are fine,
                # an endless loop of the same 8 MiB is not (A12).
                stalled = stalled + 1 if new_offset <= offset else 0
                if stalled > MAX_STALLS:
                    raise PublishError(f"YouTube upload stalled at byte {offset} of {size}")
                offset = new_offset
                f.seek(offset)
                continue
            offset = size
    body = resp.json()
    if not body.get("id"):
        raise PublishError(f"YouTube upload finished without a video id: {str(body)[:200]}")
    return Posted(body["id"], f"https://youtube.com/shorts/{body['id']}")
