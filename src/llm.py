"""One LLM adapter for the whole pipeline: Gemini free tier → Groq free tier → local Ollama.

Order comes from config `llm.fallback_order`; keys and models from .env. A provider with no key
is skipped; a provider that errors or returns unusable output falls through to the next.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

import httpx

from src.config import ROOT, Config
from src.discover.common import FetchError, request

log = logging.getLogger("raij.llm")

PROMPTS_DIR = ROOT / "src" / "prompts"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Free tiers return 429/503 under load; back off 1+2+4+8s before falling through.
CLOUD_RETRIES = 4


class LLMError(RuntimeError):
    """Every provider in the fallback order failed or was unavailable."""


class ProviderSkipped(RuntimeError):
    pass


def load_prompt(name: str, **values: str) -> str:
    """Read src/prompts/<name>.txt and fill {{placeholders}}."""
    text = (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    missing = re.findall(r"\{\{(\w+)\}\}", text)
    if missing:
        raise ValueError(f"prompt {name}: unfilled placeholders {missing}")
    return text


def _gemini(cfg: Config, client: httpx.Client, prompt: str, system: str | None, json_mode: bool) -> str:
    key = cfg.secret("GEMINI_API_KEY")
    if not key:
        raise ProviderSkipped("GEMINI_API_KEY not set")
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    model = cfg.secret("GEMINI_MODEL", "gemini-flash-latest")
    resp = request(client, "POST", GEMINI_URL.format(model=model), json=body,
                   headers={"x-goog-api-key": key}, retries=CLOUD_RETRIES)
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, ValueError):
        raise FetchError("Gemini returned no candidates (blocked or empty)") from None
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _groq(cfg: Config, client: httpx.Client, prompt: str, system: str | None, json_mode: bool) -> str:
    key = cfg.secret("GROQ_API_KEY")
    if not key:
        raise ProviderSkipped("GROQ_API_KEY not set")
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body: dict[str, Any] = {
        "model": cfg.secret("GROQ_MODEL", "llama-3.3-70b-versatile"),
        "messages": messages,
        "temperature": 0.3,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    resp = request(client, "POST", GROQ_URL, json=body, headers={"Authorization": f"Bearer {key}"},
                   retries=CLOUD_RETRIES)
    return resp.json()["choices"][0]["message"]["content"]


def _ollama(cfg: Config, client: httpx.Client, prompt: str, system: str | None, json_mode: bool) -> str:
    host = cfg.secret("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body: dict[str, Any] = {
        "model": cfg.secret("OLLAMA_MODEL", "qwen2.5:7b"),
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.3},
    }
    if json_mode:
        body["format"] = "json"
    resp = request(client, "POST", f"{host}/api/chat", json=body, retries=0)
    return resp.json()["message"]["content"]


PROVIDERS: dict[str, Callable[..., str]] = {"gemini": _gemini, "groq": _groq, "ollama": _ollama}


def parse_json(text: str) -> Any:
    """Parse model output as JSON, tolerating ```json fences and prose around the object."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _run(cfg: Config, prompt: str, system: str | None, json_mode: bool,
         parse: Callable[[str], Any], client: httpx.Client | None) -> Any:
    order = cfg.get("llm.fallback_order", ["gemini", "groq", "ollama"])
    own_client = client is None
    client = client or httpx.Client(timeout=120.0)
    failures = []
    try:
        for name in order:
            provider = PROVIDERS.get(name)
            if provider is None:
                failures.append(f"{name}: unknown provider")
                continue
            try:
                out = parse(provider(cfg, client, prompt, system, json_mode))
            except ProviderSkipped as exc:
                failures.append(f"{name}: {exc}")
                continue
            except (FetchError, KeyError, IndexError, ValueError) as exc:
                log.warning("LLM %s failed: %s", name, exc)
                failures.append(f"{name}: {exc}")
                continue
            log.debug("LLM answered by %s", name)
            return out
    finally:
        if own_client:
            client.close()
    raise LLMError("no LLM provider succeeded — " + "; ".join(failures))


def complete(cfg: Config, prompt: str, *, system: str | None = None,
             client: httpx.Client | None = None) -> str:
    return _run(cfg, prompt, system, False, lambda s: s, client)


def complete_json(cfg: Config, prompt: str, *, system: str | None = None,
                  client: httpx.Client | None = None) -> Any:
    return _run(cfg, prompt, system, True, parse_json, client)


def available_providers(cfg: Config) -> list[str]:
    """Providers that have credentials configured (Ollama is always listed; it may not be running)."""
    order = cfg.get("llm.fallback_order", ["gemini", "groq", "ollama"])
    keys = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}
    return [p for p in order if p not in keys or cfg.secret(keys[p])]

