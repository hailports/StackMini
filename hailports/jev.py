#!/usr/bin/env python3
"""Optional TypeSafe System One (Jev) classifier. Stdlib only.

classify(state, questions, timeout=8) -> {"<qid>": answer_dict} or None.

FAIL-OPEN: any error, timeout, or missing key returns None. Callers keep
their existing behavior. Never raises. Never logs the key or the state text.

Key lookup (first match): env TYPESAFE_API_KEY, env JEV_API_KEY,
env JEV_KEY_FILE, then ~/.config/jev/typesafe_api_key,
then $CLAUDE_STACK_HOME/secrets/typesafe_api_key.

Question types: noul (0..1, 0.5=unsure), choice (criteria dict >=2),
score (criteria list >=2).
Kill switch: JEV_DISABLE=1.

Local :8080 (non-social only):
  JEV_LOCAL=0 forces paid. Social callers stay on paid (hard rule).
  Strict JSON answers, 2-sample self-consistency, confidence floor;
  disagree / low confidence / malformed / model down -> TypeSafe paid.
  Every attempt logs a row to router-decisions.jsonl for the offload rollup.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
STACK = Path(os.environ.get("CLAUDE_STACK_HOME", str(HOME / "claude-stack")))
STACK_KEY = STACK / "secrets" / "typesafe_api_key"
XDG_KEY = HOME / ".config" / "jev" / "typesafe_api_key"
LOG = Path(os.environ.get("JEV_LOG", str(HOME / ".local" / "state" / "jev" / "jev.jsonl")))
LEGACY_LOG = STACK / "logs" / "jev.jsonl"
BASE = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")
MAX_STATE = 4000
HOST = socket.gethostname().split(".")[0]
MINI_URL = os.environ.get(
    "JEV_LOCAL_URL",
    os.environ.get("MINI_CHAT_URL", "http://127.0.0.1:8080/v1/chat/completions"),
).strip()
MINI_MODEL = os.environ.get("JEV_LOCAL_MODEL", "local/Qwen3.6-35B-A3B").strip()
CONF_MIN = float(os.environ.get("JEV_LOCAL_CONF_MIN", "0.55"))
LOCAL_SAMPLES = max(1, int(os.environ.get("JEV_LOCAL_SAMPLES", "2")))
_SECRET = re.compile(
    r"\b(password|passwd|passcode|api[_-]?key|token|secret|bearer)\b\s*[:=]\s*\S+"
    r"|\b(otp|2fa|verification code)\b\s*[:=]?\s*\d{4,8}"
    r"|\b(?:sk-|ghp_|github_pat_|xox[baprs]-|AKIA)[A-Za-z0-9_\-]{6,}",
    re.I,
)
_SENSITIVE = re.compile(
    r"(?i)\b(attorney|lawyer|legal counsel|accountant|bank account|routing number|"
    r"social security|\bssn\b|seed phrase|private key|finance/|legal/)\b"
)
_SOCIAL = re.compile(
    r"(?i)\b(social(?:\s*bot)?|social[_-]?bot|viral[_-]?social|"
    r"tweet|x\.com|instagram|tiktok|caption|content[_-]?calendar|"
    r"linkedin[_-]?lane|hailports[_-]?engagement)\b"
)


def mask_state(text: str) -> str:
    return _SECRET.sub("[redacted]", text or "")


def _key_files() -> list[Path]:
    """Key file lookup order from the module docstring: JEV_KEY_FILE, then XDG, then the stack secret."""
    out = []
    env_file = os.environ.get("JEV_KEY_FILE", "").strip()
    if env_file:
        out.append(Path(env_file).expanduser())
    return out + [XDG_KEY, STACK_KEY]


def _key() -> str:
    for name in ("TYPESAFE_API_KEY", "JEV_API_KEY"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    for path in _key_files():
        try:
            v = path.read_text(encoding="utf-8").strip()
            if v:
                return v
        except Exception:
            continue
    return ""


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _log(rec: dict) -> None:
    try:
        _append(LOG, rec)
    except Exception:
        pass
    try:
        if str(LEGACY_LOG) != str(LOG):
            _append(LEGACY_LOG, rec)
    except Exception:
        pass


def _decisions_path() -> Path:
    env = os.environ.get("LOCAL_FIRST_DECISIONS", "").strip()
    if env:
        return Path(env)
    cfg = STACK / "data" / "runtime" / "local_first_router.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        p = str(data.get("decisions_path") or "").strip()
        if p:
            return Path(p)
    except Exception:
        pass
    runtime = STACK / "data" / "runtime" / "router-decisions.jsonl"
    if runtime.parent.is_dir():
        return runtime
    return HOME / ".local" / "share" / "local-first" / "router-decisions.jsonl"


def _record_router(*, tool: str, route: str, why: str, backend: str, tokens: int = 0) -> None:
    """Body-free row for the offload rollup. Never logs state text."""
    try:
        from datetime import datetime, timezone

        row = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": tool or "jev",
            "class": "classify",
            "route": route,
            "tokens": int(tokens or 0),
            "latency": None,
            "why": (why or "")[:160],
            "backend": backend,
        }
        path = _decisions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass


def _social_lane(caller: str, state: str) -> bool:
    blob = f"{caller or ''}\n{state or ''}"
    if _SOCIAL.search(blob):
        return True
    env = (os.environ.get("JEV_LANE") or os.environ.get("SOCIAL_LANE") or "").strip().lower()
    return env in {"social", "1", "true", "yes"}


def _local_enabled() -> bool:
    if os.environ.get("JEV_LOCAL", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    # Optional kill file: present => force paid (quality-gate rollback).
    if (STACK / "data" / "flags" / "jev_local_off").exists():
        return False
    return True


def _hard_data_line(state: str) -> str | None:
    low = (state or "").lower()
    if _SENSITIVE.search(state or ""):
        return "hard_data_line"
    if "/legal/" in low or "/finance/" in low or "\\legal\\" in low or "\\finance\\" in low:
        return "hard_data_line_path"
    if _SECRET.search(state or ""):
        return "hard_data_line_secret"
    return None


def _model_up(timeout: float = 1.5) -> bool:
    root = MINI_URL.split("/v1/")[0] if "/v1/" in MINI_URL else "http://127.0.0.1:8080"
    try:
        with urllib.request.urlopen(root + "/health", timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200) < 500
    except Exception:
        try:
            with urllib.request.urlopen(root + "/v1/models", timeout=timeout) as resp:
                return int(getattr(resp, "status", 200) or 200) < 500
        except Exception:
            return False


def _estimate_tokens(text: str) -> int:
    return max(1, len((text or "").split()))


def _build_local_prompt(state: str, questions: dict) -> tuple[str, str]:
    system = (
        "You are a strict judgment classifier. Reply with ONLY one JSON object. "
        "No markdown fences, no prose.\n"
        "Schema: {\"answers\": {\"<id>\": <answer>}, \"confidence\": <0..1>}\n"
        "Answer shapes by question type:\n"
        "  noul  -> {\"noul\": <float 0..1>}\n"
        "  choice -> {\"choice\": \"<one criterion key>\", \"confidence\": <0..1>}\n"
        "  score -> {\"score\": <float 0..1>, \"confidence\": <0..1>}\n"
        "Use only the given criteria keys for choice. Wrap every choice as an object with choice+confidence. Wrap every score as an object with score+confidence. If unsure, lower confidence."
    )
    user = "STATE:\n" + state + "\n\nQUESTIONS:\n" + json.dumps(questions, ensure_ascii=False)
    return system, user


def _extract_json(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _normalize_answers(answers: dict, questions: dict) -> dict | None:
    if not isinstance(answers, dict) or not questions:
        return None
    out = {}
    for qid, spec in questions.items():
        if qid not in answers:
            return None
        a = answers[qid]
        qtype = str((spec or {}).get("type") or "").lower()
        if qtype == "noul":
            if isinstance(a, (int, float)):
                out[qid] = {"noul": float(a)}
                continue
            if isinstance(a, dict) and "noul" in a:
                try:
                    out[qid] = {"noul": float(a["noul"])}
                    continue
                except Exception:
                    return None
            return None
        if qtype == "choice":
            criteria = (spec or {}).get("criteria") or {}
            choice = None
            conf = 0.0
            if isinstance(a, str):
                choice = a
                conf = 0.7
            elif isinstance(a, dict):
                choice = a.get("choice") or a.get("label") or a.get("value")
                try:
                    conf = float(a.get("confidence") if a.get("confidence") is not None else 0.7)
                except Exception:
                    conf = 0.7
            if choice not in criteria:
                # tolerate close aliases / case
                if isinstance(choice, str):
                    for k in criteria:
                        if k.lower() == choice.lower():
                            choice = k
                            break
            if choice not in criteria:
                return None
            out[qid] = {"choice": choice, "confidence": conf}
            continue
        if qtype == "score":
            if isinstance(a, (int, float)):
                out[qid] = {"score": float(a), "confidence": 0.7}
                continue
            if isinstance(a, str):
                # map rubric words when criteria is a list
                labels = (spec or {}).get("criteria") or []
                if a in labels:
                    idx = labels.index(a)
                    out[qid] = {"score": (idx / max(1, len(labels) - 1)), "confidence": 0.7}
                    continue
                return None
            if not isinstance(a, dict) or a.get("score") is None:
                return None
            try:
                out[qid] = {
                    "score": float(a["score"]),
                    "confidence": float(a.get("confidence") or 0.7),
                }
                continue
            except Exception:
                return None
        return None
    return out


def _overall_confidence(answers: dict, questions: dict, declared: float | None) -> float:
    vals = []
    if isinstance(declared, (int, float)):
        vals.append(float(declared))
    for qid, spec in questions.items():
        a = answers.get(qid) or {}
        qtype = str((spec or {}).get("type") or "").lower()
        if not isinstance(a, dict):
            continue
        if qtype == "noul":
            try:
                n = float(a.get("noul"))
                vals.append(max(abs(n - 0.5) * 2.0, 0.0))
            except Exception:
                pass
        else:
            try:
                vals.append(float(a.get("confidence") or 0.0))
            except Exception:
                pass
    if not vals:
        return 0.0
    return min(vals)


def _verdict_key(answers: dict, questions: dict) -> tuple:
    rows = []
    for qid in sorted(questions):
        spec = questions[qid] or {}
        a = answers.get(qid) or {}
        qtype = str(spec.get("type") or "").lower()
        if qtype == "noul":
            try:
                n = float(a.get("noul"))
            except Exception:
                n = None
            if n is None:
                bucket = "x"
            elif n <= 0.1:
                bucket = "lo"
            elif n >= 0.9:
                bucket = "hi"
            else:
                bucket = "mid"
            rows.append((qid, "noul", bucket))
        elif qtype == "choice":
            rows.append((qid, "choice", str(a.get("choice"))))
        elif qtype == "score":
            try:
                s = float(a.get("score"))
            except Exception:
                s = None
            if s is None:
                bucket = "x"
            elif s <= 0.25:
                bucket = "lo"
            elif s >= 0.75:
                bucket = "hi"
            else:
                bucket = "mid"
            rows.append((qid, "score", bucket))
        else:
            rows.append((qid, qtype, "?"))
    return tuple(rows)


def _local_once(state: str, questions: dict, timeout: float, temperature: float) -> tuple[dict | None, float, int, str]:
    if not _model_up(min(1.5, max(0.5, timeout / 4))):
        return None, 0.0, 0, "model_down"
    system, user = _build_local_prompt(state, questions)
    payload = {
        "model": MINI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": 500,
        "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "jev_answers",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "answers": {"type": "object"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["answers"],
                    "additionalProperties": True,
                },
            },
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        MINI_URL, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=max(2.0, float(timeout))) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception as exc:
        return None, 0.0, 0, f"local_err:{type(exc).__name__}"
    msg = ((body.get("choices") or [{}])[0] or {}).get("message") or {}
    text = (msg.get("content") or "").strip()
    usage = body.get("usage") or {}
    tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    if tokens <= 0:
        tokens = _estimate_tokens(system) + _estimate_tokens(user) + _estimate_tokens(text)
    obj = _extract_json(text)
    if not obj:
        return None, 0.0, tokens, "malformed_json"
    raw_answers = obj.get("answers")
    if not isinstance(raw_answers, dict):
        raw_answers = None
    if isinstance(raw_answers, dict) and len(questions) == 1:
        qid = next(iter(questions))
        # Proper: answers.kind = {...}
        if qid in raw_answers:
            pass
        # Schema drift: answers = {choice/noul/score: ...} without qid
        elif any(k in raw_answers for k in ("choice", "noul", "score")):
            raw_answers = {qid: raw_answers}
        # Or answers accidentally holds the label string
        elif len(raw_answers) == 1:
            only_k, only_v = next(iter(raw_answers.items()))
            if only_k not in questions:
                raw_answers = {qid: raw_answers if isinstance(only_v, dict) else only_v}
    if not isinstance(raw_answers, dict):
        # Bare answer object for a single question: {choice/noul/score: ...}
        if len(questions) == 1 and any(k in obj for k in ("choice", "noul", "score")):
            qid = next(iter(questions))
            raw_answers = {qid: obj}
        elif all(k in questions for k in obj.keys() if k != "confidence"):
            raw_answers = {k: v for k, v in obj.items() if k in questions}
        else:
            raw_answers = {}
    answers = _normalize_answers(raw_answers or {}, questions)
    if not answers:
        return None, 0.0, tokens, "malformed_answers"
    conf = _overall_confidence(answers, questions, obj.get("confidence"))
    return answers, conf, tokens, "ok"


def _classify_local(state: str, questions: dict, timeout: float, caller: str) -> tuple[dict | None, str]:
    """Return (answers, why). answers None means escalate to paid."""
    temps = [0.0, 0.15] if LOCAL_SAMPLES >= 2 else [0.0]
    samples = []
    total_tokens = 0
    per = max(2.0, float(timeout) / max(1, len(temps)))
    for temp in temps[:LOCAL_SAMPLES]:
        ans, conf, tok, why = _local_once(state, questions, per, temp)
        total_tokens += tok
        if ans is None:
            _record_router(
                tool=f"jev:{caller or 'cli'}",
                route="paid",
                why=why,
                backend="mini_8080",
                tokens=tok,
            )
            return None, why
        samples.append((ans, conf))
    keys = [_verdict_key(a, questions) for a, _c in samples]
    if len(set(keys)) > 1:
        _record_router(
            tool=f"jev:{caller or 'cli'}",
            route="paid",
            why="self_consistency_disagree",
            backend="mini_8080",
            tokens=total_tokens,
        )
        return None, "self_consistency_disagree"
    conf = min(c for _a, c in samples)
    if conf < CONF_MIN:
        _record_router(
            tool=f"jev:{caller or 'cli'}",
            route="paid",
            why="low_confidence",
            backend="mini_8080",
            tokens=total_tokens,
        )
        return None, "low_confidence"
    _record_router(
        tool=f"jev:{caller or 'cli'}",
        route="local",
        why="jev_local_ok",
        backend="mini_8080",
        tokens=total_tokens,
    )
    return samples[0][0], "jev_local_ok"


def _classify_paid(state: str, questions: dict, timeout: float, caller: str, model: str | None):
    key = _key()
    if not key:
        _log({"ts": time.time(), "host": HOST, "caller": caller, "ok": False, "err": "no_key"})
        return None
    body = json.dumps({"model": model or MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/systemone",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hailports-jev/1",
        },
    )
    t0 = time.time()
    rec = {"ts": t0, "host": HOST, "caller": caller, "q": sorted(questions)}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            raise ValueError("no_answers")
        rec.update(ok=True, ms=int((time.time() - t0) * 1000), usage=data.get("usage"))
        _log(rec)
        usage = data.get("usage") or {}
        tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        if tokens <= 0:
            tokens = _estimate_tokens(state) + 40
        _record_router(
            tool=f"jev:{caller or 'cli'}",
            route="paid",
            why="typesafe",
            backend="typesafe_jev",
            tokens=tokens,
        )
        return answers
    except Exception as e:  # fail-open
        rec.update(ok=False, ms=int((time.time() - t0) * 1000), err=f"{type(e).__name__}:{str(e)[:120]}")
        _log(rec)
        _record_router(
            tool=f"jev:{caller or 'cli'}",
            route="paid",
            why="typesafe_fail",
            backend="typesafe_jev",
            tokens=0,
        )
        return None


def classify(state, questions: dict, timeout: float = 8.0, caller: str = "", model: str | None = None):
    if os.environ.get("JEV_DISABLE", "") == "1" or not questions:
        return None
    if not isinstance(state, str):
        state = str(state)
    state = mask_state(state)
    if len(state) > MAX_STATE:
        state = state[:MAX_STATE]

    blocked = _hard_data_line(state)
    social = _social_lane(caller, state)
    try_local = _local_enabled() and not social and blocked is None

    if try_local:
        local_ans, why = _classify_local(state, questions, timeout=timeout, caller=caller)
        if local_ans is not None:
            _log(
                {
                    "ts": time.time(),
                    "host": HOST,
                    "caller": caller,
                    "ok": True,
                    "backend": "mini_8080",
                    "why": why,
                    "q": sorted(questions),
                }
            )
            return local_ans
        paid = _classify_paid(state, questions, timeout=timeout, caller=caller, model=model)
        if paid is not None:
            return paid
        return None

    if social:
        _record_router(
            tool=f"jev:{caller or 'cli'}",
            route="paid",
            why="social_lane",
            backend="typesafe_jev",
            tokens=_estimate_tokens(state),
        )
    elif blocked:
        _record_router(
            tool=f"jev:{caller or 'cli'}",
            route="paid",
            why=blocked,
            backend="typesafe_jev",
            tokens=0,
        )
    return _classify_paid(state, questions, timeout=timeout, caller=caller, model=model)


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
    p.add_argument(
        "--question",
        action="append",
        default=[],
        help="NAME=TYPE:INSTRUCTIONS (repeatable). TYPE is noul, choice, or score",
    )
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
        code = int(e.code or 0)
        if code != 0:
            print("null")
        return 0
    except Exception:
        print("null")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
