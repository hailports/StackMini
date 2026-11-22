#!/usr/bin/env python3
"""job_harness: contract + progress + wrapper-owned exits for long agent jobs.

A job is a folder with CONTRACT.md, PROGRESS.md, prompt.md. The wrapper (not the
prompt) decides when the job is done:

  green           done_when command exits 0 AND HANDOFF.md is valid (STATUS complete)
  same error x N  stop, write PROGRESS.md, escalate      (N = budget max_same_error, default 2)
  budget spent    stop, escalate                         (max_wall_s, max_attempts, max_tokens)
  wall touched    stop, write PROGRESS.md, escalate      (protected path changed mid-run)

Exit codes: 0 green, 2 refused to start, 3 escalated, 4 internal error.
Stdlib only. Host-specific defaults (never-list, protected paths, agent CLIs)
come from a host config JSON, never from this file.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REQUIRED = ("goal", "output", "scope", "never", "done_when", "budget")
OPTIONAL = ("prompt", "runner", "judge", "agents")
KEYS = REQUIRED + OPTIONAL
HANDOFF_HEADINGS = ("TASK", "STATUS", "OUTPUT", "SOURCES", "NEXT OWNER",
                    "DECISIONS", "UNCERTAINTIES", "DO-NOT-ASSUME")
STATUS_VALUES = ("complete", "partial", "blocked")
TRIVIAL_DONE = {"true", ":", "exit 0", "/usr/bin/true", "/bin/true", "echo", "echo ok"}
RC_OK, RC_REFUSE, RC_ESCALATE, RC_INTERNAL = 0, 2, 3, 4
HERE = Path(__file__).resolve().parent


def markers() -> tuple[str, str]:
    """Escalation marker files: (human marker, generic marker)."""
    return os.environ.get("JOB_HARNESS_HUMAN_MARKER", "NEEDS_HUMAN"), "ESCALATED"


class Refuse(Exception):
    pass


# ---------------------------------------------------------------- utilities
def now_ct() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = os.environ.get("JOB_HARNESS_TZ")
        if tz:
            return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        pass
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def die(msg: str, rc: int) -> int:
    print(f"job-run: {msg}", file=sys.stderr)
    return rc


def jobs_root() -> Path:
    return Path(os.path.expanduser(os.environ.get("JOB_HARNESS_JOBS_ROOT", "~/jobs")))


def templates_dir(root: Path) -> Path:
    for cand in (os.environ.get("JOB_HARNESS_TEMPLATES"), root / "_templates", HERE / "templates"):
        if cand and (Path(cand) / "CONTRACT.md").exists():
            return Path(cand)
    return HERE / "templates"


def load_host(root: Path) -> dict:
    """Host config: {"never": [..], "protected": [globs], "agents": [[argv..],..]}."""
    for cand in (os.environ.get("JOB_HARNESS_HOST_CONFIG"), root / "_templates" / "host_defaults.json"):
        if cand and Path(cand).exists():
            return json.loads(Path(cand).read_text())
    return {"never": [], "protected": [], "agents": []}


def resolve_job(arg: str) -> Path:
    p = Path(os.path.expanduser(arg))
    if p.is_dir():
        return p.resolve()
    return (jobs_root() / arg).resolve()


def tail(text: str, n: int = 20) -> str:
    return "\n".join(text.rstrip().splitlines()[-n:])


# ---------------------------------------------------------------- contract
def parse_contract(text: str) -> dict:
    """`key   value` at column 0; indented lines continue the previous key."""
    fields: dict[str, str] = {}
    cur = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or line.startswith("CONTRACT"):
            continue
        m = re.match(r"^([a-z_]+)\s*(.*)$", line)
        if m and not line[0].isspace() and m.group(1) in KEYS:
            cur = m.group(1)
            fields[cur] = m.group(2).strip()
        elif cur and line[0].isspace():
            fields[cur] = (fields[cur] + "\n" + line.strip()).strip()
    return fields


def parse_budget(text: str) -> dict:
    out = {}
    for k, v in re.findall(r"\b(max_wall_s|max_attempts|max_tokens|max_same_error)\s*=\s*(\d+)", text):
        out[k] = int(v)
    return out


def done_when_cmd(text: str) -> str:
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    return re.sub(r"^(command|cmd)\s*:\s*", "", first, flags=re.I).strip().strip("`")


def check_contract(job: Path, host: dict) -> tuple[dict, list[str]]:
    """Return (contract, injected_nevers). Raise Refuse with a clear reason."""
    cp = job / "CONTRACT.md"
    if not cp.exists():
        raise Refuse(f"no CONTRACT.md in {job} (copy the template: job-run new <name>)")
    c = parse_contract(cp.read_text())
    missing = [k for k in REQUIRED if not c.get(k, "").strip()]
    if missing:
        raise Refuse(f"CONTRACT.md missing or empty field(s): {', '.join(missing)}")
    cmd = done_when_cmd(c["done_when"])
    if not cmd or cmd.lower() in TRIVIAL_DONE or "<" in cmd:
        raise Refuse(f"done_when must be a real command that can come back red, got: {cmd!r}")
    try:
        argv0 = shlex.split(cmd)[0]
    except ValueError as e:
        raise Refuse(f"done_when does not parse as a shell command: {e}")
    exe = os.path.expanduser(argv0)
    if not (shutil.which(exe) or Path(exe).exists() or (job / exe).exists()):
        raise Refuse(f"done_when command not found: {argv0}")
    for a in shlex.split(cmd)[1:]:
        if a.endswith((".sh", ".py")) and not a.startswith("-"):
            ap_ = Path(os.path.expanduser(a))
            if not (ap_ if ap_.is_absolute() else job / ap_).exists():
                raise Refuse(f"done_when script not found: {a}")
    b = parse_budget(c["budget"])
    if not b.get("max_wall_s") or not (b.get("max_attempts") or b.get("max_tokens")):
        raise Refuse("budget needs max_wall_s AND (max_attempts or max_tokens), e.g. max_wall_s=3600 max_attempts=3")
    c["_budget"] = b
    c["_done_cmd"] = cmd
    injected = [n for n in host.get("never", []) if n not in c["never"]]
    if injected:
        c["never"] += "\n" + "\n".join(f"(host default) {n}" for n in injected)
    return c, injected


# ---------------------------------------------------------------- handoff
def check_handoff(job: Path) -> tuple[bool, str, str]:
    """Return (valid, status, reason)."""
    hp = job / "HANDOFF.md"
    if not hp.exists() or not hp.read_text().strip():
        return False, "", "HANDOFF.md missing or empty"
    lines = hp.read_text().splitlines()
    pat = re.compile(r"^(?:#+\s*)?(" + "|".join(re.escape(h) for h in HANDOFF_HEADINGS) + r")(?=\s|$)(.*)$")
    found: dict[str, list[str]] = {}
    cur = None
    for line in lines:
        m = pat.match(line.rstrip())
        if m and not line[:1].isspace():
            cur = m.group(1)
            found.setdefault(cur, [])
            rest = m.group(2).strip(" :\t")
            if rest:
                found[cur].append(rest)
        elif cur and line.strip():
            found[cur].append(line.strip())
    missing = [h for h in HANDOFF_HEADINGS if h not in found]
    if missing:
        return False, "", f"HANDOFF.md missing heading(s): {', '.join(missing)}"
    empty = [h for h in HANDOFF_HEADINGS if not found[h]]
    if empty:
        return False, "", f"HANDOFF.md empty section(s): {', '.join(empty)}"
    status = found["STATUS"][0].lstrip("-* ").split()[0].lower().strip(".,")
    if status not in STATUS_VALUES:
        return False, status, f"HANDOFF.md STATUS must be one of {'/'.join(STATUS_VALUES)}, got {status!r}"
    if status == "complete":
        out = found["OUTPUT"][0].lstrip("-* ").split()[0]
        op = Path(os.path.expanduser(out))
        if not op.is_absolute():
            op = job / op
        if not op.exists() and not re.match(r"^https?://", out):
            return False, status, f"HANDOFF.md OUTPUT path does not exist: {out}"
    return True, status, "ok"


# ---------------------------------------------------------------- walls
def wall_paths(c: dict, host: dict) -> list[Path]:
    pats = list(host.get("protected", []))
    pats += re.findall(r"(?:~|/)[\w./*\-]+", c.get("never", ""))
    out = set()
    for pat in pats:
        for hit in glob.glob(os.path.expanduser(pat)):
            p = Path(hit)
            if p.is_file() and ".bak" not in p.name:
                out.add(p)
    return sorted(out)


def snapshot(paths: list[Path]) -> dict:
    snap = {}
    for p in paths:
        try:
            snap[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            snap[str(p)] = "unreadable"
    return snap


def changed_walls(before: dict) -> list[str]:
    return [p for p, h in snapshot([Path(x) for x in before]).items() if h != before[p]]


def scope_changes(c: dict, since: float, limit: int = 4000) -> list[str]:
    out, seen = [], 0
    for root in re.findall(r"(?:~|/)[\w./\-]+", c.get("scope", "")):
        base = Path(os.path.expanduser(root.rstrip("/*")))
        if not base.exists():
            continue
        it = [base] if base.is_file() else base.rglob("*")
        for f in it:
            seen += 1
            if seen > limit:
                return out + ["(scope walk truncated)"]
            if ".git" in f.parts or "__pycache__" in f.parts:
                continue
            try:
                if f.is_file() and f.stat().st_mtime >= since:
                    out.append(str(f))
            except OSError:
                pass
    return sorted(set(out))[:200]


# ---------------------------------------------------------------- rules-gate hook
def _rules_gate():
    """Lazy import of rules_gate (sibling tools/rules_gate, or hailports.rules_gate). None if absent."""
    if os.environ.get("JOB_HARNESS_RULES_GATE", "1") == "0":
        return None
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "rules_gate", here.parent.parent / "hailports"):
        if (cand / "rules_gate.py").exists() and str(cand) not in sys.path:
            sys.path.insert(0, str(cand))
    try:
        import rules_gate  # type: ignore
        return rules_gate
    except Exception:
        return None


def rules_gate_snapshot(c: dict, job_dir: Path) -> dict:
    """Hash every gated rules/lessons file under this job's scope; keep a copy for `rules-gate check --old`."""
    rg = _rules_gate()
    if rg is None:
        return {}
    try:
        cfg = rg.load_config()
    except Exception:
        return {}
    roots = [Path(os.path.expanduser(r.rstrip("/*"))).resolve() for r in re.findall(r"(?:~|/)[\w./\-]+", c.get("scope", ""))]
    snap = {}
    for g in cfg.get("gated", []):
        gp = Path(g)
        try:
            rp = gp.resolve()
        except OSError:
            continue
        if not any(rp == r or r in rp.parents for r in roots):
            continue
        text = gp.read_text(errors="replace") if gp.is_file() else ""
        keep = job_dir / ".rules_gate_snap" / (hashlib.sha256(str(rp).encode()).hexdigest()[:10] + "-" + gp.name)
        keep.parent.mkdir(exist_ok=True)
        keep.write_text(text)
        snap[str(gp)] = {"sha": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(), "old": str(keep)}
    return {"cfg": cfg, "files": snap} if snap else {}


