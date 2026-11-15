"""Coop patterns for a local model and a premium model.

Fail-open: helpers return false or empty on bad input. They do not do I/O
except the answer cache, and a cache error is a miss. No network here.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

LONG_CHARS = 1500
COMPRESS_RATIO = 0.75
CACHE_LIMIT = 200
CACHE_TTL_S = 7 * 24 * 3600

_DIG = re.compile(
    r"\b(?:summar\w*|classif\w*|triage|extract\w*|digest|checklist|health|"
    r"status|how many|which model|current state|how fresh|tok/s|"
    r"tokens per second|markdown table|format|bullet|outline|"
    r"what (?:is|are|should|do you know))\b",
    re.I,
)
_ACTION = re.compile(
    r"\b(?:i emailed|i sent|i deployed|i changed production|dear customer|"
    r"subject:\s)\b",
    re.I,
)
_DEF = re.compile(r"(?m)^\s*def\s+\w+\(")
_UNSURE = ("i don't know", "i do not know", "not sure", "cannot tell", "low confidence", "insufficient")
_SECRET = re.compile(r"(?:tskey-|sk-|ghp_|gho_|xox[baprs]-|bearer\s+[a-z0-9])", re.I)


def coop_enabled(cfg: dict | None = None) -> bool:
    flag = os.environ.get("COOP_MODE", "").strip().lower()
    if flag in {"0", "off", "no", "false"}:
        return False
    if flag in {"1", "on", "yes", "true"}:
        return True
    if cfg and cfg.get("coop_mode") is False:
        return False
    return True


def estimate_tokens(text: str) -> int:
    """Documented estimator: one token per four characters, minimum 1.

    Use a provider or server count when the call returns one. This estimate
    is only for receipts when those counts are missing.
    """
    n = len(text or "")
    return max(1, (n + 3) // 4)


def coerce_usage(usage: dict | None, timings: dict | None) -> dict:
    out = dict(usage or {})
    timings = timings or {}
    if not out.get("prompt_tokens") and timings.get("prompt_n") not in (None, ""):
        try:
            out["prompt_tokens"] = int(timings.get("prompt_n") or 0)
            out["completion_tokens"] = int(timings.get("predicted_n") or 0)
        except (TypeError, ValueError):
            return out
        details = dict(out.get("prompt_tokens_details") or {})
        try:
            details["cached_tokens"] = int(timings.get("cache_n") or 0)
        except (TypeError, ValueError):
            details["cached_tokens"] = 0
        out["prompt_tokens_details"] = details
    return out


def plain_answer(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and str(obj.get("answer") or "").strip():
            return str(obj.get("answer") or "").strip()
    return text.strip()


def dig_like(task: str) -> bool:
    return bool(_DIG.search(task or ""))


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d{2,}", text or ""))


def answer_ok(task: str, answer: str) -> bool:
    """Self-check. Rejects empty text, action claims, and dropped numbers."""
    t = (answer or "").strip()
    if not t or _SECRET.search(t):
        return False
    low = t.lower()
    if any(p in low for p in _UNSURE) and len(t) < 80:
        return False
    if _ACTION.search(low) or _DEF.search(t):
        return False
    nums = _numbers(task)
    if len(nums) >= 3:
        kept = sum(1 for n in nums if n in t)
        need = 4 if len(nums) > 8 else max(2, (len(nums) + 1) // 2)
        return kept >= need
    if nums and not any(n in t for n in nums):
        return False
    if re.search(r"\d", t) and re.search(r"\d", task or ""):
        return True
    return len(t) >= 8


def allow_local_finish(task: str, answer: str, jev_action: str | None) -> bool:
    """Accept a local finish of an unsure dig.

    A bad Jev score never accepts. Jev "stop" accepts when the self-check
    passes. When Jev is down or mid, only a number-heavy self-check accepts.
    """
    if jev_action == "escalate":
        return False
    if not answer_ok(task, answer):
        return False
    if jev_action == "stop":
        return True
    return len(_numbers(task)) >= 3


def compression_ok(task: str, summary: str, *, limit: int = LONG_CHARS) -> bool:
    task = task or ""
    summary = (summary or "").strip()
    if len(task) < limit or not summary or _SECRET.search(summary):
        return False
    if len(summary) >= int(len(task) * COMPRESS_RATIO):
        return False
    if _ACTION.search(summary) or _DEF.search(summary):
        return False
    nums = _numbers(task)
    if not nums:
        return True
    kept = sum(1 for n in nums if n in summary)
    need = len(nums) if len(nums) <= 12 else max(4, int(len(nums) * 0.8))
    return kept >= need


def compress_prompt(task: str) -> str:
    body = (task or "")[:12000]
    return (
        "Compress the context into at most 8 bullets. Keep every number. "
        "Do not add facts. Do not draft a message or code.\n\n" + body
    )


def long_chars(cfg: dict | None) -> int:
    try:
        return int((cfg or {}).get("coop_long_chars") or LONG_CHARS)
    except (TypeError, ValueError):
        return LONG_CHARS


def cache_path(cfg: dict | None) -> Path | None:
    if not coop_enabled(cfg):
        return None
    raw = str((cfg or {}).get("answer_cache_path") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _cache_load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def cache_lookup(cfg: dict | None, key: str) -> dict | None:
    path = cache_path(cfg)
    if path is None or not key or not path.is_file():
        return None
    row = _cache_load(path).get(key)
    if not isinstance(row, dict):
        return None
    answer = str(row.get("answer") or "").strip()
    if not answer or _SECRET.search(answer):
        return None
    try:
        ts = float(row.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if ts and (time.time() - ts) > CACHE_TTL_S:
        return None
    return row


def cache_store(cfg: dict | None, key: str, answer: str, saved_in: int, saved_out: int) -> None:
    path = cache_path(cfg)
    if path is None or not key:
        return
    answer = (answer or "").strip()
    if not answer or _SECRET.search(answer):
        return
    data = _cache_load(path) if path.is_file() else {}
    now = time.time()
    fresh = {}
    for item_key, row in data.items():
        if not isinstance(row, dict):
            continue
        try:
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts and (now - ts) <= CACHE_TTL_S:
            fresh[item_key] = row
    fresh[key] = {
        "answer": answer[:4000],
        "ts": now,
        "premium_in_saved": int(saved_in),
        "premium_out_saved": int(saved_out),
    }
    if len(fresh) > CACHE_LIMIT:
        ordered = sorted(fresh.items(), key=lambda kv: float(kv[1].get("ts") or 0))
        fresh = dict(ordered[-CACHE_LIMIT:])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fresh, sort_keys=True), encoding="utf-8")
    except OSError:
        return


def annotate_complete(result: dict, task: str, pattern: str) -> dict:
    answer = str(result.get("answer") or "")
    usage = result.get("usage") or {}
    out_tok = usage.get("completion_tokens")
    try:
        out_tok = int(out_tok) if out_tok else estimate_tokens(answer)
    except (TypeError, ValueError):
        out_tok = estimate_tokens(answer)
    result["coop_pattern"] = pattern
    result["premium_in_saved"] = estimate_tokens(task)
    result["premium_out_saved"] = out_tok
    return result
