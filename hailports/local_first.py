#!/usr/bin/env python3
"""Hard local-first router.

Low-end work (classify, search, summarize, triage, log read, packet prep,
first draft) is finished on the local model. High-stakes work and unsure
results escalate with a short brief. This module never calls an external
classifier, never sends mail, and never launches a paid CLI unless the
caller passes a paid hook and the decision is need_premium.

Contract fields: decision (complete_local|need_premium), why, confidence,
stakes (low|high|unsure), brief, answer, paid_invoked, backend.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA = "local_first.v1"
BRIEF_HEADER = (
    "[LOCAL-FIRST BRIEF v1]\n"
    "Role: paid model is editor and verifier only.\n"
    "Use the delta below. Do not redo a broad search for facts already listed.\n"
    "Do not send, post, or change production.\n"
)
LOCAL_ASK_PREFIX = (
    "LOCAL_FIRST_CONTRACT v1\n"
    "Reply with one JSON object and no other text.\n"
    "Keys: decision, confidence, why, answer.\n"
    "decision is complete_local or need_premium.\n"
    "confidence is a number from 0 to 1.\n"
    "complete_local only when stakes are low and the answer is finished.\n"
    "If stakes are high or unsure, decision must be need_premium and answer is a short brief.\n"
    "If you are not sure, decision is need_premium.\n"
    "Do not claim you sent, posted, or changed a live system.\n"
)
JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["complete_local", "need_premium"]},
        "confidence": {"type": "number"},
        "why": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["decision", "confidence", "why", "answer"],
}
_HIGH = (
    re.compile(r"\bprod(?:uction)?\b.{0,48}\b(?:deploy|release|change|push)\b", re.I),
    re.compile(r"\b(?:deploy|release)\b.{0,48}\bprod", re.I),
    re.compile(r"\b(?:send|post|publish)\b.{0,48}\b(?:e-?mail|mail|comment|message)\b", re.I),
    re.compile(r"\b(?:e-?mail|mail)\b.{0,48}\b(?:team|customer|client)\b", re.I),
    re.compile(r"\b(?:team-facing|customer-facing)\b", re.I),
    re.compile(r"\bdraft\b.{0,24}\b(?:e-?mail|reply|comment)\b", re.I),
    re.compile(r"\b(?:implement|refactor)\b", re.I),
    re.compile(r"\bwrite (?:the )?(?:code|function|class)\b", re.I),
    re.compile(r"\bpatch (?:the )?(?:code|function|bug|prod)", re.I),
    re.compile(r"\b(?:fix|debug)\b.{0,32}\b(?:bug|code|function|tests?)\b", re.I),
    re.compile(r"\bmerge (?:the )?(?:pr|pull request|branch)\b", re.I),
    re.compile(r"\bforce-push\b", re.I),
    re.compile(r"\bmust be (?:top-notch|perfect|production)\b", re.I),
)
_LOW = (
    re.compile(r"\b(?:summarize|summary|tl;dr|tldr)\b", re.I),
    re.compile(r"\b(?:classify|triage)\b", re.I),
    re.compile(r"\blogs?\b", re.I),
    re.compile(r"\b(?:search|look up|lookup)\b", re.I),
    re.compile(r"\b(?:first draft|draft a note|draft notes|packet prep)\b", re.I),
    re.compile(r"\bwhat does\b", re.I),
    re.compile(r"\blist the\b", re.I),
    re.compile(r"\bhow many\b", re.I),
    re.compile(r"\bstatus of\b", re.I),
)
_UNSURE = ("i don't know", "i do not know", "not sure", "cannot tell", "low confidence", "insufficient")
_FOOTER_AT = ("## STANDING RULE",)
_PREP_HINT = re.compile(r"\b(?:prep|brief|ticket|hold status)\b|\b\d{5,}\b", re.I)


def router_disabled() -> bool:
    flag = os.environ.get("LOCAL_FIRST_ROUTER", "").strip().lower()
    if flag in {"0", "off", "no", "skip"}:
        return True
    if os.environ.get("LOCAL_FIRST_DONE") == "1":
        return True
    return False


def split_footer(text: str) -> tuple[str, str]:
    cut = -1
    for marker in _FOOTER_AT:
        i = text.find(marker)
        if i != -1 and (cut == -1 or i < cut):
            cut = i
    if cut == -1:
        return text.strip(), ""
    return text[:cut].strip(), text[cut:].strip()


def classify_stakes(task: str, extra_high: list[str] | None = None) -> tuple[str, str]:
    """Return stakes and a short reason. High wins over low. Else unsure."""
    body = split_footer(task)[0]
    low_body = body.lower()
    for needle in extra_high or []:
        n = (needle or "").strip().lower()
        if n and n in low_body:
            return "high", "configured_high_stakes"
    for rx in _HIGH:
        if rx.search(body):
            return "high", "high_stakes"
    for rx in _LOW:
        if rx.search(body):
            return "low", "low_end_dig"
    return "unsure", "stakes_unsure"


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 16].rstrip() + "\n[truncated]"


def local_user_delta(task: str, stakes: str, evidence: str) -> str:
    """Stable instruction prefix, then a short delta (cache-friendly)."""
    clean = split_footer(task)[0]
    ev = _clip(evidence, 700) or "(none)"
    return (
        LOCAL_ASK_PREFIX
        + "\nSTAKES: "
        + stakes
        + "\nTASK:\n"
        + _clip(clean, 4000)
        + "\nEVIDENCE:\n"
        + ev
        + "\n"
    )


def build_brief(stakes: str, why: str, draft: str) -> str:
    delta = _clip(draft, 900) or "(no local draft)"
    return (
        BRIEF_HEADER
        + "Stakes: "
        + stakes
        + "\nWhy: "
        + _clip(why, 180).replace("\n", " ")
        + "\nDelta:\n"
        + delta
        + "\n"
    )


def _usable(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 20:
        return False
    low = t.lower()
    return not any(p in low for p in _UNSURE)


def parse_model_json(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _confidence(obj: dict | None, answer: str) -> float:
    if obj and isinstance(obj.get("confidence"), (int, float)):
        return max(0.0, min(1.0, float(obj["confidence"])))
    if not _usable(answer):
        return 0.2
    return 0.7


def policy_decision(task: str, local_text: str, *, extra_high: list[str] | None = None, backend: str = "local") -> dict:
    """Apply the conservative contract to one local reply. No network."""
    stakes, stake_why = classify_stakes(task, extra_high)
    obj = parse_model_json(local_text)
    answer = ""
    model_why = ""
    model_decision = ""
    if obj:
        answer = str(obj.get("answer") or "").strip()
        model_why = str(obj.get("why") or "").strip()
        model_decision = str(obj.get("decision") or "").strip()
    elif local_text:
        answer = local_text.strip()
    conf = _confidence(obj, answer)
    why = model_why or stake_why

    def pack(decision: str, why_text: str, final_answer: str) -> dict:
        brief = build_brief(stakes, why_text, answer or local_text or why_text)
        return {
            "schema": SCHEMA,
            "decision": decision,
            "why": _clip(why_text, 180).replace("\n", " "),
            "confidence": round(conf, 3),
            "stakes": stakes,
            "brief": brief,
            "answer": final_answer if decision == "complete_local" else "",
            "paid_invoked": False,
            "backend": backend,
        }

    if stakes == "high":
        return pack("need_premium", why or "high_stakes", "")
    if stakes == "unsure":
        return pack("need_premium", why or "stakes_unsure", "")
    # Low-end: finish locally when the text is actually usable.
    if model_decision == "need_premium" and not _usable(answer):
        return pack("need_premium", why or "local_unusable", "")
    if conf < 0.55 and not _usable(answer):
        return pack("need_premium", why or "low_confidence", "")
    if _usable(answer):
        return pack("complete_local", why or "low_end_usable", answer)
    return pack("need_premium", why or "local_unusable", "")


def compact_task_for_paid(task: str) -> str:
    clean, _footer = split_footer(task)
    transcript = clean.count("\nUser:") + clean.count("\nAssistant:") >= 2
    if transcript or len(clean) > 8000:
        return _clip(clean[-1600:], 1600)
    return clean


def paid_prompt(result: dict, task: str) -> str:
    _clean, footer = split_footer(task)
    body = BRIEF_HEADER and (result.get("brief") or build_brief(result.get("stakes") or "unsure", result.get("why") or "", ""))
    text = body.rstrip() + "\n\n[TASK]\n" + compact_task_for_paid(task).strip() + "\n"
    if footer:
        text += "\n" + footer.strip() + "\n"
    return text


def load_config() -> dict:
    env = os.environ.get("LOCAL_FIRST_CONFIG", "").strip()
    path = Path(env) if env else Path(__file__).resolve().parents[1] / "data" / "runtime" / "local_first_router.json"
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    if not data:
        data = {"transport": "http", "http_url": os.environ.get("LOCAL_FIRST_URL", "http://127.0.0.1:8080/v1/chat/completions")}
    overlay = Path.home() / ".config" / "local-first" / "paid_bins.json"
    if overlay.is_file():
        try:
            extra = json.loads(overlay.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            extra = None
        if isinstance(extra, dict):
            bins = dict(data.get("paid_bins") or {})
            incoming = extra.get("paid_bins") if isinstance(extra.get("paid_bins"), dict) else extra
            if isinstance(incoming, dict):
                for key, value in incoming.items():
                    bins.setdefault(str(key), value)
            data["paid_bins"] = bins
    return data


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def want_prep(task: str) -> bool:
    return bool(_PREP_HINT.search(split_footer(task)[0]))


def fetch_prep(cfg: dict, task: str) -> str:
    url = str(cfg.get("prep_url") or "").strip()
    if not url or not want_prep(task):
        return ""
    body = json.dumps({"task": split_footer(task)[0][:1500], "allow_llm": False, "synthesize": False})
    cmd = ["curl", "-sS", "--max-time", "8", "-H", "Content-Type: application/json", "--data-binary", "@-"]
    socks = str(cfg.get("socks_proxy") or "").strip()
    if socks:
        cmd[1:1] = ["--socks5-hostname", socks]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, input=body, text=True, capture_output=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    raw = (proc.stdout or "").strip()
    if not raw.startswith("{"):
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    text = data.get("brief_text") or data.get("synthesized_brief") or ""
    return _clip(str(text), 700)


def _ssh_fetch(cfg: dict, prompt: str, priority: int) -> dict:
    """Run the Mini hybrid caller over SSH.

    The remote shell is not given a python -c string (it splits and globs).
    The program goes to python3 - on stdin, with the task inside the program
    as base64 so the shared-prefix caller on :8080 is the one that runs.
    """
    import base64
    target = str(cfg.get("ssh_target") or "").strip()
    remote_cli = str(cfg.get("remote_cli") or "").strip()
    if not target or not re.fullmatch(r"[A-Za-z0-9_.+\-@]+", target):
        return {"ok": False, "error": "ssh_unconfigured"}
    if not re.fullmatch(r"/[A-Za-z0-9_./\-]+", remote_cli):
        return {"ok": False, "error": "ssh_unconfigured"}
    blob = base64.b64encode(json.dumps({
        "cli": remote_cli,
        "priority": int(priority),
        "prompt": prompt,
    }).encode("utf-8")).decode("ascii")
    remote = (
        "import base64,json,sys\n"
        "job=json.loads(base64.b64decode('" + blob + "'))\n"
        "sys.path.insert(0, job['cli'])\n"
        "try:\n"
        "    from hybrid_overtime_engine import call_local_8080\n"
        "except Exception as exc:\n"
        "    json.dump({'ok': False, 'error': 'import_'+type(exc).__name__}, sys.stdout)\n"
        "    raise SystemExit(0)\n"
        "schema={'type':'object','properties':{'decision':{'type':'string'},'confidence':{'type':'number'},'why':{'type':'string'},'answer':{'type':'string'}},'required':['decision','confidence','why','answer']}\n"
        "try:\n"
        "    out=call_local_8080(job['prompt'], max_tokens=420, temperature=0.1, timeout=80, priority=int(job['priority']), json_schema=schema)\n"
        "except Exception as exc:\n"
        "    json.dump({'ok': False, 'error': type(exc).__name__}, sys.stdout)\n"
        "    raise SystemExit(0)\n"
        "json.dump({'ok': True, 'text': out.get('text') or '', 'elapsed_s': out.get('elapsed_s'), 'usage': out.get('usage') or {}}, sys.stdout)\n"
    )
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "-o", "StrictHostKeyChecking=accept-new"]
    proxy = str(cfg.get("ssh_proxy_command") or "").strip()
    if proxy:
        cmd += ["-o", "ProxyCommand=" + proxy]
    cmd += [target, "python3", "-"]
    try:
        proc = subprocess.run(cmd, input=remote, text=True, capture_output=True, timeout=110)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": type(exc).__name__}
    raw = (proc.stdout or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    snippet = raw[start:end + 1] if start != -1 and end > start else ""
    try:
        data = json.loads(snippet) if snippet else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return {"ok": False, "error": "ssh_bad_reply", "rc": proc.returncode}
    return data


def _http_fetch(cfg: dict, prompt: str) -> dict:
    url = str(cfg.get("http_url") or os.environ.get("LOCAL_FIRST_URL") or "").strip()
    if not url:
        return {"ok": False, "error": "http_unconfigured"}
    payload = {
        "model": str(cfg.get("model") or "local"),
        "local_only": True,
        "messages": [
            {"role": "system", "content": LOCAL_ASK_PREFIX},
            {"role": "user", "content": prompt[len(LOCAL_ASK_PREFIX):].strip() if prompt.startswith(LOCAL_ASK_PREFIX) else prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 420,
        "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {"name": "local_first", "strict": True, "schema": JSON_SCHEMA}},
    }
    body = json.dumps(payload)
    cmd = ["curl", "-sS", "--max-time", "90", "-H", "Content-Type: application/json", "--data-binary", "@-"]
    socks = str(cfg.get("socks_proxy") or "").strip()
    if socks and not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
        cmd[1:1] = ["--socks5-hostname", socks]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, input=body, text=True, capture_output=True, timeout=100)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": type(exc).__name__}
    raw = (proc.stdout or "").strip()
    try:
        data = json.loads(raw) if raw.startswith("{") else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return {"ok": False, "error": "http_bad_reply"}
    if data.get("gateway_route", {}).get("backend") not in (None, "", "local"):
        return {"ok": False, "error": "refused_non_local"}
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    if not text and data.get("error"):
        return {"ok": False, "error": "http_error"}
    return {"ok": bool(text), "text": text, "usage": data.get("usage") or {}, "error": "" if text else "empty"}


def fetch_local(cfg: dict, prompt: str, *, priority: int) -> dict:
    transport = str(cfg.get("transport") or "http").strip().lower()
    if transport == "ssh":
        got = _ssh_fetch(cfg, prompt, priority)
        if got.get("ok"):
            got["backend"] = "mini_8080"
            return got
        http = _http_fetch(cfg, prompt)
        if http.get("ok"):
            http["backend"] = "mini_8080_http"
            return http
        return {"ok": False, "error": got.get("error") or http.get("error") or "local_unavailable", "backend": "unavailable"}
    got = _http_fetch(cfg, prompt)
    got["backend"] = "mini_8080_http" if got.get("ok") else "unavailable"
    return got


def _record(cfg: dict, result: dict, task: str) -> None:
    if os.environ.get("LOCAL_FIRST_RECORD") == "0":
        return
    path = Path(str(cfg.get("receipt_path") or "")) if cfg.get("receipt_path") else Path(tempfile.gettempdir()) / "local-first-receipts.jsonl"
    usage = result.get("usage") or {}
    row = {
        "schema": SCHEMA,
        "decision": result.get("decision"),
        "why": result.get("why"),
        "confidence": result.get("confidence"),
        "stakes": result.get("stakes"),
        "paid_invoked": bool(result.get("paid_invoked")),
        "backend": result.get("backend"),
        "task_sha": _sha(split_footer(task)[0]),
        "answer_chars": len(result.get("answer") or ""),
        "brief_chars": len(result.get("brief") or ""),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cached_tokens": ((usage.get("prompt_tokens_details") or {}) if isinstance(usage.get("prompt_tokens_details"), dict) else {}).get("cached_tokens"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass


def _jev_after_local(task: str, local_text: str, result: dict, caller: str) -> dict:
    """High-confidence score can stop on a good local answer or escalate a bad one.

    High stakes stay on the existing path. Any classifier failure leaves result unchanged.
    """
    try:
        if os.environ.get("JEV_DISABLE", "") == "1":
            return result
        if result.get("stakes") != "low":
            return result
        if not (local_text or "").strip():
            return result
        core = str(Path(__file__).resolve().parent)
        if core not in sys.path:
            sys.path.insert(0, core)
        from jev_gates import score_local
        action = score_local(task, local_text, caller="local-first:" + caller)
    except Exception:
        return result
    if action == "stop":
        answer = str(result.get("answer") or "").strip()
        if not answer:
            obj = parse_model_json(local_text)
            if obj:
                answer = str(obj.get("answer") or "").strip()
        if not answer:
            answer = (local_text or "").strip()
        if len(answer) < 1:
            return result
        if any(phrase in answer.lower() for phrase in _UNSURE) and len(answer) < 40:
            return result
        result["decision"] = "complete_local"
        result["answer"] = answer
        result["why"] = "jev_high_confidence"
        result["confidence"] = max(float(result.get("confidence") or 0), 0.9)
    elif action == "escalate":
        result["decision"] = "need_premium"
        result["answer"] = ""
        result["why"] = "jev_low_confidence"
        result["confidence"] = min(float(result.get("confidence") or 1), 0.1)
        result["brief"] = build_brief(str(result.get("stakes") or "low"), "jev_low_confidence", local_text)
    return result


def _jev_pre_route(task: str, caller: str) -> str | None:
    try:
        if os.environ.get("JEV_DISABLE", "") == "1":
            return None
        core = str(Path(__file__).resolve().parent)
        if core not in sys.path:
            sys.path.insert(0, core)
        from jev_gates import pre_route
        return pre_route(task, caller="cli-pre:" + caller)
    except Exception:
        return None


def _jev_done(task: str, answer: str, caller: str) -> bool | None:
    try:
        if os.environ.get("JEV_DISABLE", "") == "1":
            return None
        core = str(Path(__file__).resolve().parent)
        if core not in sys.path:
            sys.path.insert(0, core)
        from jev_gates import done_check
        return done_check(task, answer, caller="cli-post:" + caller)
    except Exception:
        return None


def route_task(task: str, *, caller: str = "cli", fetch=None, config: dict | None = None, execute_paid=None) -> dict:
    cfg = config if config is not None else load_config()
    extra = [str(x) for x in (cfg.get("extra_high_substrings") or [])]
    stakes, _why = classify_stakes(task, extra)
    evidence = ""
    if fetch is None and config is None:
        evidence = fetch_prep(cfg, task)
    elif fetch is None and cfg.get("prep_url"):
        evidence = fetch_prep(cfg, task)
    prompt = local_user_delta(task, stakes, evidence)
    priority = 0 if caller in {"grok", "cli", "cursor", "claude", "codex", "cursor-agent", "agent"} else 2
    if fetch is None:
        local = fetch_local(cfg, prompt, priority=priority)
    else:
        local = fetch(prompt)
    backend = str((local or {}).get("backend") or "local")
    if not local or not local.get("ok"):
        result = policy_decision(task, "", extra_high=extra, backend="unavailable")
        result["decision"] = "need_premium"
        result["answer"] = ""
        result["why"] = "local_unavailable"
        result["brief"] = build_brief(result["stakes"], "local_unavailable", evidence or "Local model did not answer.")
        result["usage"] = {}
    else:
        local_text = str(local.get("text") or "")
        result = policy_decision(task, local_text, extra_high=extra, backend=backend)
        result["usage"] = local.get("usage") or {}
        result["elapsed_s"] = local.get("elapsed_s")
        result = _jev_after_local(task, local_text, result, caller)
    result["caller"] = caller
    result["paid_invoked"] = False
    if execute_paid is not None and result["decision"] == "need_premium" and os.environ.get("LOCAL_FIRST_DRY") != "1":
        execute_paid(paid_prompt(result, task))
        result["paid_invoked"] = True
    _record(cfg if config is None else (config or {}), result, task)
    return result


def before_paid(task: str, caller: str = "paid") -> tuple[str, str]:
    """Return done+answer, escalate+briefed task, or passthrough+task.

    Never launches a paid process. Callers that escalate open the paid CLI
    themselves, with the brief already on the task.
    """
    if router_disabled():
        return "passthrough", task
    try:
        result = gate_for_paid(task, caller=caller)
    except Exception:
        return "passthrough", task
    if isinstance(result, dict) and result.get("decision") == "complete_local" and str(result.get("answer") or "").strip():
        return "done", str(result["answer"]).strip()
    brief = str((result or {}).get("brief") or "").strip() if isinstance(result, dict) else ""
    if isinstance(result, dict) and result.get("decision") == "need_premium" and brief:
        return "escalate", brief + "\n\n[TASK]\n" + task
    return "passthrough", task


def gate_for_paid(task: str, caller: str = "paid") -> dict:
    """Bot and cursor door. Never launches a paid process."""
    if router_disabled():
        return {
            "schema": SCHEMA,
            "decision": "need_premium",
            "why": "router_off",
            "confidence": 0.0,
            "stakes": "unsure",
            "brief": "",
            "answer": "",
            "paid_invoked": False,
            "backend": "skipped",
        }
    return route_task(task, caller=caller, execute_paid=None)


def _extract_prompt(argv: list[str]) -> tuple[str, str, int]:
    """Return prompt, kind (inline|file|none), index of the value."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in {"-p", "--single", "--prompt-file"} and i + 1 < len(argv):
            kind = "file" if a == "--prompt-file" else "inline"
            return argv[i + 1], kind, i + 1
        if a.startswith("--prompt-file="):
            return a.split("=", 1)[1], "file", i
        i += 1
    return "", "none", -1


