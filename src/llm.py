"""One LLM adapter for the whole pipeline: Gemini free tier → Groq free tier → local Ollama.

Order comes from config `llm.fallback_order`; keys and models from .env. A provider with no key
is skipped; a provider that errors or returns unusable output falls through to the next.
"""
from __future__ import annotations

import json
import logging
import re
import time
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
PARSE_RETRIES = 1
# Gemini models that returned 429 (quota) or 404 (retired) this process; skipped until the next run.
_EXHAUSTED: set[str] = set()
# Models that answered 503 "high demand": skipped until the deadline (monotonic seconds), then probed again. A
# spike is temporary, but re-asking five overloaded models on every call cost ~25 s per call live (2026-09-24).
_OVERLOADED: dict[str, float] = {}
OVERLOAD_COOLDOWN = 300.0


# Failure texts that mean the provider, not the prompt, was the problem: unreachable, throttled, overloaded,
# or not configured. `LLMError.outage` is True when every provider failed that way.
_OUTAGE_MARKS = ("ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
                 "RemoteProtocolError", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
                 "not set", "out of quota", "unknown provider")


class LLMError(RuntimeError):
    """Every provider in the fallback order failed or was unavailable."""

    def __init__(self, message: str, failures: list[str] | None = None) -> None:
        super().__init__(message)
        self.failures = list(failures or [])

    @property
    def outage(self) -> bool:
        """No provider could be reached or had quota (nothing about this item was at fault) — the stages then
        leave the item for the next run without counting an attempt (pipeline.max_age_days still bounds it)."""
        return bool(self.failures) and all(any(mark in f for mark in _OUTAGE_MARKS) for f in self.failures)


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


_LAST: dict[str, str | None] = {"model": None}


def last_model() -> str | None:
    """Which model produced the most recent answer in this process (recorded in scripts.notes.model)."""
    return _LAST["model"]


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
    # Free-tier quotas are per model (gemini-flash-latest allows only 20 requests/day), so
    # fall through to the next model when one is out of quota, overloaded, or retired.
    models = [cfg.secret("GEMINI_MODEL", "gemini-flash-latest")] + cfg.get("llm.gemini_fallback_models", [])
    last: FetchError | None = None
    now = time.monotonic()
    cooldown = float(cfg.get("llm.overload_cooldown_seconds", OVERLOAD_COOLDOWN))
    live = [m for m in dict.fromkeys(models) if m not in _EXHAUSTED and _OVERLOADED.get(m, 0.0) <= now]
    if not live:                                        # everything is cooling down: ask the least recent one
        live = [m for m in dict.fromkeys(models) if m not in _EXHAUSTED][-1:]
    for i, model in enumerate(live):
        # Only the last model gets the full backoff; otherwise an overloaded one is left fast.
        retries = CLOUD_RETRIES if i == len(live) - 1 else 1
        try:
            resp = request(client, "POST", GEMINI_URL.format(model=model), json=_gemini_body(body, model, json_mode),
                           headers={"x-goog-api-key": key}, retries=retries)
        except FetchError as exc:
            if any(f"HTTP {code}" in str(exc) for code in (404, 429, 503)):
                if "HTTP 503" in str(exc):                      # overloaded: rest it, re-probe later
                    _OVERLOADED[model] = time.monotonic() + cooldown
                else:                                           # out of quota or retired
                    _EXHAUSTED.add(model)
                log.warning("Gemini %s unavailable, trying next model: %s", model, exc)
                last = exc
                continue
            raise
        try:
            parts = resp.json()["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, ValueError):
            raise FetchError(f"Gemini {model} returned no candidates (blocked or empty)") from None
        log.debug("Gemini answered with %s", model)
        _LAST["model"] = model
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))
    raise last or FetchError("every Gemini model is out of quota this run")


def _gemini_body(body: dict[str, Any], model: str, json_mode: bool) -> dict[str, Any]:
    """Gemma models reject responseMimeType with a 400 (A11), so JSON mode is asked only of Gemini
    models; `parse_json` copes with fences and prose around the object either way."""
    if not json_mode or model.startswith("gemma"):
        return body
    return {**body, "generationConfig": {**body["generationConfig"], "responseMimeType": "application/json"}}


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
            # Malformed output (e.g. a garbled \u escape in JSON) is usually a one-off, so ask the
            # same provider once more; HTTP failures were already retried inside request().
            for attempt in range(PARSE_RETRIES + 1):
                try:
                    raw = provider(cfg, client, prompt, system, json_mode)
                except ProviderSkipped as exc:
                    failures.append(f"{name}: {exc}")
                    break
                except (FetchError, KeyError, IndexError, ValueError) as exc:
                    log.warning("LLM %s failed: %s", name, exc)
                    failures.append(f"{name}: {exc}")
                    break
                try:
                    out = parse(raw)
                except (KeyError, IndexError, ValueError) as exc:
                    log.warning("LLM %s returned unparseable output (attempt %d): %s", name, attempt + 1, exc)
                    if attempt == PARSE_RETRIES:
                        failures.append(f"{name}: unparseable output: {exc}")
                    continue
                log.debug("LLM answered by %s", name)
                return out
    finally:
        if own_client:
            client.close()
    raise LLMError("no LLM provider succeeded — " + "; ".join(failures), failures)


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

