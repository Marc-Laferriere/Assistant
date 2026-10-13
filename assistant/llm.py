"""Thin LLM client. Only Gemini (REST, urllib) is wired up so far; the provider
dispatch is here so a second provider is a local change, not a rewrite.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from .config import SECRET_GEMINI_API_KEY
from .secrets_store import get_secret

log = logging.getLogger(__name__)

_GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"


class LLMError(RuntimeError):
    pass


def complete_json(
    model: str,
    system: str,
    user: str,
    *,
    schema: dict | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    max_retries: int = 4,
    thinking_budget: int = 0,
) -> dict:
    """Return the model's JSON response as a dict.

    ``thinking_budget`` 0 disables the model's internal reasoning (right for
    classification/extraction — it otherwise eats the output budget and truncates
    the JSON). Raise it for genuine reasoning tasks like planning.
    """
    if model.startswith(("gemini", "models/gemini")):
        return _gemini_json(model, system, user, schema, temperature, max_tokens,
                            max_retries, thinking_budget)
    raise LLMError(f"unsupported model {model!r} (only Gemini is wired up)")


def _gemini_json(model, system, user, schema, temperature, max_tokens,
                 max_retries, thinking_budget) -> dict:
    name = model if model.startswith("models/") else f"models/{model}"
    key = get_secret(SECRET_GEMINI_API_KEY)
    url = f"{_GEMINI_ROOT}/{name}:generateContent"
    gen_cfg: dict = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
        "responseMimeType": "application/json",
        "thinkingConfig": {"thinkingBudget": thinking_budget},
    }
    if schema:
        gen_cfg["responseSchema"] = schema
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": gen_cfg,
    }
    data = json.dumps(body).encode()

    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.load(resp)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if exc.code in (429, 500, 503) and attempt <= max_retries:
                wait = min(2 ** attempt, 30)
                log.warning("Gemini %s, retry %d in %ds", exc.code, attempt, wait)
                time.sleep(wait)
                continue
            raise LLMError(f"Gemini HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            if attempt <= max_retries:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise LLMError(f"Gemini connection error: {exc}") from None

    try:
        cand = payload["candidates"][0]
        finish = cand.get("finishReason")
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        if finish not in (None, "STOP", "MAX_TOKENS"):
            log.warning("Gemini finishReason=%s", finish)
        try:
            return json.loads(text)
        except ValueError:
            if finish == "MAX_TOKENS":
                raise LLMError(
                    f"response truncated at {max_tokens} tokens — raise ai.* limits"
                ) from None
            raise
    except LLMError:
        raise
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"unexpected Gemini response: {str(payload)[:300]}") from exc