def plan_grok(argv: list[str], result: dict | None) -> tuple[str, list[str]]:
    """Return action and argv for the real grok binary (no argv0).

    action is passthrough, local, or escalate. local means do not exec grok.
    """
    if router_disabled() or result is None:
        return "passthrough", list(argv)
    prompt, kind, idx = _extract_prompt(argv)
    if kind == "none":
        return "passthrough", list(argv)
    if result.get("decision") == "complete_local" and (result.get("answer") or "").strip():
        return "local", []
    text = paid_prompt(result, _read_prompt(prompt, kind))
    new = list(argv)
    if kind == "file" or len(text) > 3500:
        fd, path = tempfile.mkstemp(prefix="local-first-", suffix=".md")
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        os.chmod(path, 0o600)
        if kind == "inline":
            new[idx - 1] = "--prompt-file"
            new[idx] = path
        elif argv[idx].startswith("--prompt-file="):
            new[idx] = "--prompt-file=" + path
        else:
            new[idx] = path
    else:
        if kind == "inline":
            new[idx] = text
        else:
            fd, path = tempfile.mkstemp(prefix="local-first-", suffix=".md")
            os.write(fd, text.encode("utf-8"))
            os.close(fd)
            new[idx] = path
    return "escalate", new


def _read_prompt(prompt: str, kind: str) -> str:
    if kind == "file":
        try:
            return Path(prompt).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return prompt
    return prompt


