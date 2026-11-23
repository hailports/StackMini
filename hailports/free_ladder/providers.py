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
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # Timeouts / refused connections must fall through to the next rung, not 500 the request.
        reason = getattr(e, "reason", e)
        msg = str(reason)[:200]
        code = 504 if "timed out" in msg.lower() or isinstance(reason, TimeoutError) else 503
        return code, {"error": {"message": f"timeout: {msg}" if code == 504 else f"unreachable: {msg}"}}, {}


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
    if provider == "claude":
        return "personal-claude"
    if provider == "codex":
        return "personal-codex"
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
    timeout: float | None = None,
) -> tuple[int, dict[str, Any], dict[str, str], dict[str, Any]]:
    """Non-stream chat completion. Returns status, body, headers, meta."""
    keys = keys or load_keys()
    meta: dict[str, Any] = {"provider": provider, "model": model, "stream": False}
    t0 = time.time()

    if provider in {"claude", "codex"}:
        return _subscription_cli(provider, payload, meta, t0, timeout=timeout)

    if provider == "openrouter":
        if not is_openrouter_free(model, keys):
            return 403, {"error": {"message": f"refused non-free openrouter model: {model}"}}, {}, {
                **meta,
                "outcome": "refused_paid_openrouter",
                "latency_ms": int((time.time() - t0) * 1000),
            }

    if provider == "glm":
        # Honor Coding Plan 5h/weekly windows via existing ledger (fail-open on import error).
        try:
            from core.glm_coding_plan import admit as glm_admit

            est = max(64, int(_estimate_prompt_tokens(payload)) + int(payload.get("max_tokens") or 512))
            ok, why, _cost = glm_admit("rescue", est, model=model)
            if not ok:
                return 429, {"error": {"message": f"glm_admit:{why}"}}, {"retry-after": "60"}, {
                    **meta,
                    "outcome": f"glm_quota:{why}",
                    "latency_ms": int((time.time() - t0) * 1000),
                }
        except Exception as exc:
            meta["glm_admit_err"] = str(exc)[:120]

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
    timeout = timeout or TIMEOUTS.get(provider, 60.0)
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
    tokens = _usage_tokens(data, body) if code < 400 else 0
    meta.update(
        {
            "latency_ms": latency,
            "tokens": tokens,
            "outcome": "ok" if code < 400 and text else ("http_error" if code >= 400 else "empty"),
            "retry_after": out_h.get("retry-after") or out_h.get("x-ratelimit-reset-requests"),
            "served_model": out_h.get("x-plugsky-served-model") or (data.get("model") if isinstance(data, dict) else None),
            "ratelimit": {
                k: out_h.get(k)
                for k in (
                    "x-ratelimit-limit-requests",
                    "x-ratelimit-remaining-requests",
                    "x-ratelimit-limit-tokens",
                    "x-ratelimit-remaining-tokens",
                )
                if out_h.get(k)
            }
            or None,
        }
    )
    if provider == "plugsky":
        served = (out_h.get("x-plugsky-served-model") or "").lower()
        if any(x in served for x in ("lite", "micro", "gpu-27b")):
            meta["plugsky_alert"] = f"weak_engine:{served}"
            _alert_plugsky_weak(served)
    if provider == "glm" and code < 400 and text:
        _glm_record_spend(tokens or max(1, len(text) // 4), model)
    return code, data, out_h, meta


def _estimate_prompt_tokens(payload: dict[str, Any]) -> int:
    n = 0
    for m in payload.get("messages") or []:
        n += max(1, len(_msg_content(m)) // 4)
    return n


def _glm_record_spend(tokens: int, model: str) -> None:
    try:
        from core.glm_coding_plan import credits_for, LEDGER
        import time as _t

        row = {
            "ts": _t.time(),
            "lane": "rescue",
            "model": model,
            "tokens": int(tokens),
            "credits": int(credits_for(int(tokens), model)),
            "source": "free_ladder",
        }
        Path(LEDGER).parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def _alert_plugsky_weak(served: str) -> None:
    """Plan lapse signal — Plugsky paid should never serve lite/micro/gpu-27b."""
    try:
        from core.alert_gateway import notify

        notify(
            "free-ladder",
            "plugsky weak engine",
            f"requested plugsky-ultra but x-plugsky-served-model={served} (plan may have lapsed)",
            issue_key="plugsky-weak-engine",
            level="warn",
        )
    except Exception:
        pass


def local_busy(probe_timeout: float = 1.0) -> bool:
    """True when every local slot is already generating (request would queue).

    A probe that does not answer within probe_timeout also counts as busy: a hung
    or saturated local server is exactly the case we must not wait on.
    """
    root = PROVIDERS["local"]["base"].rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        req = urllib.request.Request(root + "/slots", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=probe_timeout) as resp:
            slots = json.loads(resp.read().decode("utf-8", "replace") or "[]")
    except urllib.error.HTTPError:
        return False  # no /slots endpoint: unknown, let the rung timeout decide
    except Exception:
        return True
    if not isinstance(slots, list) or not slots:
        return False
    return all(isinstance(s, dict) and s.get("is_processing") for s in slots)


def _subscription_cli(
    provider: str,
    payload: dict[str, Any],
    meta: dict[str, Any],
    t0: float,
    timeout: float | None = None,
) -> tuple[int, dict[str, Any], dict[str, str], dict[str, Any]]:
    """Personal Claude or personal Codex only — do not change how they authenticate."""
    safe = redact_payload(payload)
    msgs = safe.get("messages") or []
    prompt_parts = []
    for m in msgs:
        role = m.get("role", "user")
        prompt_parts.append(f"{role}: {_msg_content(m)}")
    prompt = "\n\n".join(prompt_parts)[:8000]
    if provider == "claude":
        try:
            env = os.environ.copy()
            # never route Claude Code through GLM override for ladder calls
            env.pop("ANTHROPIC_BASE_URL", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            proc = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "text"],
                capture_output=True,
                text=True,
                timeout=timeout or TIMEOUTS["claude"],
                env=env,
            )
            if proc.returncode == 0 and (proc.stdout or "").strip():
                text = proc.stdout.strip()
                body = {
                    "id": "sub-claude",
                    "object": "chat.completion",
                    "model": "personal-claude",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
                meta.update(
                    {
                        "latency_ms": int((time.time() - t0) * 1000),
                        "outcome": "ok",
                        "model": "personal-claude",
                        "tokens": max(1, len(text) // 4),
                    }
                )
                return 200, body, {}, meta
            meta["claude_err"] = (proc.stderr or proc.stdout or "")[:120]
        except Exception as exc:
            meta["claude_err"] = str(exc)[:120]
        meta.update({"latency_ms": int((time.time() - t0) * 1000), "outcome": "sub_fail"})
        return 502, {"error": {"message": "personal claude failed"}}, {}, meta

    # personal Codex only (never corp)
    try:
        home = Path.home()
        personal = home / ".codex-personal"
        env = os.environ.copy()
        env["CODEX_HOME"] = str(personal if personal.is_dir() else home / ".codex")
        # refuse corp home
        if "codex-corp" in env["CODEX_HOME"] or env.get("CODEX_ACCOUNT") == "corp":
            meta.update({"latency_ms": int((time.time() - t0) * 1000), "outcome": "refused_corp_codex"})
            return 403, {"error": {"message": "ladder never uses corp Codex"}}, {}, meta
        proc = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "-q", prompt[:6000]],
            capture_output=True,
            text=True,
            timeout=timeout or TIMEOUTS["codex"],
            env=env,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            text = proc.stdout.strip()
            body = {
                "id": "sub-codex",
                "object": "chat.completion",
                "model": "personal-codex",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            meta.update(
                {
                    "latency_ms": int((time.time() - t0) * 1000),
                    "outcome": "ok",
                    "model": "personal-codex",
                    "tokens": max(1, len(text) // 4),
                }
            )
            return 200, body, {}, meta
        meta["codex_err"] = (proc.stderr or proc.stdout or "")[:120]
    except Exception as exc:
        meta["codex_err"] = str(exc)[:120]
    meta.update({"latency_ms": int((time.time() - t0) * 1000), "outcome": "sub_fail"})
    return 502, {"error": {"message": "personal codex failed"}}, {}, meta


def stream_chat_completions(
    provider: str,
    payload: dict[str, Any],
    *,
    model: str,
    keys: dict[str, str] | None = None,
) -> Iterator[bytes]:
    """Yield SSE bytes. Falls back to single-chunk if provider stream fails."""
    keys = keys or load_keys()
    if provider == "openrouter" and not is_openrouter_free(model, keys):
        err = json.dumps({"error": {"message": f"refused non-free openrouter model: {model}"}})
        yield f"data: {err}\n\n".encode()
        yield b"data: [DONE]\n\n"
        return

    base = (PROVIDERS.get(provider) or {}).get("base")
    if not base or provider in {"claude", "codex"}:
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
        {"id": "claude/personal", "object": "model", "owned_by": "claude"},
        {"id": "codex/personal", "object": "model", "owned_by": "codex"},
    ]
    for mid in list_openrouter_free(keys):
        # mid is already an OpenRouter id (e.g. google/gemma…:free or openrouter/free)
        oid = mid if mid.startswith("openrouter/") else f"openrouter/{mid}"
        models.append({"id": oid, "object": "model", "owned_by": "openrouter"})
    return models
