"""Small client for a llama.cpp server that speaks the OpenAI chat route."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class ChatResult:
    text: str
    raw: dict


class ModelOffline(RuntimeError):
    pass


def health(base_url: str, timeout: float = 3.0) -> bool:
    # The OpenAI base is usually .../v1. Health lives on the server root.
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        with urllib.request.urlopen(root + "/health", timeout=timeout) as resp:
            body = resp.read(200).decode("utf-8", "replace")
            return "ok" in body.lower() or resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _headers(key: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    token = (key or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def chat(base_url: str, model: str, prompt: str, timeout: float = 120.0, system: str = "", key: str = "") -> ChatResult:
    root = base_url.rstrip("/")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": model, "messages": messages, "temperature": 0.2}
    data = json.dumps(payload).encode("utf-8")
    url = root if root.endswith("/v1") else root + "/v1"
    req = urllib.request.Request(
        url + "/chat/completions",
        data=data,
        headers=_headers(key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Older servers only have /completion.
        if exc.code in (404, 400, 500):
            return _completion(root, prompt, timeout, system, key)
        raise ModelOffline(str(exc)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelOffline(str(exc)) from exc
    try:
        text = doc["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        text = doc.get("content") or ""
    if not text:
        raise ModelOffline("bad chat response")
    return ChatResult(text=text or "", raw=doc)


def _completion(root: str, prompt: str, timeout: float, system: str, key: str = "") -> ChatResult:
    bare = root[:-3] if root.endswith("/v1") else root
    text = (system + "\n" + prompt).strip()
    data = json.dumps({"prompt": text, "n_predict": 64, "temperature": 0.2}).encode("utf-8")
    req = urllib.request.Request(
        bare + "/completion",
        data=data,
        headers=_headers(key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ModelOffline(str(exc)) from exc
    return ChatResult(text=str(doc.get("content") or ""), raw=doc)