# Flag sets are the non-interactive shapes each CLI actually documents.
# claude: 2.1.283 --help. codex: `exec` / `e` / `review` (binary not on this host).
# cursor-agent: option strings in the 2026.10.19 launcher, not a live process.
_CLAUDE_VALUE = {
    "--agent", "--agents", "--append-system-prompt", "--append-system-prompt-file",
    "--append-subagent-system-prompt", "--append-subagent-system-prompt-file",
    "--autocompact", "--client-data-url", "--debug-file", "--effort", "--environment",
    "--fallback-model", "--input-format", "--json-schema", "--max-budget-usd",
    "--model", "-n", "--name", "--output-format", "--permission-mode",
    "--permission-prompts", "--permission-prompt-tool",
    "--remote-control-session-name-prefix", "--session-id", "--setting-sources",
    "--settings", "--system-prompt", "--system-prompt-file", "--system-prompt-snapshot",
}
_CLAUDE_COMMANDS = {
    "agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import", "install",
    "logs", "mcp", "plugin", "plugins", "project", "respawn", "rm", "setup-token",
    "stop", "kill", "ultrareview", "update", "upgrade",
}
_CODEX_VALUE = {
    "-m", "--model", "-s", "--sandbox", "-C", "--cd", "-i", "--image",
    "--output-schema", "-o", "--output-last-message", "--color",
    "-a", "--ask-for-approval", "-c", "--config", "--profile",
}
_CODEX_HEADLESS = {"exec", "e", "review"}
_CURSOR_VALUE = {
    "--output-format", "--model", "--mode", "--workspace", "--add-dir", "--plugin-dir",
    "--api-key", "--auth-token", "--sandbox", "--background-shell-timeout", "--image",
    "--system-prompt", "--conversation-history-file", "--new-session-id",
    "--dev-raw-model-slug", "--allowed-tools", "--exclude-tools", "--data-dir",
    "--computer-use-coords", "--http-version", "--repo-endpoint", "--repo-http-version",
}
_CURSOR_COMMANDS = {
    "about", "acp", "automations", "bedrock", "clear", "cloud", "configure", "controller",
    "create-chat", "debug", "dev", "dev-login", "disable", "enable", "env", "generate-rule",
    "get-channel", "info", "install-shell-integration", "kill", "list", "local-worker",
    "login", "logout", "ls", "marketplace", "mcp", "models", "persist", "ping", "plugin",
    "record", "repo", "reset", "resume", "run", "runs", "sandbox", "score-recent-commits",
    "self-test", "sleep", "start", "status", "team", "test", "uninstall-shell-integration",
    "update", "upgrade", "use-team-role", "worker", "worker-server",
}
_BIN_SPECS = {
    "grok": {"env": "GROK_REAL_BIN", "argv0": "grok", "fallback": True},
    "claude": {"env": "CLAUDE_REAL_BIN", "argv0": "claude"},
    "codex": {"env": "CODEX_REAL_BIN", "argv0": "codex"},
    "cursor-agent": {"env": "CURSOR_AGENT_REAL_BIN", "argv0": "cursor-agent"},
}
_WRAPPER_NAMES = {
    "grok-local-first", "claude-local-first", "codex-local-first",
    "cursor-agent-local-first", "local-first-route", "local-first-openai-door",
    "local_first_router.py", "local_first.py",
}
def _no_prompt() -> dict:
    return {"kind": "none", "text": "", "slot": -1, "extra_slots": []}


