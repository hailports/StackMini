#!/usr/bin/env python3
"""Generic watch tick: run check -> optional :8080 judge -> dedupe -> alert/notify.

Never sends email or chat. Fail-open: if the local model is down, skip judgment
and only alert when the check script itself says need=true with high certainty
(need=true and bot set, or need=true and force=true).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _home() -> Path:
    return Path.home()


def alert_dir() -> Path:
    env = os.environ.get("WATCH_ALERT_DIR", "").strip()
    if env:
        return Path(env)
    return _home() / ".local" / "share" / "local-first" / "watch-alerts"


def state_dir() -> Path:
    env = os.environ.get("WATCH_STATE_DIR", "").strip()
    if env:
        return Path(env)
    return _home() / ".local" / "share" / "local-first" / "watch-state"


def model_url() -> str:
    return os.environ.get("WATCH_MODEL_URL", "http://127.0.0.1:8080/v1/chat/completions").strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_check(script: Path, args: list[str]) -> dict:
    try:
        proc = subprocess.run(
            [str(script), *args],
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("WATCH_CHECK_TIMEOUT", "60")),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"need": False, "summary": f"check_error:{exc}", "bot": None, "ok": False}
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return {"need": False, "summary": f"check_exit:{proc.returncode}", "bot": None, "ok": False}
    try:
        data = json.loads(raw.splitlines()[-1] if raw else "{}")
    except json.JSONDecodeError:
        return {"need": False, "summary": "check_bad_json", "bot": None, "ok": False}
    if not isinstance(data, dict):
        return {"need": False, "summary": "check_not_object", "bot": None, "ok": False}
    data["ok"] = True
    data["need"] = bool(data.get("need"))
    data["summary"] = str(data.get("summary") or "").strip() or "unnamed"
    data["bot"] = data.get("bot")
    data["dedupe"] = str(data.get("dedupe") or data["summary"])
    data["force"] = bool(data.get("force"))
    return data


# A green result that matches the last run needs no model call. The judge
# only runs when the check says something needs action, asks to force, or the result changed. Watches
# listed in WATCH_JUDGE_ON_CHANGE (comma list; default local-model-ready) use this gate.
def _judge_gate_names() -> set[str]:
    raw = os.environ.get("WATCH_JUDGE_ON_CHANGE", "local-model-ready")
    return {x.strip() for x in raw.split(",") if x.strip()}


def judge_needed(name: str, check: dict) -> bool:
    """True when the judge should run. Records the result so the next run can compare."""
    if name not in _judge_gate_names():
        return True
    sig = hashlib.sha256(json.dumps({"need": bool(check.get("need")), "force": bool(check.get("force")),
                                     "summary": str(check.get("summary") or "")}, sort_keys=True).encode()).hexdigest()
    path = state_dir() / f"{name}.lastresult"
    try:
        prev = path.read_text(encoding="utf-8").strip()
    except OSError:
        prev = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sig + "\n", encoding="utf-8")
    except OSError:
        return True
    if check.get("need") or check.get("force"):
        return True
    return sig != prev


def local_judge(summary: str) -> dict | None:
    """Cheap yes/no. Returns None on failure (fail-open to check script alone)."""
    body = {
        "model": os.environ.get("WATCH_MODEL", "local"),
        "temperature": 0,
        "max_tokens": 80,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Reply with one JSON object only. Keys: need (bool), why (short string). "
                    "need=true only if a human or bot must act now. No secrets."
                ),
            },
            {"role": "user", "content": summary[:2000]},
        ],
    }
    req = urllib.request.Request(
        model_url(),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Chat-Priority": "0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(os.environ.get("WATCH_MODEL_TIMEOUT", "20"))) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    try:
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return {"need": bool(obj.get("need")), "why": str(obj.get("why") or "")[:200]}


def dedupe_hit(name: str, key: str, ttl_s: int) -> bool:
    path = state_dir() / f"{name}.dedupe.json"
    state_dir().mkdir(parents=True, exist_ok=True)
    now = time.time()
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    digest = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:24]
    prev = data.get(digest)
    if isinstance(prev, (int, float)) and now - float(prev) < ttl_s:
        return True
    data[digest] = now
    # prune
    data = {k: v for k, v in data.items() if isinstance(v, (int, float)) and now - float(v) < ttl_s * 4}
    path.write_text(json.dumps(data), encoding="utf-8")
    return False


def write_alert(name: str, payload: dict) -> Path:
    dest = alert_dir() / f"{name}.jsonl"
    alert_dir().mkdir(parents=True, exist_ok=True)
    row = {"ts": utc_now(), "name": name, **payload}
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return dest


def notify_bot(payload: dict) -> None:
    cmd = os.environ.get("WATCH_NOTIFY_CMD", "").strip()
    if not cmd:
        # reference box: agent-bus enqueue when present
        enqueue = _home() / "claude-stack" / "bin" / "agent-bus-enqueue.py"
        if enqueue.is_file():
            job = {
                "goal": str(payload.get("summary") or "watch alert"),
                "lane": "local",
                "success_criteria": "bot acknowledged watch alert",
                "payload": payload,
                "created_by": "watch-runner",
                "dry_run": False,
            }
            subprocess.run(
                [sys.executable, str(enqueue), "-"],
                input=json.dumps(job),
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
        return
    subprocess.run(
        cmd,
        shell=True,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        sys.stderr.write("usage: runner.py <name> <check-script> [args...]\n")
        return 2
    name = argv[0].strip()
    script = Path(argv[1]).expanduser()
    if not name or not script.is_file():
        sys.stderr.write("watch runner: bad name or script\n")
        return 2
    ttl = int(os.environ.get("WATCH_DEDUPE_TTL", "1800"))
    check = run_check(script, argv[2:])
    if not check.get("ok"):
        return 0
    need = bool(check.get("need"))
    judged = local_judge(str(check.get("summary") or "")) if judge_needed(name, check) else None
    if judged is not None:
        # local model can veto a soft need; cannot invent need=false into true unless check said force
        if need and not check.get("force") and judged.get("need") is False:
            need = False
        if (not need) and judged.get("need") and check.get("force"):
            need = True
        check["judge"] = judged
    elif need and not (check.get("bot") or check.get("force")):
        # model down + soft need -> skip (fail quiet)
        return 0
    if not need:
        return 0
    key = str(check.get("dedupe") or check.get("summary") or name)
    if dedupe_hit(name, key, ttl):
        return 0
    path = write_alert(name, {
        "need": True,
        "summary": check.get("summary"),
        "bot": check.get("bot"),
        "judge": check.get("judge"),
    })
    if check.get("bot"):
        notify_bot({
            "name": name,
            "summary": check.get("summary"),
            "bot": check.get("bot"),
            "alert": str(path),
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
