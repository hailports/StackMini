#!/usr/bin/env python3
"""Jev (TypeSafe System One) classifier client. Stdlib only.

classify(state, questions, timeout=8) -> {"<qid>": answer_dict} or None.
FAIL-OPEN: any error/timeout/missing key returns None; callers keep their
existing behavior. Never raises. Never logs the key or the state text.

Key lookup (first match):
  env TYPESAFE_API_KEY, env JEV_API_KEY,
  then /home/operator/.config/jev/typesafe_api_key,
  then ~/claude-stack/secrets/typesafe_api_key (Mini layout).

Question types: noul (0..1, 0.5=unsure), choice (criteria dict >=2),
score (criteria list >=2).
Kill switch: JEV_DISABLE=1.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
BOX_KEY = Path("/home/operator/.config/jev/typesafe_api_key")
STACK = Path(os.environ.get("CLAUDE_STACK_HOME", str(HOME / "claude-stack")))
STACK_KEY = STACK / "secrets" / "typesafe_api_key"
XDG_KEY = HOME / ".config" / "jev" / "typesafe_api_key"
LOG = Path(os.environ.get("JEV_LOG", str(HOME / ".local" / "state" / "jev" / "jev.jsonl")))
BASE = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")
MAX_STATE = 12000


def _key() -> str:
    for name in ("TYPESAFE_API_KEY", "JEV_API_KEY"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    for path in (BOX_KEY, XDG_KEY, STACK_KEY):
        try:
            v = path.read_text(encoding="utf-8").strip()
            if v:
                return v
        except Exception:
            continue
    return ""


def _log(rec: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def classify(state, questions: dict, timeout: float = 8.0, caller: str = "", model: str | None = None):
    if os.environ.get("JEV_DISABLE", "") == "1" or not questions:
        return None
    key = _key()
    if not key:
        _log({"ts": time.time(), "caller": caller, "ok": False, "err": "no_key"})
        return None
    if isinstance(state, str) and len(state) > MAX_STATE:
        state = state[:MAX_STATE]
    body = json.dumps({"model": model or MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/systemone", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "claude-stack-jev/1"})
    t0 = time.time()
    rec = {"ts": t0, "caller": caller, "q": sorted(questions)}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            raise ValueError("no_answers")
        rec.update(ok=True, ms=int((time.time() - t0) * 1000), usage=data.get("usage"))
        _log(rec)
        return answers
    except Exception as e:  # fail-open
        rec.update(ok=False, ms=int((time.time() - t0) * 1000), err=f"{type(e).__name__}:{str(e)[:120]}")
        _log(rec)
        return None


def noul(answers, qid: str):
    """Float 0..1 for a noul answer, or None."""
    try:
        v = answers[qid]
        v = v.get("noul") if isinstance(v, dict) else v
        return float(v)
    except Exception:
        return None


def choice(answers, qid: str):
    """(choice, confidence) for a choice answer, or (None, 0.0)."""
    try:
        a = answers[qid]
        return a.get("choice"), float(a.get("confidence") or 0.0)
    except Exception:
        return None, 0.0


def score(answers, qid: str):
    """(score, confidence) for a score answer, or (None, 0.0)."""
    try:
        a = answers[qid]
        if not isinstance(a, dict):
            return float(a), 0.0
        s = a.get("score")
        if s is None:
            return None, 0.0
        return float(s), float(a.get("confidence") or 0.0)
    except Exception:
        return None, 0.0


def _parse_question_flag(raw: str) -> tuple[str, dict]:
    """NAME=TYPE:INSTRUCTIONS  or  NAME:TYPE:INSTRUCTIONS."""
    if "=" in raw.split(":", 1)[0]:
        name, rest = raw.split("=", 1)
    else:
        name, rest = raw.split(":", 1)
    name = name.strip()
    if ":" not in rest:
        raise ValueError("question needs TYPE:INSTRUCTIONS")
    qtype, instr = rest.split(":", 1)
    qtype = qtype.strip().lower()
    instr = instr.strip()
    if qtype not in ("noul", "choice", "score"):
        raise ValueError(f"unknown question type {qtype}")
    spec: dict = {"type": qtype, "instructions": instr}
    return name, spec


def _load_payload(argv: list[str]) -> dict:
    import argparse
    p = argparse.ArgumentParser(prog="jev", add_help=True)
    p.add_argument("--state", help="state text (otherwise JSON on stdin)")
    p.add_argument("--questions", help="questions object as JSON")
    p.add_argument("--question", action="append", default=[],
                   help='NAME=TYPE:INSTRUCTIONS (repeatable). TYPE is noul, choice, or score')
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--caller", default="cli")
    args, _unknown = p.parse_known_args(argv)

    payload = {}
    if args.state is None and args.questions is None and not args.question and not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    if args.state is not None:
        payload["state"] = args.state
    if args.questions:
        payload["questions"] = json.loads(args.questions)
    if args.question:
        q = dict(payload.get("questions") or {})
        for item in args.question:
            name, spec = _parse_question_flag(item)
            q[name] = spec
        payload["questions"] = q
    payload.setdefault("timeout", args.timeout)
    payload.setdefault("caller", args.caller)
    return payload


def main(argv: list[str] | None = None) -> int:
    """CLI: JSON in, answers JSON out. Fail-open: print null, exit 0."""
    try:
        payload = _load_payload(sys.argv[1:] if argv is None else argv)
        state = payload.get("state", "")
        questions = payload.get("questions") or {}
        timeout = float(payload.get("timeout") or 8.0)
        caller = str(payload.get("caller") or "cli")
        if not isinstance(questions, dict) or not questions:
            print("null")
            return 0
        ans = classify(state, questions, timeout=timeout, caller=caller)
        print(json.dumps(ans) if ans is not None else "null")
        return 0
    except SystemExit as e:
        # argparse --help
        code = int(e.code or 0)
        if code != 0:
            print("null")
        return 0
    except Exception:
        print("null")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