def _caller_key(caller: str) -> str:
    name = (caller or "").strip().lower()
    if name in {"cursor", "agent"}:
        return "cursor-agent"
    return name


def _scan_args(argv: list[str], value_flags: set[str]) -> tuple[set[str], list[tuple[int, str]]]:
    flags: set[str] = set()
    positionals: list[tuple[int, str]] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            positionals.extend((i + 1 + n, item) for n, item in enumerate(argv[i + 1 :]))
            break
        if token.startswith("-") and token != "-":
            name = token.split("=", 1)[0]
            flags.add(name)
            if "=" in token:
                i += 1
                continue
            if name in value_flags and i + 1 < len(argv):
                i += 2
                continue
            i += 1
            continue
        positionals.append((i, token))
        i += 1
    return flags, positionals


def _inline_prompt(items: list[tuple[int, str]]) -> dict:
    if not items:
        return {"kind": "stdin", "text": "", "slot": -1, "extra_slots": []}
    text = " ".join(token for _, token in items)
    return {
        "kind": "inline",
        "text": text,
        "slot": items[0][0],
        "extra_slots": [index for index, _ in items[1:]],
    }


def extract_prompt(caller: str, argv: list[str], *, stdout_is_tty: bool = True) -> dict:
    """Pull a non-interactive prompt. kind none means leave the real CLI alone."""
    key = _caller_key(caller)
    if key == "grok":
        text, kind, idx = _extract_prompt(argv)
        if kind == "none":
            return _no_prompt()
        return {"kind": kind, "text": text, "slot": idx, "extra_slots": []}
    if key == "claude":
        flags, positionals = _scan_args(argv, _CLAUDE_VALUE)
        if positionals and positionals[0][1] in _CLAUDE_COMMANDS and "-p" not in flags and "--print" not in flags:
            return _no_prompt()
        print_mode = "-p" in flags or "--print" in flags
        if not print_mode and stdout_is_tty:
            return _no_prompt()
        if not print_mode and not positionals:
            return _no_prompt()
        return _inline_prompt(positionals)
    if key == "codex":
        flags, positionals = _scan_args(argv, _CODEX_VALUE)
        head = next((n for n, (_, token) in enumerate(positionals) if token in _CODEX_HEADLESS), None)
        if head is None:
            return _no_prompt()
        word = positionals[head][1]
        rest = positionals[head + 1 :]
        if word == "review" and not rest:
            return _no_prompt()
        if len(rest) == 1 and rest[0][1] == "-":
            return {"kind": "stdin", "text": "", "slot": -1, "extra_slots": []}
        return _inline_prompt(rest)
    if key == "cursor-agent":
        if argv and argv[0] in _CURSOR_COMMANDS:
            return _no_prompt()
        flags, positionals = _scan_args(argv, _CURSOR_VALUE)
        prompts = [(index, token) for index, token in positionals if not (index == 0 and token == "agent")]
        print_mode = "-p" in flags or "--print" in flags
        if not print_mode and (stdout_is_tty or not prompts):
            return _no_prompt()
        return _inline_prompt(prompts)
    return _no_prompt()


