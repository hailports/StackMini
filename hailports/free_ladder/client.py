"""In-process + HTTP client helpers for callers on the Mini."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .config import LADDER_PORT, load_keys


def ladder_base_url() -> str:
    return os.environ.get("FREE_LADDER_URL", f"http://127.0.0.1:{LADDER_PORT}").rstrip("/")


def complete(
    prompt: str,
    *,
    system: str | None = None,
    model: str = "auto",
    max_tokens: int = 1024,
    temperature: float | None = 0.2,
    timeout: float = 120.0,
    skip_local: bool = False,
) -> tuple[str | None, str | None]:
    """Return (text, provider_name) or (None, None). Uses the live :8790 gateway."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if temperature is not None:
        body["temperature"] = temperature
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "free-ladder-client/1.0",
    }
    if skip_local:
        headers["X-Free-Ladder-Skip-Local"] = "1"
    data = json.dumps(body).encode("utf-8")
    url = ladder_base_url() + "/v1/chat/completions"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        # in-process fallback if gateway down
        try:
            from .router import route_chat

            code, payload, meta = route_chat(body, keys=load_keys(), headers=headers)
            if code >= 400:
                return None, None
        except Exception:
            return None, None
    try:
        text = (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        provider = (payload.get("free_ladder") or {}).get("provider") or payload.get("model")
        if not text.strip():
            return None, None
        return text.strip(), str(provider)
    except Exception:
        return None, None


async def try_free_ladder(
    prompt: str,
    system: str | None = None,
    max_tokens: int = 4096,
    **_kwargs,
) -> tuple[str | None, str | None]:
    """Async-compatible wrapper matching free_llm_pool.try_free_providers shape."""
    import asyncio

    return await asyncio.to_thread(
        complete, prompt, system=system, max_tokens=max_tokens
    )
