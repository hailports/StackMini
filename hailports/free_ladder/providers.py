"""Provider adapters (OpenAI-compatible + paid CLI last resort)."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from .config import (
    DEFAULT_MODELS,
    OR_FREE_CACHE,
    PROVIDERS,
    TIMEOUTS,
    USER_AGENT,
    key_for,
    load_keys,
)
from .redact import redact_payload

_OR_FREE_TTL = 3600


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            out_headers = {k.lower(): v for k, v in resp.headers.items()}
            try:
                parsed = json.loads(raw.decode("utf-8", "replace") or "{}")
            except json.JSONDecodeError:
                parsed = {"raw": raw.decode("utf-8", "replace")[:2000]}
            return int(resp.status), parsed, out_headers
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        out_headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        try:
            parsed = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            parsed = {"error": {"message": raw.decode("utf-8", "replace")[:500]}}
        return int(e.code), parsed, out_headers


def _auth_headers(provider: str, keys: dict[str, str]) -> dict[str, str]:
    token = key_for(provider, keys)
    if not token:
        return {}
    if provider == "openrouter":
        return {
            "Authorization": f"Bearer {token}",
            "HTTP-Referer": "https://hailports.com",
            "X-Title": "free-ladder",
        }
    return {"Authorization": f"Bearer {token}"}


def _msg_content(msg: dict[str, Any]) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        bits = []
        for p in c:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                bits.append(p["text"])
            elif isinstance(p, str):
                bits.append(p)
        return "\n".join(bits)
    return ""


def _extract_text(resp: dict[str, Any]) -> str:
    choices = resp.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    # reasoning-only models (Cerebras gpt-oss / qwen) may put answer in reasoning
    reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
    if isinstance(reasoning, str) and reasoning.strip() and not content:
        # last non-empty line often is the answer when truncated mid-thought
        lines = [ln.strip() for ln in reasoning.strip().splitlines() if ln.strip()]
        return lines[-1] if lines else reasoning.strip()
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict)
        ).strip()
    return (content or "") if isinstance(content, str) else ""


def _usage_tokens(resp: dict[str, Any], payload: dict[str, Any]) -> int:
    u = resp.get("usage") or {}
    total = u.get("total_tokens")
    if total:
        return int(total)
    prompt = int(u.get("prompt_tokens") or 0)
    completion = int(u.get("completion_tokens") or 0)
    if prompt or completion:
        return prompt + completion
    # estimate
    blob = json.dumps(payload.get("messages") or [])[:20000]
    return max(1, len(blob) // 4)


def list_openrouter_free(keys: dict[str, str] | None = None, *, force: bool = False) -> list[str]:
    """Return OpenRouter model ids that are free (pricing==0) AND end with :free."""
    cache = OR_FREE_CACHE
    now = time.time()
    if not force and cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if now - float(cached.get("ts") or 0) < _OR_FREE_TTL:
                return list(cached.get("ids") or [])
        except Exception:
            pass
    keys = keys or load_keys()
    hdrs = _auth_headers("openrouter", keys)
    if not hdrs:
        return []
    code, data, _ = _http_json("GET", "https://openrouter.ai/api/v1/models", headers=hdrs, timeout=30)
    if code >= 400:
        return []
    ids: list[str] = []
    for m in data.get("data") or []:
        mid = str(m.get("id") or "")
        pricing = m.get("pricing") or {}
        try:
            prompt = float(pricing.get("prompt") or 0)
            completion = float(pricing.get("completion") or 0)
            request = float(pricing.get("request") or 0)
        except (TypeError, ValueError):
            continue
        # HARD: zero price AND :free suffix (or openrouter/free router)
        if prompt == 0 and completion == 0 and request == 0:
            if mid.endswith(":free") or mid == "openrouter/free":
                ids.append(mid)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"ts": now, "ids": ids}), encoding="utf-8")
    except OSError:
        pass
    return ids


def is_openrouter_free(model: str, keys: dict[str, str] | None = None) -> bool:
    mid = (model or "").strip()
    if not mid:
        return False
    if not (mid.endswith(":free") or mid == "openrouter/free"):
        return False
    free = set(list_openrouter_free(keys))
    return mid in free


def pick_openrouter_model(task: str, keys: dict[str, str] | None = None) -> str | None:
    free = list_openrouter_free(keys)
    if not free:
        return None
    prefer: list[str] = []
    if task == "code":
        prefer = [m for m in free if "coder" in m or "code" in m or "qwen" in m]
    elif task == "reasoning":
        prefer = [m for m in free if "r1" in m or "reason" in m or "ultra" in m or "nemotron" in m]
    elif task == "vision":
        prefer = [m for m in free if "vl" in m or "vision" in m or "omni" in m]
    elif task in {"summarize", "long_context"}:
        prefer = [m for m in free if ("gemma" in m or "nemotron" in m)]
    pool = prefer or free
    # prefer larger-sounding ids last among free; take first stable
    for needle in ("gemma-4-31b", "nemotron-3-ultra", "gemma-4-26b", "nemotron-3.5", "ernie"):
        for m in pool:
            if needle in m:
                return m
    return pool[0]


def model_for(provider: str, task: str, pin: str | None = None) -> str:
    if pin:
        return pin
    if provider == "local":
        return DEFAULT_MODELS["local"]
    if provider == "cerebras":
        return DEFAULT_MODELS["cerebras_strong"] if task in {"reasoning", "code"} else DEFAULT_MODELS["cerebras"]
    if provider == "groq":
        return DEFAULT_MODELS["groq_strong"] if task in {"reasoning", "code", "chat", "summarize"} else DEFAULT_MODELS["groq_fast"]
    if provider == "gemini":
        return DEFAULT_MODELS["gemini_pro"] if task == "reasoning" else DEFAULT_MODELS["gemini_flash"]
    if provider == "plugsky":
        return DEFAULT_MODELS["plugsky"]
    if provider == "glm":
        return DEFAULT_MODELS["glm"] if task in {"code", "reasoning"} else DEFAULT_MODELS["glm_fast"]
    if provider == "openrouter":
        return pick_openrouter_model(task) or "openrouter/free"
    return "auto"


def chat_completions(
    provider: str,
    payload: dict[str, Any],
    *,
    model: str,
    keys: dict[str, str] | None = None,
    stream: bool = False,
) -> tuple[int, dict[str, Any], dict[str, str], dict[str, Any]]:
    """Non-stream chat completion. Returns status, body, headers, meta."""
    keys = keys or load_keys()
    meta: dict[str, Any] = {"provider": provider, "model": model, "stream": False}
    t0 = time.time()

    if provider == "paid":
        return _paid_cli(payload, meta, t0)

    if provider == "openrouter":
        if not is_openrouter_free(model, keys):
            return 403, {"error": {"message": f"refused non-free openrouter model: {model}"}}, {}, {
                **meta,
                "outcome": "refused_paid_openrouter",
                "latency_ms": int((time.time() - t0) * 1000),
            }

    base = (PROVIDERS.get(provider) or {}).get("base")
    if not base:
        return 500, {"error": {"message": f"no base for {provider}"}}, {}, meta

    safe = redact_payload(payload)
    body = dict(safe)
    body["model"] = model
    body["stream"] = False
    # drop anthropic-only fields if present
    body.pop("metadata", None)

    hdrs = _auth_headers(provider, keys)
    if provider != "local" and not hdrs.get("Authorization"):
        return 401, {"error": {"message": f"missing key for {provider}"}}, {}, {
            **meta,
            "outcome": "no_key",
            "latency_ms": int((time.time() - t0) * 1000),
        }

    url = base.rstrip("/") + "/chat/completions"
    timeout = TIMEOUTS.get(provider, 60.0)
    code, data, out_h = _http_json("POST", url, headers=hdrs, body=body, timeout=timeout)
    latency = int((time.time() - t0) * 1000)
    # some providers (Gemini) return a JSON list on errors
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {"error": {"message": str(data)[:300]}}
    if not isinstance(data, dict):
        data = {"error": {"message": str(data)[:300]}}
    text = _extract_text(data) if code < 400 else ""
    # if content empty but choices exist with reasoning, synthesize content for clients
    if code < 400 and text and not ((data.get("choices") or [{}])[0].get("message") or {}).get("content"):
        try:
            data = dict(data)
            choices = list(data.get("choices") or [])
            if choices:
                ch0 = dict(choices[0])
                msg = dict(ch0.get("message") or {})
                msg["content"] = text
                ch0["message"] = msg
                choices[0] = ch0
                data["choices"] = choices
        except Exception:
            pass
    meta.update(
        {
            "latency_ms": latency,
            "tokens": _usage_tokens(data, body) if code < 400 else 0,
            "outcome": "ok" if code < 400 and text else ("http_error" if code >= 400 else "empty"),
            "retry_after": out_h.get("retry-after"),
            "served_model": out_h.get("x-plugsky-served-model") or (data.get("model") if isinstance(data, dict) else None),
        }
    )
    if provider == "plugsky":
        served = (out_h.get("x-plugsky-served-model") or "").lower()
        if any(x in served for x in ("lite", "micro", "gpu-27b")):
            meta["plugsky_alert"] = f"weak_engine:{served}"
    return code, data, out_h, meta


def _paid_cli(payload: dict[str, Any], meta: dict[str, Any], t0: float) -> tuple[int, dict[str, Any], dict[str, str], dict[str, Any]]:
    """Personal Claude then personal Codex — do not change how they authenticate."""
    safe = redact_payload(payload)
    msgs = safe.get("messages") or []
    prompt_parts = []
    for m in msgs:
        role = m.get("role", "user")
        prompt_parts.append(f"{role}: {_msg_content(m)}")
    prompt = "\n\n".join(prompt_parts)[:8000]
    # Claude CLI
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=TIMEOUTS["paid"],
            env=os.environ.copy(),
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            text = proc.stdout.strip()
            body = {
                "id": "paid-claude",
                "object": "chat.completion",
                "model": "personal-claude",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            meta.update({"latency_ms": int((time.time() - t0) * 1000), "outcome": "ok", "model": "personal-claude", "tokens": max(1, len(text) // 4)})
            return 200, body, {}, meta
    except Exception as exc:
        meta["claude_err"] = str(exc)[:120]
    # Codex CLI
    try:
        proc = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "-q", prompt[:6000]],
            capture_output=True,
            text=True,
            timeout=TIMEOUTS["paid"],
            env=os.environ.copy(),
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            text = proc.stdout.strip()
            body = {
                "id": "paid-codex",
                "object": "chat.completion",
                "model": "personal-codex",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            meta.update({"latency_ms": int((time.time() - t0) * 1000), "outcome": "ok", "model": "personal-codex", "tokens": max(1, len(text) // 4)})
            return 200, body, {}, meta
        meta["codex_err"] = (proc.stderr or proc.stdout or "")[:120]
    except Exception as exc:
        meta["codex_err"] = str(exc)[:120]
    meta.update({"latency_ms": int((time.time() - t0) * 1000), "outcome": "paid_fail"})
    return 502, {"error": {"message": "paid claude/codex failed"}}, {}, meta


def stream_chat_completions(
    provider: str,
    payload: dict[str, Any],
    *,
    model: str,
    keys: dict[str, str] | None = None,
) -> Iterator[bytes]:
    """Yield SSE bytes. Falls back to single-chunk if provider stream fails."""
    keys = keys or load_keys()
    if provider in {"paid", "openrouter"} and provider == "openrouter" and not is_openrouter_free(model, keys):
        err = json.dumps({"error": {"message": f"refused non-free openrouter model: {model}"}})
        yield f"data: {err}\n\n".encode()
        yield b"data: [DONE]\n\n"
        return

    base = (PROVIDERS.get(provider) or {}).get("base")
    if not base or provider == "paid":
        # non-stream then fake SSE
        code, data, _, _ = chat_completions(provider, payload, model=model, keys=keys)
        if code >= 400:
            yield f"data: {json.dumps(data)}\n\n".encode()
            yield b"data: [DONE]\n\n"
            return
        text = _extract_text(data)
        chunk = {
            "id": data.get("id") or "chunk",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"
        return

    safe = redact_payload(payload)
    body = dict(safe)
    body["model"] = model
    body["stream"] = True
    hdrs = _auth_headers(provider, keys)
    hdrs["Accept"] = "text/event-stream"
    url = base.rstrip("/") + "/chat/completions"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={**hdrs, "Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUTS.get(provider, 60.0)) as resp:
            while True:
                line = resp.readline()
                if not line:
                    break
                yield line if line.endswith(b"\n") else line + b"\n"
    except Exception:
        code, data, _, _ = chat_completions(provider, payload, model=model, keys=keys, stream=False)
        text = _extract_text(data) if code < 400 else ""
        chunk = {
            "id": "fallback",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"


def list_models(keys: dict[str, str] | None = None) -> list[dict[str, Any]]:
    keys = keys or load_keys()
    models = [
        {"id": "auto", "object": "model", "owned_by": "free-ladder"},
        {"id": "local/auto", "object": "model", "owned_by": "local"},
        {"id": f"cerebras/{DEFAULT_MODELS['cerebras']}", "object": "model", "owned_by": "cerebras"},
        {"id": f"cerebras/{DEFAULT_MODELS['cerebras_strong']}", "object": "model", "owned_by": "cerebras"},
        {"id": f"groq/{DEFAULT_MODELS['groq_fast']}", "object": "model", "owned_by": "groq"},
        {"id": f"groq/{DEFAULT_MODELS['groq_strong']}", "object": "model", "owned_by": "groq"},
        {"id": f"gemini/{DEFAULT_MODELS['gemini_flash']}", "object": "model", "owned_by": "gemini"},
        {"id": f"gemini/{DEFAULT_MODELS['gemini_pro']}", "object": "model", "owned_by": "gemini"},
        {"id": f"plugsky/{DEFAULT_MODELS['plugsky']}", "object": "model", "owned_by": "plugsky"},
        {"id": f"glm/{DEFAULT_MODELS['glm']}", "object": "model", "owned_by": "glm"},
        {"id": "paid/claude", "object": "model", "owned_by": "paid"},
        {"id": "paid/codex", "object": "model", "owned_by": "paid"},
    ]
    for mid in list_openrouter_free(keys):
        models.append({"id": f"openrouter/{mid}", "object": "model", "owned_by": "openrouter"})
    return models