def _flag_value(argv: list[str], names: set[str]) -> str:
    for index, token in enumerate(argv):
        for name in names:
            if token == name and index + 1 < len(argv):
                return argv[index + 1]
            if token.startswith(name + "="):
                return token.split("=", 1)[1]
    return ""


def wants_structured_output(caller: str, argv: list[str]) -> bool:
    """A JSON event stream cannot be replaced with a plain local answer."""
    key = _caller_key(caller)
    if key == "claude":
        fmt = _flag_value(argv, {"--output-format"}).lower()
        incoming = _flag_value(argv, {"--input-format"}).lower()
        return fmt in {"json", "stream-json"} or incoming == "stream-json" or "--json-schema" in argv or any(
            token.startswith("--json-schema=") for token in argv
        )
    if key == "codex":
        return "--json" in argv or "--output-schema" in argv or any(token.startswith("--output-schema=") for token in argv)
    if key == "cursor-agent":
        fmt = _flag_value(argv, {"--output-format"}).lower()
        return fmt in {"json", "stream-json"}
    return False


def _is_wrapper(path: str | Path) -> bool:
    candidate = Path(path)
    try:
        if not candidate.exists():
            return False
        resolved = candidate.resolve()
    except OSError:
        return False
    if resolved.name in _WRAPPER_NAMES or resolved == Path(__file__).resolve():
        return True
    try:
        with candidate.open("rb") as handle:
            blob = handle.read(400)
    except OSError:
        return False
    return b"local-first-wrapper-v1" in blob


