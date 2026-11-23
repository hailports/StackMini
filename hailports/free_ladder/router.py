"""Ladder routing: classify → best free rung → climb on quota/error/quality fail."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from .classify import classify
from .config import (
    CALL_LOG,
    CLASS_PREFERENCE,
    DAILY_SUMMARY,
    DEFAULT_MODELS,
    PROVIDERS,
    SUBSCRIPTION_PEERS,
    SUB_TASK_BIAS,
    ensure_dirs,
    key_for,
    load_keys,
)
from .providers import (
    chat_completions,
    is_openrouter_free,
    list_openrouter_free,
    model_for,
    stream_chat_completions,
)
from .quality import extract_text, quality_ok
from .quota import QuotaStore

# Public channel copy must NOT use the local model (HARD).
_PUBLIC_COPY = ("hailports", "channel_b", "ima furad", "public_copy", "tweet", "caption")


def _log_call(row: dict[str, Any]) -> None:
    ensure_dirs()
    row = dict(row)
    row.setdefault("ts", datetime.now(tz=timezone.utc).isoformat())
    try:
        with CALL_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    if row.get("provider") in SUBSCRIPTION_PEERS and row.get("outcome") == "ok":
        _note_paid(row)  # flat-rate sub used (still better than metered)
    elif row.get("outcome") == "ok" and row.get("provider") not in SUBSCRIPTION_PEERS:
        _note_avoided(row)


def _note_avoided(row: dict[str, Any]) -> None:
    ensure_dirs()
    day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        with DAILY_SUMMARY.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": row.get("ts"),
                        "day": day,
                        "event": "paid_avoided",
                        "provider": row.get("provider"),
                        "model": row.get("model"),
                        "task": row.get("task"),
                        "tokens": row.get("tokens"),
                    }
                )
                + "\n"
            )
    except OSError:
        pass


def _note_paid(row: dict[str, Any]) -> None:
    ensure_dirs()
    day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        with DAILY_SUMMARY.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": row.get("ts"),
                        "day": day,
                        "event": "paid_used",
                        "provider": row.get("provider"),
                        "model": row.get("model"),
                        "task": row.get("task"),
                    }
                )
                + "\n"
            )
    except OSError:
        pass


def parse_model_pin(model: str | None) -> tuple[str | None, str | None]:
    """Return (provider, model_id) for pins like cerebras/qwen-3.8-27b; auto -> (None, None)."""
    m = (model or "auto").strip()
    if not m or m in {"auto", "free-ladder", "ladder"}:
        return None, None
    if "/" in m:
        prov, _, rest = m.partition("/")
        prov = prov.lower().strip()
        rest = rest.strip()
        if prov in PROVIDERS:
            return prov, rest or None
        # openrouter ids contain slashes: openrouter/meta/llama...
        if m.startswith("openrouter/"):
            return "openrouter", m[len("openrouter/") :]
    # bare provider name
    if m.lower() in PROVIDERS:
        return m.lower(), None
    return None, m


def _skip_local(payload: dict[str, Any], headers: dict[str, str] | None = None) -> bool:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if headers.get("x-free-ladder-skip-local", "").strip() in {"1", "true", "yes"}:
        return True
    if headers.get("x-public_copy", "").strip() in {"1", "true", "yes"}:
        return True
    blob = json.dumps(payload.get("messages") or [])[:4000].lower()
    return any(s in blob for s in _PUBLIC_COPY)


def build_plan(
    payload: dict[str, Any],
    *,
    keys: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    quota: QuotaStore | None = None,
) -> dict[str, Any]:
    keys = keys or load_keys()
    info = classify(payload)
    pin_prov, pin_model = parse_model_pin(payload.get("model"))
    pref = list(CLASS_PREFERENCE.get(info["task"]) or CLASS_PREFERENCE["chat"])
    if _skip_local(payload, headers):
        pref = [p for p in pref if p != "local"]
    # drop providers without keys (except local + subscription CLIs)
    usable = []
    for p in pref:
        if p in {"local", "claude", "codex"}:
            usable.append(p)
        elif key_for(p, keys):
            usable.append(p)
    # Reorder subscription peers by task bias + remaining quota (sideways climb).
    usable = _order_subscription_peers(usable, info["task"], quota)
    if pin_prov:
        # pinned: try that provider first (and only climb after)
        order = [pin_prov] + [p for p in usable if p != pin_prov]
    else:
        order = usable
    return {
        "task": info["task"],
        "tokens_est": info["tokens"],
        "vision": info["vision"],
        "tools": info["tools"],
        "order": order,
        "pin_provider": pin_prov,
        "pin_model": pin_model,
        "info": info,
    }


def _sub_remaining(provider: str, quota: QuotaStore | None) -> float:
    """Higher = more remaining capacity. Used to pick among glm/claude/codex."""
    score = 0.0
    if quota is not None:
        try:
            ok, why = quota.allow(provider)
            if not ok:
                return -1.0
            # prefer peers with fresher headroom (no backoff)
            score += 100.0
        except Exception:
            score += 50.0
    if provider == "glm":
        try:
            from core.glm_coding_plan import status as glm_status

            st = glm_status()
            week = st.get("week") or {}
            left = max(0, int(week.get("cap") or 0) - int(week.get("used") or 0))
            score += float(left)
            burst = st.get("burst_5h") or {}
            score += max(0, int(burst.get("cap") or 0) - int(burst.get("used") or 0)) * 0.5
        except Exception:
            score += 10.0
    elif provider == "claude":
        score += 80.0  # flat Max — prefer when glm is tight
    elif provider == "codex":
        score += 70.0
    return score


def _order_subscription_peers(
    order: list[str],
    task: str,
    quota: QuotaStore | None,
) -> list[str]:
    """Keep free/premium prefix; rewrite glm/claude/codex block by fit + quota."""
    prefix = [p for p in order if p not in SUBSCRIPTION_PEERS]
    present = [p for p in order if p in SUBSCRIPTION_PEERS]
    if not present:
        return order
    bias = list(SUB_TASK_BIAS.get(task) or SUBSCRIPTION_PEERS)
    # sort present by (-remaining, bias index)
    def key(p: str) -> tuple:
        return (-_sub_remaining(p, quota), bias.index(p) if p in bias else 99)

    present_sorted = sorted(present, key=key)
    # drop peers with negative remaining (hard blocked)
    present_sorted = [p for p in present_sorted if _sub_remaining(p, quota) >= 0]
    return prefix + present_sorted


def _retry_after_seconds(headers: dict[str, str], default: float = 30.0) -> float:
    ra = headers.get("retry-after") or headers.get("Retry-After")
    if not ra:
        return default
    try:
        return max(1.0, float(ra))
    except ValueError:
        return default


def route_chat(
    payload: dict[str, Any],
    *,
    keys: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    quota: QuotaStore | None = None,
    allow_paid: bool = True,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """Execute ladder. Returns http status, response body, routing meta."""
    keys = keys or load_keys()
    quota = quota or QuotaStore()
    plan = build_plan(payload, keys=keys, headers=headers, quota=quota)
    attempts: list[dict[str, Any]] = []
    last_err: dict[str, Any] = {"error": {"message": "all rungs failed"}}

    for provider in plan["order"]:
        if provider in SUBSCRIPTION_PEERS and not allow_paid:
            continue
        model = model_for(
            provider,
            plan["task"],
            plan["pin_model"] if plan["pin_provider"] == provider else None,
        )
        # gemini pro has its own 50/day bucket when selected
        if provider == "gemini" and model == DEFAULT_MODELS["gemini_pro"]:
            q_provider = "gemini_pro"
        else:
            q_provider = provider
        ok, why = quota.allow(q_provider)
        if not ok:
            attempts.append({"provider": provider, "model": model, "outcome": f"quota:{why}", "rung": PROVIDERS[provider]["rung"]})
            continue

        if provider == "openrouter":
            if plan["pin_model"] and plan["pin_provider"] == "openrouter":
                if not is_openrouter_free(plan["pin_model"], keys):
                    attempts.append({"provider": provider, "model": plan["pin_model"], "outcome": "refused_paid_openrouter", "rung": 4})
                    continue
                model = plan["pin_model"]
            else:
                free = list_openrouter_free(keys)
                if not free:
                    attempts.append({"provider": provider, "model": model, "outcome": "no_free_models", "rung": 4})
                    continue
                if not is_openrouter_free(model, keys):
                    model = free[0]

        # test hooks: FREE_LADDER_FORCE_FAIL=provider:429|timeout
        force = (os.environ.get("FREE_LADDER_FORCE_FAIL") or "").strip()
        if force:
            fp, _, fcode = force.partition(":")
            if fp == provider or fp == "*":
                fake = int(fcode) if fcode.isdigit() else 429
                row = {
                    "provider": provider,
                    "model": model,
                    "tokens": 0,
                    "latency_ms": 1,
                    "outcome": f"force_fail:{fake}",
                    "rung": PROVIDERS[provider]["rung"],
                    "task": plan["task"],
                    "http": fake,
                    "attempt": 0,
                }
                if fake == 429:
                    quota.set_backoff(q_provider, 2.0, "force_429")
                elif fake in {408, 504}:
                    quota.set_backoff(q_provider, 2.0, "force_timeout")
                _log_call(row)
                attempts.append(row)
                continue

        # one retry max per rung
        for attempt in range(2):
            code, body, out_h, meta = chat_completions(
                provider, payload, model=model, keys=keys
            )
            row = {
                "provider": provider,
                "model": meta.get("model") or model,
                "tokens": meta.get("tokens") or 0,
                "latency_ms": meta.get("latency_ms"),
                "outcome": meta.get("outcome"),
                "rung": PROVIDERS[provider]["rung"],
                "task": plan["task"],
                "http": code,
                "attempt": attempt,
                "served_model": meta.get("served_model"),
                "plugsky_alert": meta.get("plugsky_alert"),
            }
            if code == 429:
                secs = _retry_after_seconds(out_h, 30.0 * (attempt + 1))
                quota.set_backoff(q_provider, secs, "429")
                row["outcome"] = "429"
                _log_call(row)
                attempts.append(row)
                if attempt == 0:
                    time.sleep(min(secs, 5.0))
                    continue
                break
            if code >= 400 or meta.get("outcome") in {"empty", "http_error", "no_key", "paid_fail", "sub_fail"}:
                row["outcome"] = meta.get("outcome") or f"http_{code}"
                _log_call(row)
                attempts.append(row)
                last_err = body if isinstance(body, dict) else {"error": {"message": str(body)}}
                # timeout-ish
                if code in {408, 504} or "timeout" in json.dumps(body).lower():
                    quota.set_backoff(q_provider, 15.0 * (attempt + 1), "timeout")
                break

            text = extract_text(body)
            q_ok, q_why = quality_ok(text, task=plan["task"])
            if not q_ok:
                row["outcome"] = f"quality:{q_why}"
                _log_call(row)
                attempts.append(row)
                if attempt == 0:
                    continue
                break

            # success
            quota.record(q_provider, int(meta.get("tokens") or 0))
            quota.clear_backoff(q_provider)
            row["outcome"] = "ok"
            _log_call(row)
            attempts.append(row)
            meta_out = {
                "task": plan["task"],
                "provider": provider,
                "model": meta.get("model") or model,
                "rung": PROVIDERS[provider]["rung"],
                "attempts": attempts,
                "tokens_est": plan["tokens_est"],
                "paid_avoided": provider not in SUBSCRIPTION_PEERS,
                "subscription_used": provider in SUBSCRIPTION_PEERS,
            }
            # annotate response
            if isinstance(body, dict):
                body = dict(body)
                body.setdefault("free_ladder", meta_out)
            return code, body, meta_out

    meta_out = {"task": plan["task"], "attempts": attempts, "paid_avoided": False}
    return 502, last_err, meta_out


def route_stream(
    payload: dict[str, Any],
    *,
    keys: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    quota: QuotaStore | None = None,
    allow_paid: bool = True,
):
    """Pick first usable rung and stream; climb on immediate failure before streaming starts."""
    keys = keys or load_keys()
    quota = quota or QuotaStore()
    plan = build_plan(payload, keys=keys, headers=headers, quota=quota)

    for provider in plan["order"]:
        if provider in SUBSCRIPTION_PEERS and not allow_paid:
            continue
        model = model_for(provider, plan["task"], plan["pin_model"] if plan["pin_provider"] == provider else None)
        q_provider = "gemini_pro" if (provider == "gemini" and model == DEFAULT_MODELS["gemini_pro"]) else provider
        ok, why = quota.allow(q_provider)
        if not ok:
            continue
        if provider == "openrouter":
            if plan["pin_model"] and plan["pin_provider"] == "openrouter" and not is_openrouter_free(plan["pin_model"], keys):
                continue
            if not is_openrouter_free(model, keys):
                free = list_openrouter_free(keys)
                if not free:
                    continue
                model = free[0]
        # probe non-stream for local/paid? For stream providers, start stream.
        # On open failure, climb.
        try:
            gen = stream_chat_completions(provider, payload, model=model, keys=keys)
            first = next(gen)
        except Exception:
            continue
        # if first chunk is an error JSON, climb
        if b'"error"' in first[:200] and b"chat.completion" not in first[:200]:
            continue
        quota.record(q_provider, plan["tokens_est"])
        _log_call(
            {
                "provider": provider,
                "model": model,
                "tokens": plan["tokens_est"],
                "latency_ms": None,
                "outcome": "ok_stream",
                "rung": PROVIDERS[provider]["rung"],
                "task": plan["task"],
            }
        )

        def _chain(first_chunk=first, rest=gen):
            yield first_chunk
            for chunk in rest:
                yield chunk

        return _chain(), {"provider": provider, "model": model, "rung": PROVIDERS[provider]["rung"], "task": plan["task"]}

    def _fail():
        yield b'data: {"error":{"message":"all rungs failed"}}\n\n'
        yield b"data: [DONE]\n\n"

    return _fail(), {"provider": None, "model": None, "rung": None, "task": plan["task"]}
