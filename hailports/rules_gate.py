#!/usr/bin/env python3
"""rules-gate: a regression gate for rules, lessons and agent-instruction files.

A proposed edit to a gated file lands only if replaying a fixed set of past questions
shows it helps more checks than it hurts. Stdlib only.

  rules-gate baseline [--replay PATH] [--rules PATH ...] [--offline] [--live]
  rules-gate check    --old PATH --new PATH [--replay PATH] [--target PATH]
  rules-gate apply    --target PATH (--new PATH | --stdin) [--replay PATH]
  rules-gate drain    re-run queued proposals in pending/
  rules-gate status

Exit codes: 0 accept / noop / baseline ok / status ok; 2 refuse (bad args, missing replay);
3 reject (helps <= hurts, locked rule with no safe only-if, or a previously rejected edit);
4 internal error; 5 skipped (model down or busy, edit queued in pending/, nothing landed).

Every check sorts into one bucket:
  working       pass old, pass new      (the hinted rule gets locked)
  broke         pass old, fail new
  helped        fail old, pass new
  never-solved  fail old, fail new
An edit is accepted only when helped > broke. A tie rejects. 0/0 rejects unless the edit is
a no-op. A locked rule is never deleted: the gate re-adds it with an "only if <condition>"
qualifier taken from the checks that locked it, or rejects when no condition is known.
Rejected edits are logged in rejected.jsonl and refused on sight afterwards.

Live chat must never wait on this module: chat_allowed() is fail-open and nothing on an
answer path should call check/apply.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RC_OK, RC_REFUSE, RC_REJECT, RC_ERROR, RC_SKIP = 0, 2, 3, 4, 5
STATUS_RC = {"accept": RC_OK, "noop": RC_OK, "reject": RC_REJECT, "duplicate": RC_REJECT,
             "skip": RC_SKIP, "refuse": RC_REFUSE, "error": RC_ERROR}
CHECK_TYPES = ("contains", "not_contains", "regex", "not_regex", "max_len", "min_len",
               "not_empty", "json_path")

DEFAULTS = {
    "replay": "~/.local/state/rules_gate/replay.json",
    "state": "~/.local/state/rules_gate",
    "gated": [],
    "prompt_files": [],
    "model_url": "http://127.0.0.1:8080/v1",
    "model_name": "",
    "probe_timeout_s": 2.0,
    "request_timeout_s": 120.0,
    "max_tokens": 400,
    "rules_char_cap": 24000,
    "writer_timeout_s": 900.0,
    "nice": 10,
    "interactive_lock": "",
    "fail_open_chat": True,
    "tz": "",
}


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local engine: never via a proxy


class ModelDown(Exception):
    """Model unreachable, busy, or interactive chat holds the lock. Never land an edit."""


# ------------------------------------------------------------------ config
def default_config_path() -> Path | None:
    env = os.environ.get("RULES_GATE_CONFIG")
    if env:
        return Path(os.path.expanduser(env))
    return None


def _expand(p: str, home: str | None) -> str:
    p = str(p)
    if home and (p == "~" or p.startswith("~/")):
        return str(Path(home) / p[2:]) if p != "~" else home
    return os.path.expanduser(p)


def load_config(path: str | Path | None = None, home: str | None = None, **over) -> dict:
    """home: expand "~" against this dir instead of $HOME (writers run under a test HOME)."""
    cfg = dict(DEFAULTS)
    p = Path(os.path.expanduser(str(path))) if path else default_config_path()
    if p and p.is_file():
        cfg.update(json.loads(p.read_text()))
    for k in ("replay", "state", "model_url"):
        env = os.environ.get("RULES_GATE_" + k.upper())
        if env:
            cfg[k] = env
    cfg.update({k: v for k, v in over.items() if v is not None})
    for k in ("replay", "state", "interactive_lock"):
        if cfg.get(k):
            cfg[k] = _expand(cfg[k], home)
    cfg["gated"] = [_expand(x, home) for x in cfg.get("gated", [])]
    cfg["prompt_files"] = [_expand(x, home) for x in cfg.get("prompt_files", [])]
    return cfg


def state_dir(cfg: dict) -> Path:
    d = Path(cfg["state"])
    for sub in ("pending", "baselines", "receipts", "cache"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def now_ct(cfg: dict | None = None) -> str:
    tz = (cfg or DEFAULTS).get("tz") or ""
    try:
        if not tz:
            raise ValueError("local time")
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")
    except Exception:
        return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def norm_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def rule_lines(text: str) -> list[str]:
    return [norm_line(l) for l in text.splitlines() if norm_line(l)]


def rule_id(line: str) -> str:
    return sha(norm_line(line).lower())[:12]


def is_gated(cfg: dict, target: str | Path) -> bool:
    try:
        t = Path(os.path.expanduser(str(target))).resolve()
    except OSError:
        return False
    for g in cfg.get("gated", []):
        try:
            if Path(g).resolve() == t:
                return True
        except OSError:
            continue
    return False


def _append_jsonl(path: Path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def audit(cfg: dict, event: str, **kw):
    _append_jsonl(state_dir(cfg) / "AUDIT.jsonl", {"ts": now_ct(cfg), "event": event, **kw})


# ------------------------------------------------------------------ replay + checks
def load_replay(path: str | Path) -> list[dict]:
    p = Path(os.path.expanduser(str(path)))
    if not p.is_file():
        raise FileNotFoundError(f"replay set not found: {p}")
    data = json.loads(p.read_text())
    cases = data["cases"] if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases:
        raise ValueError("replay set is empty or not a list")
    for i, c in enumerate(cases):
        if not c.get("id") or not c.get("question") or not c.get("checks"):
            raise ValueError(f"replay case {i} missing id/question/checks")
        for ch in c["checks"]:
            if ch.get("type") not in CHECK_TYPES:
                raise ValueError(f"case {c['id']}: unknown check type {ch.get('type')!r}")
    return cases


def _json_path(answer: str, spec: str) -> bool:
    path, _, want = spec.partition("=")
    m = re.search(r"[\[{].*[\]}]", answer, re.S)
    if not m:
        return False
    try:
        node = json.loads(m.group(0))
    except ValueError:
        return False
    for part in [p for p in path.strip().split(".") if p]:
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return False
    return True if not want else str(node).strip().lower() == want.strip().lower()


def run_check(check: dict, answer: str | None) -> bool:
    a = answer or ""
    t, v = check["type"], check.get("value", "")
    case_sens = bool(check.get("case"))
    flags = re.S | (0 if case_sens else re.I)
    if t == "contains":
        return (v in a) if case_sens else (str(v).lower() in a.lower())
    if t == "not_contains":
        return (v not in a) if case_sens else (str(v).lower() not in a.lower())
    if t == "regex":
        return re.search(v, a, flags) is not None
    if t == "not_regex":
        return re.search(v, a, flags) is None
    if t == "max_len":
        return len(a.strip()) <= int(v)
    if t == "min_len":
        return len(a.strip()) >= int(v)
    if t == "not_empty":
        return bool(a.strip())
    if t == "json_path":
        return _json_path(a, str(v))
    raise ValueError(f"unknown check type {t}")


def grade(cases: list[dict], answers: dict) -> dict:
    """answers: case_id -> answer text (None = no answer). Returns {case_id: {check_id: bool}}."""
    out = {}
    for c in cases:
        if c["id"] not in answers:
            continue
        out[c["id"]] = {ch["id"]: run_check(ch, answers[c["id"]]) for ch in c["checks"]}
    return out


def score(results: dict) -> tuple[int, int]:
    flat = [v for r in results.values() for v in r.values()]
    return sum(flat), len(flat)


# ------------------------------------------------------------------ model
class SplashModel:
    """OpenAI-compatible local engine. Low priority, single-flight, yields to interactive chat."""

    is_live = True

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = cfg["model_url"].rstrip("/")
        self.name = cfg.get("model_name") or ""
        self._lock_fd = None
        self._niced = False

    def probe(self) -> bool:
        try:
            with _OPENER.open(self.base + "/models", timeout=float(self.cfg["probe_timeout_s"])) as r:
                data = json.loads(r.read().decode() or "{}")
            if not self.name:
                ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
                self.name = ids[0] if ids else "default"
            return True
        except Exception:
            return False

    def _interactive_busy(self) -> bool:
        lp = self.cfg.get("interactive_lock")
        if not lp or not os.path.exists(lp):
            return False
        try:
            pid = int(Path(lp).read_text().strip().split()[0])
            os.kill(pid, 0)
            return True
        except (ValueError, IndexError, ProcessLookupError):
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def acquire(self, state: Path):
        if not self._niced and self.cfg.get("nice"):
            try:
                os.nice(int(self.cfg["nice"]))
            except OSError:
                pass
            self._niced = True
        if self._lock_fd is None:
            fd = os.open(str(state / "model.lock"), os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise ModelDown("another rules-gate replay holds the model lock")
            self._lock_fd = fd

    def release(self):
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            finally:
                self._lock_fd = None

    def __call__(self, rules_text: str, rules_hash: str, case: dict) -> str:
        if self._interactive_busy():
            raise ModelDown("interactive chat holds the model; replay aborted")
        body = json.dumps({
            "model": self.name or "default",
            "messages": [{"role": "system", "content": rules_text},
                         {"role": "user", "content": case["question"]}],
            "max_tokens": int(self.cfg["max_tokens"]), "temperature": 0, "stream": False,
        }).encode()
        req = urllib.request.Request(self.base + "/chat/completions", data=body,
                                     headers={"content-type": "application/json"})
        try:
            with _OPENER.open(req, timeout=float(self.cfg["request_timeout_s"])) as r:
                data = json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise ModelDown(f"model request failed: {type(e).__name__}")
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        return (msg.get("content") or "").strip()


class Replayer:
    """Answers each case under a rules text, cached by (rules_hash, case_id)."""

    def __init__(self, cfg: dict, model=None, deadline: float | None = None):
        self.cfg = cfg
        self.state = state_dir(cfg)
        self.model = model
        self.deadline = deadline

    def _cache_path(self, rules_hash: str) -> Path:
        return self.state / "cache" / f"{rules_hash[:24]}.json"

    def answers(self, rules_text: str, cases: list[dict]) -> dict:
        rh = sha(rules_text)
        cp = self._cache_path(rh)
        cache = json.loads(cp.read_text()) if cp.exists() else {}
        missing = [c for c in cases if c["id"] not in cache]
        if missing:
            if self.model is None:
                raise ModelDown("no model configured")
            if getattr(self.model, "is_live", False):
                if not self.model.probe():
                    raise ModelDown("model endpoint down")
                self.model.acquire(self.state)
            for c in missing:
                if self.deadline and time.time() > self.deadline:
                    raise ModelDown("replay deadline reached")
                cache[c["id"]] = self.model(rules_text, rh, c)
                cp.write_text(json.dumps(cache, ensure_ascii=False))
        return {c["id"]: cache[c["id"]] for c in cases}


# ------------------------------------------------------------------ rules bundle
def bundle(cfg: dict, override: dict | None = None) -> str:
    """The system prompt a replay sees: prompt_files (with overrides) capped per file."""
    override = {str(Path(k).resolve()): v for k, v in (override or {}).items()}
    files = list(cfg.get("prompt_files") or [])
    for k in override:
        if not any(str(Path(f).resolve()) == k for f in files):
            files.append(k)
    cap = int(cfg.get("rules_char_cap") or 24000)
    parts = []
    for f in files:
        key = str(Path(f).resolve())
        if key in override:
            text = override[key]
        else:
            try:
                text = Path(f).read_text(errors="replace")
            except OSError:
                continue
        if len(text) > cap:
            text = text[-cap:] if f.endswith(".jsonl") else text[:cap]
        parts.append(f"### {Path(f).name}\n{text}")
    return "\n\n".join(parts)


# ------------------------------------------------------------------ locks + rejected
def load_locks(cfg: dict) -> dict:
    p = state_dir(cfg) / "locked_rules.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_locks(cfg: dict, locks: dict):
    p = state_dir(cfg) / "locked_rules.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(locks, indent=1, ensure_ascii=False))
    tmp.replace(p)


def _hints(check: dict) -> list[str]:
    h = check.get("rule_hint")
    if not h:
        return []
    return [x for x in ([h] if isinstance(h, str) else h) if x]


def lock_working(cfg: dict, target: str, text: str, cases: list[dict], results: dict,
                 still_present: str | None = None) -> list[str]:
    """Lock every rule line in `text` that a passing check points at via rule_hint."""
    locks = load_locks(cfg)
    lines = rule_lines(text)
    present = set(rule_lines(still_present)) if still_present is not None else None
    added = []
    tkey = str(Path(target).resolve()) if target else ""
    for c in cases:
        for ch in c["checks"]:
            if not results.get(c["id"], {}).get(ch["id"]):
                continue
            for hint in _hints(ch):
                for line in lines:
                    if hint.lower() not in line.lower():
                        continue
                    if present is not None and line not in present:
                        continue
                    rid = rule_id(line)
                    key = f"{tkey}::{rid}"
                    row = locks.get(key) or {"id": rid, "file": tkey, "text": line[:300], "checks": [], "only_if": []}
                    ref = f"{c['id']}:{ch['id']}"
                    if ref not in row["checks"]:
                        row["checks"].append(ref)
                    cond = ch.get("only_if") or c.get("only_if")
                    if cond and cond not in row["only_if"]:
                        row["only_if"].append(cond)
                    if key not in locks:
                        added.append(rid)
                    locks[key] = row
    save_locks(cfg, locks)
    return added


def deleted_locked(cfg: dict, target: str, old: str, new: str) -> list[dict]:
    tkey = str(Path(target).resolve())
    new_set = {l.lower() for l in rule_lines(new)}
    old_set = {l.lower() for l in rule_lines(old)}
    out = []
    for row in load_locks(cfg).values():
        if row.get("file") != tkey:
            continue
        t = row["text"].lower()
        if t in old_set and t not in new_set and not any(t in n and "only if" in n for n in new_set):
            out.append(row)
    return out


def only_if_rewrite(old: str, new: str, rows: list[dict]) -> str | None:
    """Re-add each deleted locked rule with an 'only if' qualifier. None when no safe condition."""
    if any(not r.get("only_if") for r in rows):
        return None
    out = new.splitlines()
    old_lines = old.splitlines()
    for r in rows:
        cond = " or ".join(r["only_if"][:3])
        orig = next((l for l in old_lines if norm_line(l).lower() == r["text"].lower()), r["text"])
        qualified = f"{orig.rstrip().rstrip('.')} -- only if {cond}."
        idx = old_lines.index(orig) if orig in old_lines else -1
        anchor = old_lines[idx - 1] if idx > 0 else None
        pos = out.index(anchor) + 1 if anchor in out else len(out)
        out.insert(pos, qualified)
    return "\n".join(out) + ("\n" if new.endswith("\n") or not new else "")


def delta_hash(old: str, new: str) -> str:
    o, n = set(rule_lines(old)), set(rule_lines(new))
    return sha(json.dumps({"add": sorted(n - o), "del": sorted(o - n)}))


def was_rejected(cfg: dict, dh: str, nh: str) -> dict | None:
    for row in _read_jsonl(state_dir(cfg) / "rejected.jsonl"):
        if row.get("delta_hash") == dh or row.get("new_hash") == nh:
            return row
    return None


# ------------------------------------------------------------------ core compare
def buckets(cases: list[dict], old_r: dict, new_r: dict) -> dict:
    b = {"working": [], "broke": [], "helped": [], "never_solved": []}
    for c in cases:
        for ch in c["checks"]:
            o = old_r.get(c["id"], {}).get(ch["id"])
            n = new_r.get(c["id"], {}).get(ch["id"])
            ref = f"{c['id']}:{ch['id']}"
            if o and n:
                b["working"].append(ref)
            elif o and not n:
                b["broke"].append(ref)
            elif n and not o:
                b["helped"].append(ref)
            else:
                b["never_solved"].append(ref)
    return b


def counts(b: dict) -> dict:
    return {k: len(v) for k, v in b.items()}


def _pending(cfg: dict, target: str, new: str, reason: str, source: str) -> Path:
    p = state_dir(cfg) / "pending" / f"{int(time.time())}-{sha(new)[:12]}.json"
    p.write_text(json.dumps({"ts": now_ct(cfg), "target": target, "new_text": new, "reason": reason,
                             "source": source, "new_hash": sha(new)}, ensure_ascii=False))
    return p


def _in_tmp(path: Path) -> bool:
    try:
        return str(path.resolve()).startswith(str(Path(tempfile.gettempdir()).resolve()))
    except OSError:
        return False


def evaluate(cfg: dict, target: str, old: str, new: str, *, model=None, cases=None,
             write: bool = False, source: str = "cli", deadline: float | None = None) -> dict:
    """GUARD comparison of one edit. Returns a result dict with status + counts.
    status: accept | noop | reject | duplicate | skip."""
    state = state_dir(cfg)
    target = str(Path(os.path.expanduser(target)).resolve())
    if cases is None:
        cases = load_replay(cfg["replay"])
    res = {"target": target, "old_hash": sha(old), "new_hash": sha(new), "source": source}
    if norm_line(old) == norm_line(new) and rule_lines(old) == rule_lines(new):
        res["status"] = "noop"
        audit(cfg, "noop", **res)
        return res
    dh = delta_hash(old, new)
    res["delta_hash"] = dh
    prior = was_rejected(cfg, dh, sha(new))
    if prior:
        res.update(status="duplicate", reason=f"same edit was rejected at {prior.get('ts')}")
        audit(cfg, "rejected_duplicate", **res)
        return res
    proposal = new
    rows = deleted_locked(cfg, target, old, proposal)
    if rows:
        rewritten = only_if_rewrite(old, proposal, rows)
        if rewritten is None:
            return _reject(cfg, res, old, new, {}, "deletes a locked rule and no safe only-if condition is known")
        proposal, res["only_if_rewrite"] = rewritten, [r["id"] for r in rows]
    rep = Replayer(cfg, model, deadline)
    try:
        old_r = grade(cases, rep.answers(bundle(cfg, {target: old}), cases))
        new_r = grade(cases, rep.answers(bundle(cfg, {target: proposal}), cases))
        lock_working(cfg, target, old, cases, old_r, still_present=proposal)
        b = buckets(cases, old_r, new_r)
        if b["broke"]:
            # a regressing deletion of a rule the broken checks point at: re-add it with only-if
            lock_working(cfg, target, old, cases, old_r)
            rows = deleted_locked(cfg, target, old, proposal)
            if rows:
                rewritten = only_if_rewrite(old, proposal, rows)
                if rewritten is None:
                    return _reject(cfg, res, old, new, b, "regressing deletion of a locked rule; no safe only-if")
                proposal = rewritten
                res.setdefault("only_if_rewrite", []).extend(r["id"] for r in rows)
                new_r = grade(cases, rep.answers(bundle(cfg, {target: proposal}), cases))
                b = buckets(cases, old_r, new_r)
    except ModelDown as e:
        res.update(status="skip", reason=str(e), pending=str(_pending(cfg, target, new, str(e), source)))
        audit(cfg, "skip", **{k: v for k, v in res.items()})
        return res
    finally:
        if getattr(model, "is_live", False):
            model.release()
    res["counts"] = counts(b)
    res["broke"], res["helped"] = b["broke"][:50], b["helped"][:50]
    c = res["counts"]
    if c["helped"] <= c["broke"]:
        why = "no evidence of improvement (0 helped, 0 broke)" if c["helped"] == c["broke"] == 0 \
            else f"helped {c['helped']} <= broke {c['broke']}"
        return _reject(cfg, res, old, new, b, why)
    lock_working(cfg, target, proposal, cases, new_r)
    res["status"] = "accept"
    res["final_hash"] = sha(proposal)
    if write:
        tp = Path(target)
        if tp.exists() and not _in_tmp(tp):
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            (tp.parent / f"{tp.name}.bak-rulesgate-{stamp}").write_text(old)
        tmp = tp.with_name(tp.name + ".rulesgate.tmp")
        tmp.write_text(proposal)
        tmp.replace(tp)
        res["written"] = True
    receipt = {"ts": now_ct(cfg), "verdict": "accept", "target": target, "sha256": sha(proposal),
               "old_hash": sha(old), "counts": c, "source": source}
    (state / "receipts" / f"{sha(proposal)}.json").write_text(json.dumps(receipt, indent=1))
    res["receipt"] = str(state / "receipts" / f"{sha(proposal)}.json")
    res["proposal"] = proposal
    audit(cfg, "accept", **{k: v for k, v in res.items() if k != "proposal"})
    return res


def _reject(cfg, res, old, new, b, why) -> dict:
    o, n = set(rule_lines(old)), set(rule_lines(new))
    excerpt = {"added": sorted(n - o)[:8], "removed": sorted(o - n)[:8]}
    row = {"ts": now_ct(cfg), "target": res["target"], "delta_hash": res.get("delta_hash") or delta_hash(old, new),
           "new_hash": sha(new), "reason": why, "counts": counts(b) if b else {}, "excerpt": excerpt,
           "source": res.get("source")}
    _append_jsonl(state_dir(cfg) / "rejected.jsonl", row)
    res.update(status="reject", reason=why, counts=row["counts"])
    audit(cfg, "reject", **{k: v for k, v in res.items()})
    return res


def has_receipt(cfg: dict, text_or_hash: str) -> bool:
    h = text_or_hash if re.fullmatch(r"[0-9a-f]{64}", text_or_hash or "") else sha(text_or_hash)
    p = state_dir(cfg) / "receipts" / f"{h}.json"
    if not p.exists():
        return False
    try:
        return json.loads(p.read_text()).get("verdict") == "accept"
    except ValueError:
        return False


# ------------------------------------------------------------------ fail-open helpers
def safe_call(fn, *a, timeout: float = 2.0, default=None, **kw):
    """Run fn in a daemon thread; return default on any exception or timeout. Never raises."""
    box = {"v": default}

    def run():
        try:
            box["v"] = fn(*a, **kw)
        except BaseException:
            box["v"] = default
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return box["v"] if not t.is_alive() else default


def chat_allowed(*_a, **_kw) -> bool:
    """Live chat is never gated. Always True, even if config or state is broken."""
    return True


# ------------------------------------------------------------------ baseline
def recorded_answers(cases: list[dict]) -> dict:
    return {c["id"]: c.get("recorded_answer") for c in cases if c.get("recorded_answer") is not None}


def baseline(cfg: dict, *, offline: bool, model=None, rules: list[str] | None = None, cases=None) -> dict:
    cases = cases if cases is not None else load_replay(cfg["replay"])
    if rules:
        cfg = dict(cfg, prompt_files=[os.path.expanduser(r) for r in rules])
    if offline:
        ans = recorded_answers(cases)
        method = "offline-from-recorded-answers"
        rtext = None
    else:
        rtext = bundle(cfg)
        ans = Replayer(cfg, model).answers(rtext, cases)
        method = "live-model"
    res = grade(cases, ans)
    p, t = score(res)
    per_type: dict = {}
    for c in cases:
        for ch in c["checks"]:
            v = res.get(c["id"], {}).get(ch["id"])
            if v is None:
                continue
            row = per_type.setdefault(ch["type"], [0, 0])
            row[0] += int(v)
            row[1] += 1
    cases_pass = sum(1 for r in res.values() if r and all(r.values()))
    out = {"ts": now_ct(cfg), "method": method, "cases": len(cases), "cases_graded": len(res),
           "cases_all_pass": cases_pass, "checks_pass": p, "checks_total": t,
           "working": p, "never_solved_or_failing": t - p, "per_type": per_type,
           "rules_hash": sha(rtext) if rtext else None}
    for f in cfg.get("prompt_files") or []:
        try:
            lock_working(cfg, f, Path(f).read_text(errors="replace"), cases, res)
        except OSError:
            pass
    out["locked_rules"] = len(load_locks(cfg))
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (state_dir(cfg) / "baselines" / f"{stamp}-{method}.json").write_text(json.dumps(
        dict(out, results=res), indent=1))
    audit(cfg, "baseline", **out)
    return out


# ------------------------------------------------------------------ apply (writer entry point)
def apply_edit(target: str, new_text: str, *, cfg: dict | None = None, model="auto",
               source: str = "api", timeout_s: float | None = None, cases=None) -> dict:
    """Gate one edit and write it only on accept. Never raises: errors come back as status 'error'."""
    try:
        cfg = cfg or load_config()
        tp = Path(os.path.expanduser(target))
        old = tp.read_text(errors="replace") if tp.exists() else ""
        if model == "auto":
            model = SplashModel(cfg)
        deadline = time.time() + float(timeout_s) if timeout_s else None
        return evaluate(cfg, str(tp), old, new_text, model=model, cases=cases, write=True,
                        source=source, deadline=deadline)
    except FileNotFoundError as e:
        return {"status": "refuse", "reason": str(e), "target": target}
    except Exception as e:  # noqa: BLE001 - writers must never crash on the gate
        return {"status": "error", "reason": f"{type(e).__name__}: {e}", "target": target}


# ------------------------------------------------------------------ CLI
def _print(res: dict):
    print(json.dumps({k: v for k, v in res.items() if k not in ("proposal", "results")}, indent=1))


def cmd_baseline(a, cfg) -> int:
    rc = RC_OK
    if a.offline or not a.live:
        _print(baseline(cfg, offline=True, rules=a.rules))
    if a.live:
        m = SplashModel(cfg)
        if not m.probe():
            print("rules-gate: live replay skipped (model endpoint down)", file=sys.stderr)
            return RC_SKIP
        try:
            _print(baseline(cfg, offline=False, model=m, rules=a.rules))
        except ModelDown as e:
            print(f"rules-gate: live replay skipped ({e})", file=sys.stderr)
            rc = RC_SKIP
        finally:
            m.release()
    return rc


def cmd_check(a, cfg) -> int:
    old = Path(a.old).read_text(errors="replace")
    new = Path(a.new).read_text(errors="replace")
    target = a.target or a.new
    res = evaluate(cfg, target, old, new, model=SplashModel(cfg), source="check")
    _print(res)
    return STATUS_RC[res["status"]]


def cmd_apply(a, cfg) -> int:
    new = sys.stdin.read() if a.stdin else Path(a.new).read_text(errors="replace")
    res = apply_edit(a.target, new, cfg=cfg, source="apply")
    _print(res)
    return STATUS_RC.get(res["status"], RC_ERROR)


def cmd_drain(a, cfg) -> int:
    worst = RC_OK
    for p in sorted((state_dir(cfg) / "pending").glob("*.json")):
        row = json.loads(p.read_text())
        res = apply_edit(row["target"], row["new_text"], cfg=cfg, source="drain:" + row.get("source", ""))
        _print(res)
        if res["status"] != "skip":
            p.rename(p.with_suffix(".done"))
        else:
            worst = RC_SKIP
            break
    return worst


def cmd_status(a, cfg) -> int:
    st = state_dir(cfg)
    bl = sorted((st / "baselines").glob("*.json"))
    last = json.loads(bl[-1].read_text()) if bl else {}
    out = {"replay": cfg["replay"], "replay_cases": None, "state": str(st),
           "gated": cfg["gated"], "locked_rules": len(load_locks(cfg)),
           "rejected": len(_read_jsonl(st / "rejected.jsonl")),
           "pending": len(list((st / "pending").glob("*.json"))),
           "receipts": len(list((st / "receipts").glob("*.json"))),
           "last_baseline": {k: last.get(k) for k in ("ts", "method", "checks_pass", "checks_total")} if last else None,
           "model_up": SplashModel(cfg).probe()}
    try:
        out["replay_cases"] = len(load_replay(cfg["replay"]))
    except Exception as e:  # noqa: BLE001
        out["replay_cases"] = f"error: {e}"
    print(json.dumps(out, indent=1))
    return RC_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rules-gate", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="exit: 0 ok/accept, 2 refuse, 3 reject, 4 error, 5 skipped (queued)")
    ap.add_argument("--config", help="JSON config (default $RULES_GATE_CONFIG)")
    ap.add_argument("--replay", help="replay set path")
    ap.add_argument("--state", help="state dir")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("baseline", help="score current rules against the replay set")
    s.add_argument("--rules", nargs="*", help="rules files for the prompt (default: config prompt_files)")
    s.add_argument("--offline", action="store_true", help="grade recorded answers, no model call")
    s.add_argument("--live", action="store_true", help="also replay against the local model if it is up")
    s = sub.add_parser("check", help="compare old vs new rules text (no write)")
    s.add_argument("--old", required=True)
    s.add_argument("--new", required=True)
    s.add_argument("--target", help="the gated file this edit is for (default: --new)")
    s.add_argument("--ruleset", nargs="*", help="(compat) ignored; use config prompt_files")
    s = sub.add_parser("apply", help="gate an edit and write the target only on accept")
    s.add_argument("--target", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--new")
    g.add_argument("--stdin", action="store_true")
    sub.add_parser("drain", help="re-run queued proposals in pending/")
    sub.add_parser("status", help="counts, last baseline, model up/down")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return RC_OK if e.code == 0 else RC_REFUSE
    if not a.cmd:
        ap.print_help()
        return RC_REFUSE
    try:
        cfg = load_config(a.config, replay=a.replay, state=a.state)
        return {"baseline": cmd_baseline, "check": cmd_check, "apply": cmd_apply,
                "drain": cmd_drain, "status": cmd_status}[a.cmd](a, cfg)
    except (FileNotFoundError, ValueError) as e:
        print(f"rules-gate: refuse: {e}", file=sys.stderr)
        return RC_REFUSE
    except Exception as e:  # noqa: BLE001
        print(f"rules-gate: internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return RC_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