def _usable_real(path: str) -> bool:
    text = (path or "").strip()
    if not text or _is_wrapper(text):
        return False
    return True


def _path_after_wrapper(argv0: str) -> str:
    dirs = [item for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    wrapped: list[int] = []
    found: list[tuple[int, str]] = []
    for index, directory in enumerate(dirs):
        candidate = Path(directory) / argv0
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        if _is_wrapper(candidate):
            wrapped.append(index)
            continue
        found.append((index, str(candidate)))
    if not found:
        return ""
    if wrapped:
        last = max(wrapped)
        for index, path in found:
            if index > last:
                return path
    return found[0][1]


def resolve_real_bin(caller: str, cfg: dict | None = None) -> str:
    """Env, then paid_bins, then the next PATH entry that is not this wrapper."""
    key = _caller_key(caller)
    spec = _BIN_SPECS.get(key)
    if spec is None:
        return ""
    env = os.environ.get(spec["env"], "").strip()
    if env and _usable_real(env):
        return env
    cfg = cfg if cfg is not None else load_config()
    bins = cfg.get("paid_bins") if isinstance(cfg.get("paid_bins"), dict) else {}
    configured = str((bins or {}).get(key) or "").strip()
    if not configured and key == "grok":
        configured = str(cfg.get("paid_bin") or "").strip()
    if configured and _usable_real(configured):
        return configured
    found = _path_after_wrapper(spec["argv0"])
    if found:
        return found
    if spec.get("fallback"):
        home_bin = str(Path.home() / ".grok" / "bin" / "grok")
        if _usable_real(home_bin):
            return home_bin
    return ""


def real_grok_bin(cfg: dict | None = None) -> str:
    return resolve_real_bin("grok", cfg)


def _rewrite_prompt(argv: list[str], extracted: dict, text: str) -> list[str]:
    new = list(argv)
    extras = [slot for slot in extracted.get("extra_slots") or [] if isinstance(slot, int)]
    for slot in sorted(extras, reverse=True):
        if 0 <= slot < len(new):
            del new[slot]
    slot = extracted.get("slot", -1)
    if isinstance(slot, int) and slot >= 0:
        slot -= sum(1 for item in extras if item < slot)
        if 0 <= slot < len(new):
            new[slot] = text
            return new
    new.append(text)
    return new


def _materialize_prompt(extracted: dict, stdin_text: str | None) -> str:
    kind = extracted.get("kind")
    if kind == "file":
        return _read_prompt(str(extracted.get("text") or ""), "file")
    if kind == "stdin":
        if stdin_text is not None:
            return stdin_text
        if sys.stdin.isatty():
            return ""
        return sys.stdin.read()
    return str(extracted.get("text") or "")


def prepare_intercept(
    caller: str,
    argv: list[str],
    *,
    fetch=None,
    config: dict | None = None,
    stdin_text: str | None = None,
    stdout_is_tty: bool | None = None,
) -> dict:
    """Decide local, escalate, or passthrough. Does not exec."""
    tty = sys.stdout.isatty() if stdout_is_tty is None else stdout_is_tty
    base = {"action": "passthrough", "argv": list(argv), "answer": "", "result": {}, "set_done": False}
    if any(token in {"-h", "--help", "--version"} for token in argv):
        return base
    if router_disabled():
        return base
    extracted = extract_prompt(caller, argv, stdout_is_tty=tty)
    if extracted["kind"] == "none":
        return base
    if wants_structured_output(caller, argv):
        return base
    task = _materialize_prompt(extracted, stdin_text)
    if not task.strip():
        return base
    if task.startswith("[LOCAL-FIRST BRIEF v1]"):
        base["set_done"] = True
        return base
    if _jev_pre_route(task, _caller_key(caller)) == "escalate":
        result = policy_decision(task, "", backend="jev_pre")
        result["decision"] = "need_premium"
        result["answer"] = ""
        result["why"] = "jev_route_escalate"
        result["brief"] = build_brief(str(result.get("stakes") or "low"), "jev_route_escalate", "Routing asked for a stronger model.")
        result["caller"] = _caller_key(caller)
        result["paid_invoked"] = False
    else:
        result = route_task(task, caller=_caller_key(caller), fetch=fetch, config=config, execute_paid=None)
    if _caller_key(caller) == "grok":
        action, new_argv = plan_grok(argv, result)
    elif result.get("decision") == "complete_local" and str(result.get("answer") or "").strip():
        action, new_argv = "local", []
    else:
        action, new_argv = "escalate", _rewrite_prompt(argv, extracted, paid_prompt(result, task))
    if action == "local" and _jev_done(task, str(result.get("answer") or ""), _caller_key(caller)) is False:
        action, new_argv = "escalate", _rewrite_prompt(argv, extracted, paid_prompt(result, task))
    return {
        "action": action,
        "argv": new_argv,
        "answer": str(result.get("answer") or ""),
        "result": result,
        "set_done": action == "escalate",
    }


def run_intercept(
    caller: str,
    argv: list[str],
    *,
    fetch=None,
    config: dict | None = None,
    stdin_text: str | None = None,
    stdout_is_tty: bool | None = None,
) -> int:
    try:
        decision = prepare_intercept(
            caller, argv, fetch=fetch, config=config, stdin_text=stdin_text, stdout_is_tty=stdout_is_tty,
        )
    except Exception:
        decision = {"action": "passthrough", "argv": list(argv), "answer": "", "result": {}, "set_done": False}
    if decision.get("action") == "local":
        sys.stdout.write((decision.get("answer") or "").rstrip() + "\n")
        backend = (decision.get("result") or {}).get("backend")
        sys.stderr.write(f"[local-first] decision=complete_local paid_invoked=false backend={backend}\n")
        sys.stdout.flush()
        sys.stderr.flush()
        return 0
    bin_path = resolve_real_bin(caller, config)
    if not bin_path:
        sys.stderr.write("[local-first] real binary not found\n")
        return 127
    if decision.get("set_done"):
        os.environ["LOCAL_FIRST_DONE"] = "1"
    result = decision.get("result") or {}
    if decision.get("action") == "escalate":
        sys.stderr.write(
            f"[local-first] decision=need_premium paid_invoked=true backend={result.get('backend')} why={result.get('why')}\n"
        )
    sys.stdout.flush()
    sys.stderr.flush()
    forwarded = decision.get("argv")
    if not isinstance(forwarded, list):
        forwarded = list(argv)
    os.execv(bin_path, [bin_path, *forwarded])
    return 127


def run_grok_intercept(argv: list[str]) -> int:
    return run_intercept("grok", argv)


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item.get("text")))
        return "\n".join(parts)
    return ""