def rules_gate_unreceipted(snap: dict) -> list[str]:
    """Gated files that changed during the run without a rules-gate accept receipt for their new content."""
    rg = _rules_gate()
    if not snap or rg is None:
        return []
    out = []
    for path, row in snap["files"].items():
        p = Path(path)
        text = p.read_text(errors="replace") if p.is_file() else ""
        h = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        if h == row["sha"]:
            continue
        try:
            ok = rg.has_receipt(snap["cfg"], h)
        except Exception:
            ok = False
        if not ok:
            out.append(f"{path} (run: rules-gate check --old {row['old']} --new {path} --target {path})")
    return out


# ---------------------------------------------------------------- job state
class Job:
    def __init__(self, path: Path, host: dict):
        self.path, self.host = path, host
        self.audit_p = path / "AUDIT.log"

    def audit(self, event: str, **kw):
        rec = {"ts": now_ct(), "event": event, **kw}
        with self.audit_p.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def progress_note(self, lines: list[str]):
        p = self.path / "PROGRESS.md"
        body = p.read_text() if p.exists() else f"# PROGRESS  {self.path.name}\n"
        body = body.rstrip() + f"\n\nharness  {now_ct()}\n" + "".join(f"  {l}\n" for l in lines)
        p.write_text(body)

    def escalate(self, reason: str, sig: str = "", detail: str = "") -> int:
        text = (f"ts        {now_ct()}\njob       {self.path}\nreason    {reason}\n"
                f"signature {sig or '-'}\ndetail    {detail.strip()[:400] or '-'}\n"
                f"progress  {self.path / 'PROGRESS.md'}\naudit     {self.audit_p}\n")
        for name in markers():
            (self.path / name).write_text(text)
        idx = self.path.parent / "_escalated"
        idx.mkdir(exist_ok=True)
        with (idx / "INDEX.md").open("a") as fh:
            fh.write(f"- {now_ct()} | {self.path.name} | {reason} | sig {sig or '-'} | {self.path / 'ESCALATED'}\n")
        self.progress_note([f"STOP  {reason}", f"sig   {sig or '-'}", f"last  {detail.strip().splitlines()[-1][:200] if detail.strip() else '-'}",
                            f"escalated -> {' / '.join(markers())}"])
        self.audit("escalate", reason=reason, sig=sig)
        print(f"job-run: ESCALATED {self.path.name}: {reason} (sig {sig or '-'})", file=sys.stderr)
        return RC_ESCALATE

    # lock: one wrapper per job
    def lock(self) -> bool:
        lp = self.path / ".job-run.lock"
        try:
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode()); os.close(fd)
            return True
        except FileExistsError:
            try:
                pid = int(lp.read_text().strip() or 0)
                os.kill(pid, 0)
                return False
            except (ValueError, ProcessLookupError):
                lp.unlink(missing_ok=True)
                return self.lock()
            except PermissionError:
                return False

    def unlock(self):
        (self.path / ".job-run.lock").unlink(missing_ok=True)


