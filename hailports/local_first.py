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
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"transport": "http", "http_url": os.environ.get("LOCAL_FIRST_URL", "http://127.0.0.1:8080/v1/chat/completions")}


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
    priority = 0 if caller in {"grok", "cli", "cursor"} else 2
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
        result = policy_decision(task, str(local.get("text") or ""), extra_high=extra, backend=backend)
        result["usage"] = local.get("usage") or {}
        result["elapsed_s"] = local.get("elapsed_s")
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


def real_grok_bin(cfg: dict | None = None) -> str:
    env = os.environ.get("GROK_REAL_BIN", "").strip()
    if env:
        return env
    cfg = cfg if cfg is not None else load_config()
    configured = str(cfg.get("paid_bin") or "").strip()
    if configured:
        return configured
    return str(Path.home() / ".grok" / "bin" / "grok")


def run_grok_intercept(argv: list[str]) -> int:
    if any(a in {"-h", "--help", "--version"} for a in argv):
        os.execv(real_grok_bin(), [real_grok_bin(), *argv])
    if router_disabled():
        bin_path = real_grok_bin()
        os.execv(bin_path, [bin_path, *argv])
    prompt, kind, _idx = _extract_prompt(argv)
    if kind == "none":
        bin_path = real_grok_bin()
        os.execv(bin_path, [bin_path, *argv])
    task = _read_prompt(prompt, kind)
    if not task.strip():
        bin_path = real_grok_bin()
        os.execv(bin_path, [bin_path, *argv])
    # A prompt that is already an escalation brief must not loop.
    if task.startswith("[LOCAL-FIRST BRIEF v1]"):
        os.environ["LOCAL_FIRST_DONE"] = "1"
        bin_path = real_grok_bin()
        os.execv(bin_path, [bin_path, *argv])
    result = route_task(task, caller="grok", execute_paid=None)
    action, new_argv = plan_grok(argv, result)
    if action == "local":
        sys.stdout.write((result.get("answer") or "").rstrip() + "\n")
        sys.stderr.write(
            f"[local-first] decision=complete_local paid_invoked=false backend={result.get('backend')}\n"
        )
        return 0
    if action == "passthrough":
        new_argv = list(argv)
    os.environ["LOCAL_FIRST_DONE"] = "1"
    bin_path = real_grok_bin()
    sys.stderr.write(
        f"[local-first] decision=need_premium paid_invoked=true backend={result.get('backend')} why={result.get('why')}\n"
    )
    os.execv(bin_path, [bin_path, *new_argv])
    return 127


def _selftest() -> int:
    cases = []

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
    failed = [name for name, ok in cases if not ok]
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--grok-intercept":
        return run_grok_intercept(argv[1:])
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