def openai_completion(payload: dict, *, fetch=None, config: dict | None = None) -> tuple[int, dict, str, bool]:
    """One chat completion. The door never starts a paid IDE model."""
    messages = payload.get("messages") if isinstance(payload, dict) else None
    task = "\n".join(
        _content_text(msg.get("content"))
        for msg in (messages or [])
        if isinstance(msg, dict) and msg.get("role") == "user"
    ).strip()
    if not task:
        task = "Summarize the request."
    try:
        result = route_task(task, caller="openai-door", fetch=fetch, config=config, execute_paid=None)
    except Exception:
        result = {
            "decision": "need_premium",
            "answer": "",
            "brief": build_brief("unsure", "router_error", "Local route failed."),
            "backend": "unavailable",
            "why": "router_error",
        }
    local = result.get("decision") == "complete_local" and str(result.get("answer") or "").strip()
    if local:
        content = str(result.get("answer") or "").rstrip()
    else:
        content = (
            str(result.get("brief") or "").rstrip()
            + "\n\nThis endpoint cannot start the paid editor model. "
            + "Switch the model picker to a paid model and use the brief above.\n"
        )
    body = {
        "id": "local-first",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }
    return 200, {"X-Local-First-Decision": "complete_local" if local else "need_premium"}, json.dumps(body), bool(payload.get("stream"))


