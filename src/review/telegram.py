"""Minimal Telegram Bot API client over httpx (no python-telegram-bot dependency).

The bot token is part of every request path, so errors and logs never include URLs.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("raij.review")

API = "https://api.telegram.org/bot{token}/{method}"
MAX_VIDEO_BYTES = 50 * 1024 * 1024       # bots can upload at most 50 MB
MAX_CAPTION = 1024
MAX_MESSAGE = 4096


class TelegramError(RuntimeError):
    """A Bot API call failed. The message never contains the token."""


class Bot:
    def __init__(self, token: str, client: httpx.Client | None = None):
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        self._token = token
        self._client = client or httpx.Client(timeout=httpx.Timeout(60.0, read=120.0))

    def call(self, method: str, files: dict[str, Any] | None = None, retries: int = 2, **params: Any) -> Any:
        url = API.format(token=self._token, method=method)
        # Nested objects (reply_markup) must be JSON strings in multipart form fields.
        data = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                for k, v in params.items() if v is not None}
        for attempt in range(retries + 1):
            try:
                if files:
                    resp = self._client.post(url, data=data, files=files)
                else:
                    resp = self._client.post(url, json={k: v for k, v in params.items() if v is not None})
            except httpx.HTTPError as exc:
                if attempt == retries:
                    raise TelegramError(f"{method}: {type(exc).__name__}") from None
                time.sleep(2 ** attempt)
                continue
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if body.get("ok"):
                return body.get("result")
            retry_after = (body.get("parameters") or {}).get("retry_after")
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < retries:
                time.sleep(retry_after or 2 ** attempt)
                continue
            raise TelegramError(f"{method}: HTTP {resp.status_code} {body.get('description', '')}".rstrip())
        raise AssertionError("unreachable")

    # -- helpers ---------------------------------------------------------------
    def send_message(self, chat_id: str, text: str, **kw: Any) -> dict:
        return self.call("sendMessage", chat_id=chat_id, text=text[:MAX_MESSAGE], **kw)

    def send_video(self, chat_id: str, path: Path, caption: str, reply_markup: dict | None = None,
                   **kw: Any) -> dict:
        with open(path, "rb") as f:
            return self.call("sendVideo", files={"video": (path.name, f, "video/mp4")}, chat_id=chat_id,
                             caption=caption[:MAX_CAPTION], supports_streaming="true",
                             reply_markup=reply_markup, **kw)

    def edit_markup(self, chat_id: str, message_id: int, reply_markup: dict | None) -> Any:
        return self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                         reply_markup=reply_markup or {"inline_keyboard": []})

    def edit_caption(self, chat_id: str, message_id: int, caption: str, reply_markup: dict | None = None) -> Any:
        return self.call("editMessageCaption", chat_id=chat_id, message_id=message_id,
                         caption=caption[:MAX_CAPTION], reply_markup=reply_markup)

    def answer(self, callback_id: str, text: str = "") -> Any:
        return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])

    def set_commands(self, commands: list[tuple[str, str]]) -> Any:
        """The slash menu Telegram shows in the chat (U8)."""
        return self.call("setMyCommands", commands=[{"command": c, "description": d[:256]} for c, d in commands])

    def updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return self.call("getUpdates", offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"]) or []
