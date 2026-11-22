"""Trusted provider endpoints, account model discovery and typed outputs. No credentials in model context."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from typing import Literal

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel

from .config import get_settings

Provider = Literal["openai", "gemini", "anthropic", "groq"]
BASES = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "anthropic": "https://api.anthropic.com/v1",
    "groq": "https://api.groq.com/openai/v1",
}
_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_LOCK = threading.Lock()


class ProviderError(RuntimeError):
    """Only fixed safe messages, never provider response bodies or request headers."""

    def __init__(self, message: str, status_code: int = 400, *, uncertain: bool = False):
        super().__init__(message)
        self.status_code, self.uncertain = status_code, uncertain


def encryption_ready() -> bool:
    try:
        _cipher()
        return True
    except ProviderError:
        return False


def _cipher() -> Fernet:
    try:
        return Fernet(get_settings().credential_encryption_key.get_secret_value().encode())
    except (ValueError, TypeError):
        raise ProviderError("Personal keys are unavailable until server-side encryption is configured.", 503) from None


def encrypt_key(user_id: str, credential_id: str, provider: str, key: str) -> str:
    return _cipher().encrypt(json.dumps([user_id, credential_id, provider, key]).encode()).decode()


def decrypt_key(user_id: str, credential: dict) -> str:
    try:
        owner, identity, provider, key = json.loads(_cipher().decrypt(credential["ciphertext"].encode()))
        if owner != user_id or identity != credential["id"] or provider != credential["provider"]:
            raise ValueError("wrong owner")
        return key
    except (InvalidToken, ValueError, TypeError):
        raise ProviderError("This credential is unavailable. Replace the key in AI settings.", 409) from None


def shared_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise ProviderError("Pantree default API is not configured. Connect a personal key in AI settings.", 503)
    return key


def key_version(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def rates(provider: str, model: str) -> tuple | None:
    prices = get_settings().prices
    rate = prices.get(f"{provider}:{model}") or (prices.get(model) if provider == "openai" else None)
    if rate is not None and (len(rate) != 3 or any(not math.isfinite(x) or x < 0 for x in rate)):
        raise ProviderError("Model pricing configuration is invalid.", 503)
    return rate


def compatible(provider: str, model: str) -> bool:
    """Known text/tool families only; a model must also be returned by this credential's provider API."""
    if re.search(r"audio|realtime|image|embedding|tts|transcrib|moderation|search|computer|deep-research|guard", model):
        return False
    if provider == "openai":
        return bool(re.match(r"(?:gpt-(?:4|5|6)|o[134])", model)) and model not in ("o1-mini", "o1-preview")
    if provider == "gemini":
        return bool(re.match(r"gemini-(?:2\.[05]|[3-9])", model)) and "embedding" not in model
    if provider == "anthropic":
        return model.startswith(("claude-3", "claude-sonnet", "claude-opus", "claude-haiku"))
    return model.startswith(("llama-", "meta-llama/", "qwen/", "openai/gpt-oss-", "moonshotai/"))