def serve_openai(argv: list[str]) -> int:
    host = "127.0.0.1"
    port = 8791
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--host" and index + 1 < len(argv):
            host = argv[index + 1]
            index += 2
            continue
        if token == "--port" and index + 1 < len(argv):
            port = int(argv[index + 1])
            index += 2
            continue
        index += 1
    if host not in {"127.0.0.1", "localhost", "::1"}:
        sys.stderr.write("local-first door refuses a non-loopback bind\n")
        return 2
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, status: int, headers: dict, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/v1/models":
                self._send(404, {}, b"{}", "application/json")
                return
            body = json.dumps({"object": "list", "data": [{"id": "local-first", "object": "model"}]}).encode()
            self._send(200, {}, body, "application/json")

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/v1/chat/completions":
                self._send(404, {}, b"{}", "application/json")
                return
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send(400, {}, b"{}", "application/json")
                return
            status, headers, text, stream = openai_completion(payload)
            if stream:
                chunk = json.dumps({
                    "id": "local-first",
                    "object": "chat.completion.chunk",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": json.loads(text)["choices"][0]["message"]["content"]},
                        "finish_reason": "stop",
                    }],
                })
                data = ("data: " + chunk + "\n\ndata: [DONE]\n\n").encode()
                self._send(status, headers, data, "text/event-stream")
                return
            self._send(status, headers, text.encode(), "application/json")

    try:
        server = ThreadingHTTPServer((host if host != "localhost" else "127.0.0.1", port), Handler)
    except OSError as exc:
        sys.stderr.write(f"local-first door failed: {exc}\n")
        return 1
    sys.stderr.write(f"[local-first] openai door on http://127.0.0.1:{port}/v1\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


def _selftest() -> int:
    cases = []
    saved_router = os.environ.get("LOCAL_FIRST_ROUTER")
    saved_done = os.environ.get("LOCAL_FIRST_DONE")
    saved_record = os.environ.get("LOCAL_FIRST_RECORD")
    saved_jev = os.environ.get("JEV_DISABLE")
    os.environ.pop("LOCAL_FIRST_ROUTER", None)
    os.environ.pop("LOCAL_FIRST_DONE", None)
    os.environ["LOCAL_FIRST_RECORD"] = "0"
    os.environ["JEV_DISABLE"] = "1"

    def check(name: str, ok: bool) -> None:
        cases.append((name, ok))
        print(("ok " if ok else "FAIL ") + name)

    stakes, _ = classify_stakes("Summarize this log in one sentence: aa bb cc started")
    check("low summary", stakes == "low")
    stakes, _ = classify_stakes("deploy the change to production and email the team")
    check("high deploy", stakes == "high")
    task = "Summarize this log: router started\n\n## STANDING RULE\ncommit and push the stack"
    check("footer does not raise stakes", classify_stakes(task)[0] == "low")
    check("unsure bare", classify_stakes("help me with this")[0] == "unsure")
    low = policy_decision("Summarize this log line for me please", '{"decision":"complete_local","confidence":0.9,"why":"short","answer":"The router started and wrote a one-line summary."}')
    check("low completes", low["decision"] == "complete_local" and low["paid_invoked"] is False)
    high = policy_decision("implement the function and deploy it to production", '{"decision":"complete_local","confidence":0.99,"why":"done","answer":"Shipped the production deploy already."}')
    check("high never completes locally", high["decision"] == "need_premium" and high["answer"] == "")
    check("brief header stable", high["brief"].startswith(BRIEF_HEADER))
    unsure = policy_decision("look at this thing", "I don't know what you want from that.")
    check("unsure escalates", unsure["decision"] == "need_premium")
    weak = policy_decision("Summarize this log for me now", "ok")
    check("weak low escalates", weak["decision"] == "need_premium")
    action, args = plan_grok(["-p", "Summarize this log in one sentence please"], low)
    check("plan local skips grok", action == "local" and args == [])
    action, args = plan_grok(["-m", "paid", "-p", "implement the function"], high)
    check("plan escalate keeps model flag", action == "escalate" and "-m" in args and "paid" in args)
    check("plan escalate has brief", action == "escalate" and "[LOCAL-FIRST BRIEF v1]" in args[-1])
    off = os.environ.get("LOCAL_FIRST_ROUTER")
    os.environ["LOCAL_FIRST_ROUTER"] = "0"
    try:
        action, args = plan_grok(["-p", "Summarize this log"], low)
        check("router off passthrough", action == "passthrough" and args == ["-p", "Summarize this log"])
    finally:
        if off is None:
            os.environ.pop("LOCAL_FIRST_ROUTER", None)
        else:
            os.environ["LOCAL_FIRST_ROUTER"] = off
    claude_prompt = ["-p", "--model", "opus", "--output-format", "text", "Summarize this log in one sentence"]
    check("claude print prompt", extract_prompt("claude", claude_prompt)["text"] == "Summarize this log in one sentence")
    check("claude tui passthrough", extract_prompt("claude", ["Summarize this log"], stdout_is_tty=True)["kind"] == "none")
    check("claude command passthrough", extract_prompt("claude", ["doctor"])["kind"] == "none")
    codex_prompt = ["exec", "--sandbox", "read-only", "Summarize this log in one sentence"]
    check("codex exec prompt", extract_prompt("codex", codex_prompt)["text"] == "Summarize this log in one sentence")
    check("codex tui passthrough", extract_prompt("codex", ["Summarize this log"])["kind"] == "none")
    cursor_prompt = ["--print", "--force", "--trust", "--output-format", "text", "--model", "paid", "--workspace", "/tmp/ws", "Summarize this log in one sentence"]
    check("cursor print prompt", extract_prompt("cursor-agent", cursor_prompt)["text"] == "Summarize this log in one sentence")
    check("cursor tui passthrough", extract_prompt("cursor-agent", ["Summarize this log"], stdout_is_tty=True)["kind"] == "none")
    check("cursor login passthrough", extract_prompt("cursor-agent", ["login"])["kind"] == "none")
    fake_low = {"ok": True, "text": '{"decision":"complete_local","confidence":0.9,"why":"short","answer":"The router started and wrote a one-line summary."}', "backend": "fake"}
    fake_high = {"ok": True, "text": '{"decision":"complete_local","confidence":0.99,"why":"done","answer":"Shipped the production deploy already and mailed the team."}', "backend": "fake"}
    cfg = {"transport": "inject", "extra_high_substrings": []}
    local_plan = prepare_intercept("claude", ["-p", "Summarize this log in one sentence please"], fetch=lambda _p: fake_low, config=cfg, stdout_is_tty=True)
    check("claude local does not escalate", local_plan["action"] == "local")
    high_plan = prepare_intercept("codex", ["exec", "implement the function and deploy it to production"], fetch=lambda _p: fake_high, config=cfg, stdout_is_tty=True)
    check("codex high never local", high_plan["action"] == "escalate" and "[LOCAL-FIRST BRIEF v1]" in " ".join(high_plan["argv"]))
    cursor_high = prepare_intercept("cursor-agent", ["--print", "implement the function and deploy it to production"], fetch=lambda _p: fake_high, config=cfg, stdout_is_tty=True)
    check("cursor high never local", cursor_high["action"] == "escalate")
    called = {"n": 0}

    def boom(_prompt):
        called["n"] += 1
        return fake_low

    structured = prepare_intercept("claude", ["-p", "--output-format", "json", "Summarize this log"], fetch=boom, config=cfg, stdout_is_tty=True)
    check("json mode passthrough", structured["action"] == "passthrough" and called["n"] == 0)
    if saved_router is None:
        os.environ.pop("LOCAL_FIRST_ROUTER", None)
    else:
        os.environ["LOCAL_FIRST_ROUTER"] = saved_router
    if saved_done is None:
        os.environ.pop("LOCAL_FIRST_DONE", None)
    else:
        os.environ["LOCAL_FIRST_DONE"] = saved_done
    if saved_record is None:
        os.environ.pop("LOCAL_FIRST_RECORD", None)
    else:
        os.environ["LOCAL_FIRST_RECORD"] = saved_record
    if saved_jev is None:
        os.environ.pop("JEV_DISABLE", None)
    else:
        os.environ["JEV_DISABLE"] = saved_jev
    failed = [name for name, ok in cases if not ok]
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--grok-intercept":
        return run_grok_intercept(argv[1:])
    if argv and argv[0] == "--intercept" and len(argv) > 1:
        return run_intercept(argv[1], argv[2:])
    if argv and argv[0] == "--openai-serve":
        return serve_openai(argv[1:])
    if argv and argv[0] == "--selftest":
        return _selftest()
    execute = False
    as_json = False
    dry = False
    task_parts: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
        elif a == "--dry-run":
            dry = True
        elif a == "--execute-escalate":
            execute = True
        elif a in {"-p", "--single", "--prompt-file"} and i + 1 < len(argv):
            task_parts.append(_read_prompt(argv[i + 1], "file" if a == "--prompt-file" else "inline"))
            i += 1
        elif a == "--help":
            sys.stdout.write(
                "usage: local-first-route [--json] [--dry-run] [--execute-escalate] [-p TEXT | --prompt-file FILE | TEXT]\n"
                "exit 0 complete_local, 10 need_premium. --dry-run never launches a paid CLI.\n"
            )
            return 0
        else:
            task_parts.append(a)
        i += 1
    task = "\n".join(p for p in task_parts if p).strip()
    if not task:
        sys.stderr.write("local-first-route: need a task\n")
        return 2
    launched: list[str] = []

    def _paid(prompt: str) -> None:
        bin_path = real_grok_bin()
        fd, path = tempfile.mkstemp(prefix="local-first-", suffix=".md")
        os.write(fd, prompt.encode("utf-8"))
        os.close(fd)
        os.chmod(path, 0o600)
        env = os.environ.copy()
        env["LOCAL_FIRST_DONE"] = "1"
        subprocess.run([bin_path, "--prompt-file", path], env=env, check=False)

    hook = None
    if execute and not dry:
        hook = _paid
        os.environ.pop("LOCAL_FIRST_DRY", None)
    else:
        os.environ["LOCAL_FIRST_DRY"] = "1"
    result = route_task(task, caller="bot", execute_paid=hook)
    if dry:
        result["paid_invoked"] = False
    if as_json or result["decision"] != "complete_local":
        sys.stdout.write(json.dumps({k: result[k] for k in ("schema", "decision", "why", "confidence", "stakes", "brief", "answer", "paid_invoked", "backend")}, indent=2) + "\n")
    else:
        sys.stdout.write((result.get("answer") or "").rstrip() + "\n")
    if launched:
        pass
    return 0 if result["decision"] == "complete_local" else 10


if __name__ == "__main__":
    raise SystemExit(main())