def signature(parts: list[str]) -> str:
    norm = "\n".join(parts)
    norm = re.sub(r"\b[0-9a-f]{7,}\b", "H", norm)
    norm = re.sub(r"\d+(\.\d+)?", "N", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


def build_prompt(job: Path, c: dict, attempt: int, last_fail: str) -> str:
    def rd(n):
        p = job / n
        return p.read_text().strip() if p.exists() else "(none)"
    pf = job / (c.get("prompt") or "prompt.md").split()[0]
    task = pf.read_text() if pf.exists() else "(no prompt file; work from CONTRACT.md)"
    never = c["never"]
    return f"""# HARNESS (job-run)  attempt {attempt}
You are running inside job-run. The WRAPPER decides done, not you.
Done = `{c['_done_cmd']}` exits 0 AND HANDOFF.md is valid with STATUS complete.
Keep PROGRESS.md current (done / current / decisions / dead ends / blocked) so a fresh session can resume.
Write HANDOFF.md with exact headings: {' / '.join(HANDOFF_HEADINGS)}. STATUS is one of complete, partial, blocked.
If you are blocked, set STATUS blocked and say why; do not claim complete.
NEVER (contract + host defaults):
{never}
Last failure seen by the wrapper: {last_fail or '(none)'}

## CONTRACT.md
{rd('CONTRACT.md')}

## PROGRESS.md
{rd('PROGRESS.md')}

## HANDOFF.md (previous)
{rd('HANDOFF.md')}

## TASK ({pf.name})
{task}
"""


def agent_chain(job: Path, c: dict, host: dict) -> list[list[str]]:
    env_cmd = os.environ.get("JOB_HARNESS_AGENT_CMD")
    if env_cmd:
        return [shlex.split(env_cmd)]
    if c.get("runner"):
        return [shlex.split(os.path.expanduser(c["runner"]))]
    rs = job / "run.sh"
    if rs.exists() and "job-run" not in rs.read_text():
        return [["/bin/sh", str(rs)]]
    if c.get("agents"):
        return [shlex.split(x) for x in c["agents"].splitlines() if x.strip()]
    return [list(a) for a in host.get("agents", [])]


def run_agent(job: Job, chain: list[list[str]], prompt_file: Path, attempt: int, timeout: float, env: dict) -> tuple[int, str, int]:
    """Try each agent in order until one exits 0. Return (rc, output_tail, chars)."""
    if not chain:
        return 127, "no agent configured (host agents, contract agents, runner, or JOB_HARNESS_AGENT_CMD)", 0
    rc, out, chars = 1, "", 0
    deadline = time.time() + timeout
    for i, argv in enumerate(chain):
        left = deadline - time.time()
        if left <= 0:
            return 124, "wall budget spent before agent finished", chars
        has_ph = any("{prompt_file}" in a for a in argv)
        argv = [a.replace("{prompt_file}", str(prompt_file)) for a in argv]
        if not has_ph and not argv[0].endswith(("sh", "run.sh")):
            argv = argv + [prompt_file.read_text()]
        log = job.path / f"attempt-{attempt}.agent{i}.log"
        env = dict(env, JOB_PROMPT_FILE=str(prompt_file))
        t0 = time.time()
        try:
            with log.open("w") as fh:
                p = subprocess.run(argv, cwd=job.path, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   timeout=left, env=env)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = 124
        except FileNotFoundError as e:
            rc = 127
            log.write_text(f"agent not found: {e}\n")
        text = log.read_text(errors="replace")
        chars += len(text)
        out = tail(text, 15)
        job.audit("agent_exit", attempt=attempt, agent=i, argv0=argv[0], rc=rc, secs=round(time.time() - t0, 1), log=log.name)
        if rc == 0 or rc == 124:
            break
    return rc, out, chars


def run_done_when(job: Job, c: dict, timeout: float) -> tuple[int, str]:
    t0 = time.time()
    try:
        p = subprocess.run(c["_done_cmd"], shell=True, cwd=job.path, capture_output=True, text=True,
                           timeout=max(5, timeout), stdin=subprocess.DEVNULL)
        rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        rc, out = 124, "done_when timed out"
    with (job.path / "DONE_WHEN.log").open("a") as fh:
        fh.write(f"=== {now_ct()} rc={rc} secs={time.time() - t0:.1f} cmd={c['_done_cmd']}\n{tail(out, 20)}\n")
    job.audit("done_when", rc=rc)
    return rc, out


def run_judge(job: Job, c: dict, judge_cmd: str, chain: list[list[str]], timeout: float) -> tuple[bool, str]:
    if judge_cmd:
        try:
            p = subprocess.run(judge_cmd, shell=True, cwd=job.path, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            ok, out = p.returncode == 0, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            ok, out = False, "judge timed out"
    else:
        if not chain:
            return False, "no judge command and no agent to act as judge"
        prompt = (f"You are a separate reviewer. Look for problems. Check the job in {job.path} against its CONTRACT.md "
                  f"(goal, output, scope, never, done_when). Read HANDOFF.md and the output. Do not edit files. "
                  f"Last line must be exactly VERDICT: PASS or VERDICT: FAIL <reason>.")
        try:
            p = subprocess.run(chain[0] + [prompt], cwd=job.path, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            out = (p.stdout or "") + (p.stderr or "")
            ok = p.returncode == 0 and "VERDICT: PASS" in out and "VERDICT: FAIL" not in out
        except subprocess.TimeoutExpired:
            ok, out = False, "judge timed out"
    (job.path / "JUDGE.log").open("a").write(f"=== {now_ct()} ok={ok}\n{tail(out, 20)}\n")
    job.audit("judge", ok=ok)
    return ok, out


# ---------------------------------------------------------------- commands
def cmd_check(args) -> int:
    root = jobs_root()
    job = resolve_job(args.job)
    try:
        c, inj = check_contract(job, load_host(root))
    except Refuse as e:
        return die(f"REFUSE: {e}", RC_REFUSE)
    print(f"ok  {job.name}  done_when={c['_done_cmd']}  budget={c['_budget']}  injected_nevers={len(inj)}")
    return RC_OK


def cmd_run(args) -> int:
    root = jobs_root()
    path = resolve_job(args.job)
    if not path.is_dir():
        return die(f"REFUSE: job folder not found: {path}", RC_REFUSE)
    if int(os.environ.get("JOB_HARNESS_DEPTH", "0")) > 0:
        return die("REFUSE: job-run called from inside a job-run agent (recursion)", RC_REFUSE)
    host = load_host(root)
    job = Job(path, host)
    try:
        c, injected = check_contract(path, host)
    except Refuse as e:
        try:
            job.audit("refuse", reason=str(e))
        except OSError:
            pass
        return die(f"REFUSE: {e}", RC_REFUSE)
    if not job.lock():
        return die(f"REFUSE: another job-run holds {path / '.job-run.lock'}", RC_REFUSE)
    try:
        return _run_locked(job, c, injected, args)
    finally:
        job.unlock()


def _run_locked(job: Job, c: dict, injected: list[str], args) -> int:
    b = c["_budget"]
    max_wall = b["max_wall_s"]
    max_att = b.get("max_attempts") or 10**6
    max_tok = b.get("max_tokens") or 0
    max_same = b.get("max_same_error") or 2
    for m in markers():
        mp = job.path / m
        if mp.exists():
            mp.rename(job.path / f"{m}.prev")
    if args.prompt:
        c["prompt"] = args.prompt
    sigs: list[str] = []
    last_fail, tokens, attempt = "", 0, 0
    judge_cmd = args.judge if args.judge else (c.get("judge", "").split("\n")[0] if c.get("judge", "").startswith("cmd:") else "")
    judge_cmd = judge_cmd[4:].strip() if judge_cmd.startswith("cmd:") else judge_cmd
    want_judge = args.judge is not None or bool(judge_cmd)
    t_start = time.time()
    job.audit("start", pid=os.getpid(), done_when=c["_done_cmd"], budget=b, prompt=c.get("prompt") or "prompt.md",
              injected_nevers=injected, judge=want_judge, judge_cmd=judge_cmd or ("default-cli" if want_judge else ""))
    walls = snapshot(wall_paths(c, job.host))
    job.audit("walls", count=len(walls))
    rg_snap = rules_gate_snapshot(c, job.path)
    job.audit("rules_gate", gated_in_scope=len(rg_snap.get("files", {})))
    chain = agent_chain(job.path, c, job.host)
    env = dict(os.environ, JOB_HARNESS_DEPTH="1", JOB_DIR=str(job.path))

    while True:
        elapsed = time.time() - t_start
        if attempt >= max_att:
            return job.escalate("budget spent: max_attempts", sigs[-1] if sigs else "", last_fail)
        if elapsed >= max_wall:
            return job.escalate("budget spent: max_wall_s", sigs[-1] if sigs else "", last_fail)
        if max_tok and tokens >= max_tok:
            return job.escalate("budget spent: max_tokens (estimate chars/4)", sigs[-1] if sigs else "", last_fail)
        attempt += 1
        pf = job.path / f".prompt.attempt-{attempt}.md"
        pf.write_text(build_prompt(job.path, c, attempt, last_fail))
        tokens += len(pf.read_text()) // 4
        rc, out, chars = run_agent(job, chain, pf, attempt, max_wall - (time.time() - t_start), env)
        tokens += chars // 4
        hit = changed_walls(walls)
        if hit:
            job.audit("wall_changed", paths=hit)
            return job.escalate("wall: protected path changed during run", "wall", "\n".join(hit))
        changes = scope_changes(c, t_start)
        if changes:
            job.audit("scope_changes", count=len(changes), files=changes[:50])
        if rc == 124:
            return job.escalate("budget spent: max_wall_s (agent killed)", "timeout", out)
        drc, dout = run_done_when(job, c, max(30, max_wall - (time.time() - t_start)))
        hv, hstatus, hreason = check_handoff(job.path)
        job.audit("handoff", valid=hv, status=hstatus, reason=hreason)
        if hv and hstatus == "blocked":
            return job.escalate("agent reported STATUS blocked in HANDOFF.md", "blocked", hreason)
        green = drc == 0 and hv and hstatus == "complete"
        if green and rg_snap:
            unrec = rules_gate_unreceipted(rg_snap)
            if unrec:
                job.audit("rules_gate_unreceipted", files=unrec)
                green, drc = False, 1
                dout = "rules-gate: gated file changed without an accept receipt:\n" + "\n".join(unrec)
        if green and want_judge:
            ok, jout = run_judge(job, c, judge_cmd, chain, max(60, max_wall - (time.time() - t_start)))
            if not ok:
                green = False
                drc, dout = 1, "judge failed: " + tail(jout, 5)
        if green:
            (job.path / "DONE.json").write_text(json.dumps({"ts": now_ct(), "attempt": attempt, "done_when_rc": 0,
                                                            "handoff": "valid", "judge": want_judge}, indent=1))
            job.audit("green", attempt=attempt)
            job.progress_note([f"GREEN  attempt {attempt}: done_when rc=0, HANDOFF valid (STATUS complete)"])
            print(f"job-run: GREEN {job.path.name} (attempt {attempt})")
            return RC_OK
        why = []
        if rc != 0:
            why.append(f"agent rc={rc}: {tail(out, 3)}")
        if drc != 0:
            why.append(f"done_when rc={drc}: {tail(dout, 5)}")
        if not hv or hstatus != "complete":
            why.append(f"handoff: {hreason if not hv else 'STATUS ' + hstatus}")
        last_fail = "\n".join(why)
        sig = signature([f"drc={drc}", tail(dout, 5), hreason if not hv else hstatus])
        sigs.append(sig)
        job.audit("not_done", attempt=attempt, sig=sig, why=last_fail[:500])
        print(f"job-run: attempt {attempt} not done (sig {sig})", file=sys.stderr)
        if sigs.count(sig) >= max_same:
            return job.escalate(f"same error signature {max_same}x", sig, last_fail)


def cmd_new(args) -> int:
    root = jobs_root()
    dest = root / args.name
    if dest.exists():
        return die(f"REFUSE: {dest} already exists", RC_REFUSE)
    tpl = templates_dir(root)
    dest.mkdir(parents=True)
    host = load_host(root)
    for name in ("CONTRACT.md", "PROGRESS.md", "HANDOFF.md", "done_when.sh", "prompt.md"):
        src = tpl / name
        if not src.exists():
            continue
        text = src.read_text().replace("{{JOB}}", args.name).replace("{{JOB_DIR}}", str(dest))
        if name == "CONTRACT.md" and host.get("never"):
            text = text.replace("{{HOST_NEVER}}", "\n            ".join(host["never"]))
        else:
            text = text.replace("{{HOST_NEVER}}", "(host defaults are injected at start)")
        out = dest / (name + ".template" if name == "HANDOFF.md" else name)
        out.write_text(text)
        if name.endswith(".sh"):
            out.chmod(0o755)
    print(f"created {dest}  (edit CONTRACT.md + prompt.md, then: job-run start {args.name})")
    return RC_OK


def cmd_start(args) -> int:
    path = resolve_job(args.job)
    rc = cmd_check(args)
    if rc:
        return rc
    if not shutil.which("tmux"):
        return die("tmux not found; use `job-run run`", RC_REFUSE)
    sess = "job-" + re.sub(r"[^\w-]", "_", path.name)[:40]
    me = os.environ.get("JOB_HARNESS_SELF") or f"{sys.executable} {Path(__file__).resolve()}"
    extra = f" --judge {shlex.quote(args.judge)}" if args.judge else (" --judge" if args.judge == "" else "")
    inner = f"{me} run {shlex.quote(str(path))}{extra} >> {shlex.quote(str(path / 'wrapper.log'))} 2>&1"
    r = subprocess.run(["tmux", "new-session", "-d", "-s", sess, inner])
    if r.returncode:
        return die(f"tmux new-session failed rc={r.returncode} (session {sess} exists?)", RC_INTERNAL)
    print(f"started tmux session {sess}; log {path / 'wrapper.log'}")
    return RC_OK


def cmd_handoff(args) -> int:
    ok, status, reason = check_handoff(resolve_job(args.job))
    if not ok:
        return die(f"HANDOFF invalid: {reason}", 1)
    print(f"HANDOFF ok STATUS {status}")
    return RC_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="job-run", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    for name in ("run", "start"):
        s = sub.add_parser(name, help=f"{name} a job ({'foreground' if name == 'run' else 'in tmux'})")
        s.add_argument("job")
        s.add_argument("--judge", nargs="?", const="", default=None, help="run a separate judge; optional command")
        s.add_argument("--prompt", default=None, help="prompt file in the job dir (default: contract prompt or prompt.md)")
    s = sub.add_parser("check", help="validate CONTRACT.md only"); s.add_argument("job")
    s = sub.add_parser("handoff", help="validate HANDOFF.md only"); s.add_argument("job")
    s = sub.add_parser("new", help="scaffold a job from templates"); s.add_argument("name")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("run", "start", "check", "handoff", "new", "-h", "--help"):
        argv = ["run"] + argv
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help(sys.stderr)
        return RC_REFUSE
    try:
        return {"run": cmd_run, "start": cmd_start, "check": cmd_check, "handoff": cmd_handoff, "new": cmd_new}[args.cmd](args)
    except Exception as e:  # loud, never a silent 0
        return die(f"INTERNAL ERROR {type(e).__name__}: {e}", RC_INTERNAL)


if __name__ == "__main__":
    sys.exit(main())