def _request(provider: str, key: str, path: str, *, payload: dict | None = None, params: dict | None = None) -> dict:
    if provider not in BASES:
        raise ProviderError("Unsupported provider")
    if not re.fullmatch(r"[!-~]{8,512}", key):
        raise ProviderError("Invalid API-key format", 400)
    headers = {"Content-Type": "application/json"}
    if provider == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif provider == "gemini":
        headers["x-goog-api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    try:
        # No redirects, custom endpoints or SDK retries: one reservation represents exactly one attempt.
        response = httpx.request(
            "POST" if payload is not None else "GET",
            BASES[provider] + path,
            headers=headers,
            json=payload,
            params=params,
            timeout=get_settings().llm_timeout_s,
            follow_redirects=False,
        )
    except (httpx.HTTPError, UnicodeError, ValueError):
        raise ProviderError(
            "The provider did not confirm completion. Retry later; no shared fallback was used.",
            504,
            uncertain=payload is not None,
        ) from None
    if response.status_code >= 300:
        code = response.status_code
        message = (
            "The provider rejected this API key."
            if code in (401, 403)
            else "The provider is rate-limited or its billing allowance is exhausted."
            if code == 429
            else "This model is unavailable or rejected the structured-output request."
            if code in (400, 404, 422)
            else "The provider could not complete the request."
        )
        raise ProviderError(message, code if code < 500 else 502, uncertain=code >= 500 and payload is not None)
    try:
        return response.json()
    except ValueError:
        raise ProviderError("The provider returned an invalid response.", 502, uncertain=payload is not None) from None


def discover(provider: str, key: str, user_id: str, *, refresh: bool = False) -> list[dict]:
    identity = (user_id, provider, key_version(key))
    with _LOCK:
        cached = _CACHE.get(identity)
        if cached and not refresh and time.monotonic() - cached[0] < 300:
            return cached[1]
    models, token = [], None
    for _ in range(10):
        params = (
            {"pageSize": 100, **({"pageToken": token} if token else {})}
            if provider == "gemini"
            else {"limit": 100, **({"after_id": token} if token else {})}
            if provider == "anthropic"
            else None
        )
        result = _request(provider, key, "/models", params=params)
        for item in result.get("models" if provider == "gemini" else "data", []):
            name = (item.get("id") or item.get("name", "")).removeprefix("models/")
            if (
                compatible(provider, name)
                and item.get("active", True)
                and (provider != "gemini" or "generateContent" in item.get("supportedGenerationMethods", []))
            ):
                models.append(
                    {
                        "id": name,
                        "name": item.get("displayName") or item.get("display_name") or name,
                        "rates": rates(provider, name),
                        "structured_output": "validated JSON/tool output",
                    }
                )
        token = (
            result.get("nextPageToken")
            if provider == "gemini"
            else result.get("last_id")
            if result.get("has_more")
            else None
        )
        if not token:
            break
    models = sorted({m["id"]: m for m in models}.values(), key=lambda m: m["id"])
    with _LOCK:
        if len(_CACHE) >= 128:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[identity] = (time.monotonic(), models)
    return models


def invoke(provider: str, key: str, model: str, schema: type[BaseModel], system: str, user: str) -> tuple[dict, dict]:
    """Return raw typed arguments and normalized usage; parsing is done only after cost reconciliation."""
    cap = get_settings().max_output_tokens
    shape = schema.model_json_schema()
    if provider == "openai":
        from openai.lib._pydantic import to_strict_json_schema

        payload = {
            "model": model,
            "input": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": [
                {
                    "type": "function",
                    "name": "respond",
                    "description": "Return the analytical structure",
                    "parameters": to_strict_json_schema(schema),
                    "strict": True,
                }
            ],
            "tool_choice": {"type": "function", "name": "respond"},
            "max_output_tokens": cap,
            "store": False,
        }
        if re.match(r"(?:gpt-[56]|o[134])", model) and get_settings().reasoning_effort != "none":
            payload["reasoning"] = {"effort": get_settings().reasoning_effort}
        result = _request(provider, key, "/responses", payload=payload)
        u = result.get("usage", {})
        usage = {
            "input": u.get("input_tokens", 0),
            "cached": u.get("input_tokens_details", {}).get("cached_tokens", 0),
            "output": u.get("output_tokens", 0),
            "reported": 'input_tokens' in u and 'output_tokens' in u,
        }
        value = next(
            (
                item.get("arguments")
                for item in result.get("output", [])
                if item.get("type") == "function_call" and item.get("name") == "respond"
            ),
            None,
        )
    elif provider == "anthropic":
        result = _request(
            provider,
            key,
            "/messages",
            payload={
                "model": model,
                "max_tokens": cap,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "tools": [
                    {
                        "name": "respond",
                        "description": "Return the requested analytical structure",
                        "input_schema": shape,
                    }
                ],
                "tool_choice": {"type": "tool", "name": "respond"},
            },
        )
        u = result.get("usage", {})
        usage = {
            "input": u.get("input_tokens", 0)
            + u.get("cache_read_input_tokens", 0)
            + u.get("cache_creation_input_tokens", 0),
            "cached": u.get("cache_read_input_tokens", 0),
            "output": u.get("output_tokens", 0),
            "reported": 'input_tokens' in u and 'output_tokens' in u,
        }
        value = next(
            (
                b.get("input")
                for b in result.get("content", [])
                if b.get("type") == "tool_use" and b.get("name") == "respond"
            ),
            None,
        )
    elif provider == "gemini":
        result = _request(
            provider,
            key,
            f"/models/{model}:generateContent",
            payload={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "maxOutputTokens": cap,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": shape,
                },
            },
        )
        u = result.get("usageMetadata", {})
        usage = {
            "input": u.get("promptTokenCount", 0),
            "cached": u.get("cachedContentTokenCount", 0),
            "output": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
            "reported": 'promptTokenCount' in u and 'candidatesTokenCount' in u,
        }
        value = "".join(
            p.get("text", "")
            for c in result.get("candidates", [])
            for p in c.get("content", {}).get("parts", [])
            if not p.get("thought")
        )
    else:
        tool = {"name": "respond", "description": "Return the requested analytical structure", "parameters": shape}
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": [{"type": "function", "function": tool}],
            "tool_choice": {"type": "function", "function": {"name": "respond"}},
            "max_completion_tokens": cap,
        }
        result = _request(provider, key, "/chat/completions", payload=payload)
        u = result.get("usage", {})
        usage = {
            "input": u.get("prompt_tokens", 0),
            "cached": u.get("prompt_tokens_details", {}).get("cached_tokens", 0),
            "output": u.get("completion_tokens", 0),
            "reported": 'prompt_tokens' in u and 'completion_tokens' in u,
        }
        value = next(
            (
                t.get("function", {}).get("arguments")
                for c in result.get("choices", [])
                for t in c.get("message", {}).get("tool_calls", [])
                if t.get("function", {}).get("name") == "respond"
            ),
            None,
        )
    return value, usage
