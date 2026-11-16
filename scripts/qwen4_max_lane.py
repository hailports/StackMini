#!/usr/bin/env python3
"""Capped hosted Qwen4-Max lane for personal tasks after the local model.

Work and employer prompts are blocked. Thinking is off by default. This lane
does not enable paid inference on the local chat door. The ledger never stores
prompt text or API keys.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("HAILPORTS_ROOT") or (Path.home() / "claude-stack"))
STATE_DIR = ROOT / "data" / "runtime" / "qwen4_max_lane"
LEDGER = STATE_DIR / "spend.jsonl"
CT = ZoneInfo("America/Chicago")

MODEL = "qwen/qwen4-max"
BASE = "https://openrouter.ai/api/v1/chat/completions"
DAILY_CAP_USD = 1.00
PER_CALL_CAP_USD = 0.25
KEY_FLOOR_USD = 0.50
INPUT_PER_M = 1.50
OUTPUT_PER_M = 4.50
DEFAULT_MAX_TOKENS = 2048
DEFAULT_EFFORT = "none"

# Generic work/employer block. Do not put workplace names or ticket prefixes here.
_WORK_PATTERNS = [
    re.compile(r"\bemployer\b", re.I),
    re.compile(r"\bwork[- ](?:ticket|index|pack|notes|item)\b", re.I),
    re.compile(r"\binternal[- ](?:ticket|work item)\b", re.I),
    re.compile(r"\bcustomer[- ](?:ticket|account)\b", re.I),
]


def estimate_usd(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens / 1_000_000) * INPUT_PER_M + (completion_tokens / 1_000_000) * OUTPUT_PER_M


def work_or_employer(text: str) -> tuple[bool, str]:
    blob = text or ""
    for pat in _WORK_PATTERNS:
        if pat.search(blob):
            return True, "blocked_marker"
    return False, "ok"


def _day() -> str:
    return datetime.now(CT).date().isoformat()


def spent_today() -> float:
    day = _day()
    total = 0.0
    try:
        for line in LEDGER.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if str(row.get("ts") or "").startswith(day):
                total += float(row.get("cost") or 0)
    except FileNotFoundError:
        return 0.0
    return total


def _key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def key_remaining() -> float | None:
    try:
        from core import openrouter_budget as ob  # type: ignore

        rem = ob.remaining()
        if rem is not None:
            return float(rem)
    except Exception:
        pass
    k = _key()
    if not k:
        return None
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": "Bearer " + k, "User-Agent": "qwen4-max-lane/1"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode()).get("data") or {}
        if data.get("limit_remaining") is not None:
            return float(data["limit_remaining"])
    except Exception:
        return None
    return None


def allow(prompt: str, *, est_cost: float | None = None, max_tokens: int = DEFAULT_MAX_TOKENS) -> tuple[bool, str]:
    blocked, _why = work_or_employer(prompt)
    if blocked:
        return False, "work_or_employer"
    if not _key():
        return False, "nokey"
    today = spent_today()
    if today >= DAILY_CAP_USD:
        return False, "daily_cap"
    if est_cost is None:
        est_cost = estimate_usd(4000, max_tokens)
    if est_cost > PER_CALL_CAP_USD:
        return False, "per_call_cap"
    if today + min(est_cost, PER_CALL_CAP_USD) > DAILY_CAP_USD:
        return False, "daily_cap"
    rem = key_remaining()
    if rem is not None and rem < KEY_FLOOR_USD:
        return False, "key_floor"
    return True, "ok"


def record(usage: dict, *, status: str, reason: str = "") -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cost = usage.get("cost")
    try:
        cost_f = float(cost) if cost is not None else float(
            estimate_usd(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        )
    except (TypeError, ValueError):
        cost_f = 0.0
    row = {
        "ts": datetime.now(CT).isoformat(timespec="seconds"),
        "model": MODEL,
        "status": status,
        "reason": reason,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "cost": round(cost_f, 6),
        "cost_src": "openrouter" if cost is not None else "estimate",
    }
    with LEDGER.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def complete(
    prompt: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: str = DEFAULT_EFFORT,
    temperature: float = 0,
    seed: int | None = 7,
    timeout: int = 180,
) -> dict:
    ok, reason = allow(prompt, max_tokens=max_tokens)
    if not ok:
        return {"ok": False, "reason": reason, "text": ""}
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning": {"effort": effort},
        "usage": {"include": True},
    }
    if seed is not None:
        body["seed"] = seed
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE,
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer " + _key(),
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/qwen4-max-lane",
            "X-Title": "personal-qwen4-max-lane",
        },
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        record({}, status="http_error", reason=f"http_{e.code}")
        return {"ok": False, "reason": f"http_{e.code}", "text": "", "latency_s": time.perf_counter() - t0}
    except Exception as e:
        record({}, status="error", reason=type(e).__name__)
        return {"ok": False, "reason": type(e).__name__, "text": "", "latency_s": time.perf_counter() - t0}

    choice = ((raw.get("choices") or [{}])[0].get("message") or {})
    usage = raw.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    usage_out = {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "cost": usage.get("cost"),
    }
    cost = usage_out["cost"]
    if cost is None:
        cost = estimate_usd(int(usage_out["prompt_tokens"] or 0), int(usage_out["completion_tokens"] or 0))
        usage_out["cost"] = cost
    if float(cost) > PER_CALL_CAP_USD:
        record(usage_out, status="over_per_call_cap", reason="over_per_call_cap")
        return {
            "ok": False,
            "reason": "over_per_call_cap",
            "text": choice.get("content") or "",
            "usage": usage_out,
            "latency_s": time.perf_counter() - t0,
        }
    record(usage_out, status="ok")
    return {
        "ok": True,
        "reason": "ok",
        "text": choice.get("content") or "",
        "usage": usage_out,
        "model": raw.get("model") or MODEL,
        "latency_s": time.perf_counter() - t0,
    }


def main() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Qwen4-Max personal lane. Work and employer prompts are blocked.")
    ap.add_argument("--prompt-file", help="Prompt file path")
    ap.add_argument("--check", action="store_true", help="Check allow() only; do not call the API")
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--effort", default=DEFAULT_EFFORT, help="Reasoning effort; default none (thinking off)")
    args = ap.parse_args()
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text()
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        print("need --prompt-file or stdin", file=sys.stderr)
        return 2
    if args.check:
        ok, reason = allow(prompt, max_tokens=args.max_tokens)
        print(
            json.dumps(
                {
                    "allow": ok,
                    "reason": reason,
                    "spent_today": round(spent_today(), 6),
                    "daily_cap": DAILY_CAP_USD,
                    "per_call_cap": PER_CALL_CAP_USD,
                }
            )
        )
        return 0 if ok else 1
    rec = complete(prompt, max_tokens=args.max_tokens, effort=args.effort)
    print(
        json.dumps(
            {k: rec.get(k) for k in ("ok", "reason", "text", "usage", "model", "latency_s") if k in rec},
            ensure_ascii=False,
        )
    )
    return 0 if rec.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
