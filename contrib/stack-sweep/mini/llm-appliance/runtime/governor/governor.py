#!/usr/bin/env python3
"""Single memory governor for the 24GB Mini.

One heavy lane (embeds, evals, index rebuilds, training, ingest backfills,
discovery). Drafter, runner, and interactive `l` stay ahead of that lane.
Pause is SIGSTOP / SIGCONT only. Never signals llama-server :8080, :8078,
Edge bridges :18812/:18822/:18820, or OneDrive.

  governor.py daemon
  governor.py run <job> -- <cmd...>   # exit 0 if the lane refuses
  governor.py check
  governor.py once
  governor.py status
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
LOCK_DIR = HOME / "claude-stack" / "data" / "locks"
LOCK_PATH = LOCK_DIR / "heavy.lock"
META_PATH = LOCK_DIR / "heavy.meta.json"
STATE_PATH = HOME / "llm-appliance" / "runtime" / "governor" / "state.json"
LOG_PATH = HOME / "Library" / "Logs" / "claude-stack" / "memory-governor.log"
FLAG_OWNS = HOME / "claude-stack" / "data" / "flags" / "memory-governor-owns"
CHAT_FLAG = HOME / "llm-appliance" / "runtime" / "chatprio" / "foreground"
STOPPED_PATH = HOME / "llm-appliance" / "runtime" / "governor" / "stopped.json"

DB_PATH = HOME / "claude-stack" / "data" / "work_context.db"
_DB_HOLDERS: dict = {"t": 0.0, "pids": set()}


def _db_holder_pids() -> set[int]:
    """Pids with work_context.db open.

    SIGSTOP on one of these freezes the sqlite write lock until the busy
    timeout, so new notes never become searchable. Cached for 2s.
    """
    now = time.time()
    if now - float(_DB_HOLDERS["t"]) < 2.0:
        return _DB_HOLDERS["pids"]
    pids: set[int] = set()
    try:
        out = subprocess.run(
            ["lsof", "-t", str(DB_PATH)],
            capture_output=True, text=True, timeout=8,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.isdigit():
            pids.add(int(ln))
    _DB_HOLDERS["t"] = now
    _DB_HOLDERS["pids"] = pids
    return pids


# Jobs that must take the heavy lane. Matched against the full command line.
HEAVY_MARKERS = (
    "work_context_index.py",
    "core.work_rag",
    "work_rag",
    "work_brain_embed",
    "brain2_incremental",
    "brain2_golden",
    "brain2_dossier",
    "brain2_noise",
    "noise_prune.py",
    "realeval_nightly",
    "work_learn.py",
    "workbrain_onedrive_copy",
    "student_learn_cycle",
    "mtp_train/nightly",
    "mtp_train/gen.sh",
    "loadup/nightchain",
    "loadup-nptest",
    "peer_session_ingest",
    "ops_overnight.py",
    "drafter-nightly-score",
    "work_graph.py build",
    "work_graph.py",
    "github_workbrain_sync",
    "work_brain_cycle",
    "work_brain_zoom",
    "zoom_email_summary_sync",
    "gov-stress-heavy",
)

# Never stop or gate these. Sacred services plus the priority lane.
NEVER_MARKERS = (
    "mlx-qwen-llama-server",
    "llama-server",
    "minicpm_serve",
    "omlx_gate",
    "work-brain-ts-proxy",
    "work_brain_ts_proxy",
    "remote-debugging-port=18812",
    "remote-debugging-port=18822",
    "remote-debugging-port=18820",
    "OneDrive",
    "fileproviderd",
    "File Provider",
    "ops_draft_immortal",
    "ops_draft_runner",
    "work_loop.py",
    "drop-job-runner",
    "llm_agent.py",
    "agent-bus-runner",
    "governor.py",
    "memory-governor",
    "mini-memguard",
    "chat_priority",
    "sample-pressure",
    "grok_bot",
    "Grok Bot",
    "tailscaled",
    "cloudflared",
    "ai_egress",
    "automation_window_guard",
)

PRIORITY_CLIENTS = (
    "work_loop.py",
    "ops_draft_immortal",
    "ops_draft_runner",
    "llm_agent.py",
    "drop-job-runner",
    "imsg_qwen",
)


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%dT%H:%M:%S") + " " + msg
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def pressure_level() -> int:
    try:
        raw = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        return int(raw)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 99


def mem_snapshot() -> dict:
    level = pressure_level()
    swap = ""
    comp = 0
    try:
        swap = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        comp = int(subprocess.run(
            ["/usr/sbin/sysctl", "-n", "vm.compressor_bytes_used"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        comp = 0
    return {"level": level, "swap": swap, "compressor_bytes": comp, "ts": time.time()}


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def foreground_live(max_age_s: float = 20.0) -> bool:
    """Interactive `l` holds runtime/chatprio/foreground and heartbeats it."""
    try:
        if not CHAT_FLAG.is_file():
            return False
        age = time.time() - CHAT_FLAG.stat().st_mtime
        if age > max_age_s:
            return False
        pid = int(CHAT_FLAG.read_text().splitlines()[0].strip() or 0)
    except (OSError, ValueError, IndexError):
        return False
    return _pid_alive(pid)


def server_busy() -> bool:
    try:
        raw = subprocess.run(
            ["/usr/bin/curl", "-sf", "--max-time", "1", "http://127.0.0.1:8080/metrics"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        for ln in raw.splitlines():
            if ln.startswith("llamacpp:requests_processing "):
                return float(ln.split()[-1]) > 0
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return False


def established_clients() -> list[tuple[int, str]]:
    try:
        out = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-iTCP:8080", "-sTCP:ESTABLISHED"],
            capture_output=True, text=True, timeout=4,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    seen = set()
    for ln in out.splitlines()[1:]:
        parts = ln.split()
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        if parts[0].startswith("llama"):
            continue
        pid = int(parts[1])
        if pid in seen:
            continue
        seen.add(pid)
        cmd = _cmdline(pid)
        rows.append((pid, cmd))
    return rows


def priority_inflight() -> bool:
    """True when a chat or draft owns the only llama slot.

    Any in-flight completion counts: interactive `l`, the drafter, or the
    runner. New heavy work waits. CPU-heavy jobs already running are paused
    by the daemon so they do not allocate under that completion.
    """
    if foreground_live():
        return True
    return server_busy()


def _cmdline(pid: int) -> str:
    try:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _state(pid: int) -> str:
    try:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# Night mode (2026-10-19, operator: "max local burn, NOT paid"). Flag file NIGHT_MODE = ON.
# Inside 22:00-06:00 local, `run` and `check` WAIT (poll) up to NIGHT_WAIT_S for the lane
# instead of skipping on a busy model / level 2 / lane-busy, and refuse new admission
# above NIGHT_SWAP_MAX_MB (below swap_guard's 3072MB :8080 bounce). Pause rules in tick()
# are unchanged. Flag missing/OFF or outside the window = old behavior exactly.
NIGHT_FLAG = HOME / "claude-stack" / "data" / "flags" / "NIGHT_MODE"
NIGHT_WAIT_S = int(os.environ.get("GOV_NIGHT_WAIT_S", "1500"))
NIGHT_POLL_S = float(os.environ.get("GOV_NIGHT_POLL_S", "30"))
NIGHT_SWAP_MAX_MB = float(os.environ.get("GOV_NIGHT_SWAP_MAX_MB", "2400"))  # 2600->2400 2026-10-20 memstagger
# 2026-10-20 memstagger: stagger night admissions. A heavy job waits NIGHT_GAP_S after the
# previous heavy DONE, and the lane must look free on two polls NIGHT_SETTLE_S apart.
NIGHT_GAP_S = float(os.environ.get("GOV_NIGHT_GAP_S", "90"))
NIGHT_SETTLE_S = float(os.environ.get("GOV_NIGHT_SETTLE_S", "30"))


def _last_done() -> float:
    try:
        return float((LOCK_DIR / "heavy.last_done").read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _stamp_done() -> None:
    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        (LOCK_DIR / "heavy.last_done").write_text(f"{time.time():.0f}\n")
    except OSError:
        pass
NIGHT_START_H, NIGHT_END_H = 22, 6


def night_active(now: float | None = None) -> bool:
    try:
        if not NIGHT_FLAG.is_file() or NIGHT_FLAG.read_text().strip().upper() != "ON":
            return False
    except OSError:
        return False
    h = time.localtime(now).tm_hour
    return h >= NIGHT_START_H or h < NIGHT_END_H


def swap_used_mb() -> float:
    try:
        raw = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"],
                             capture_output=True, text=True, timeout=3).stdout
        part = raw.split("used =")[1].split()[0]
        return float(part.rstrip("M"))
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return 99999.0  # fail closed


def lane_free() -> bool:
    return pressure_level() <= 1 and not priority_inflight()


def _write_meta(name: str, pid: int) -> None:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps({
        "name": name,
        "pid": pid,
        "priority": 10,
        "started": time.time(),
    }) + "\n")


def _clear_meta() -> None:
    try:
        META_PATH.unlink()
    except FileNotFoundError:
        pass


class HeavyLane:
    """fcntl lock. Held by the waiter process for the life of the child."""

    def __init__(self) -> None:
        self.fh = None

    def try_enter(self, name: str, pid: int) -> bool:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        self.fh = LOCK_PATH.open("a+")
        try:
            import fcntl
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            self.fh = None
            return False
        _write_meta(name, pid)
        return True

    def update_pid(self, name: str, pid: int) -> None:
        _write_meta(name, pid)

    def leave(self) -> None:
        _clear_meta()
        if self.fh is not None:
            import fcntl
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.fh.close()
            self.fh = None


def cmd_check() -> int:
    if night_active():
        deadline = time.time() + NIGHT_WAIT_S
        while time.time() < deadline and not (lane_free() and swap_used_mb() <= NIGHT_SWAP_MAX_MB):
            time.sleep(NIGHT_POLL_S)
    snap = mem_snapshot()
    info = {
        "ok": lane_free(),
        "level": snap["level"],
        "priority_inflight": priority_inflight(),
        "foreground": foreground_live(),
        "server_busy": server_busy(),
        "holder": json.loads(META_PATH.read_text()) if META_PATH.exists() else None,
    }
    print(json.dumps(info))
    return 0 if info["ok"] else 75


def _child_env() -> dict:
    env = os.environ.copy()
    # Caps apply to the next natural run of index/embed/ingest children.
    env.setdefault("WORK_RAG_MAX_DOCS", "40")
    env.setdefault("WORK_RAG_MAX_CHUNKS", "80")
    env.setdefault("WCI_EMAIL_BODY_BATCHES", "4")
    env.setdefault("WCI_EMAIL_BODY_BUDGET", "40")
    env.setdefault("GOV_HEAVY", "1")
    return env


BIZ_FLAG = HOME / "claude-stack" / "data" / "flags" / "BIZHOURS_INGEST_DEFER"
BIZ_DEFER = ("safe-reindex", "allctx", "reingest", "reindex", "re-index", "embed_all", "backfill")
BIZ_LIGHT = ("ingest email", "ingest zoom", "ingest", "littlebird", "pull")


def biz_policy(command: list[str]) -> str:
    """'defer', 'light' or 'normal'. Weekdays 07:00-18:00 local. Flag OFF disables."""
    try:
        if BIZ_FLAG.is_file() and BIZ_FLAG.read_text().strip().upper() == "OFF":
            return "normal"
    except OSError:
        pass
    lt = time.localtime()
    if lt.tm_wday >= 5 or not (7 <= lt.tm_hour < 18):
        return "normal"
    cmd = " ".join(command).lower()
    if any(k in cmd for k in BIZ_DEFER):
        return "defer"
    if any(k in cmd for k in BIZ_LIGHT):
        return "light"
    return "normal"


def light_class(name: str, command: list[str]) -> bool:
    """True when name+command is a light ingest and not a defer job. Ignores the clock."""
    text = (name + " " + " ".join(command)).lower()
    if any(k in text for k in BIZ_DEFER):
        return False
    return any(k in text for k in BIZ_LIGHT)


def cheap_no_llm(name: str, command: list[str]) -> bool:
    """Ingest and embed. These do not call :8080.

    Memory-pressure warning (level 2) still runs them. Urgent/critical
    (level 4+) and the night swap check stay in cmd_run. :8080 work does not
    qualify, so the model cannot starve the index.
    """
    text = (name + " " + " ".join(command)).lower()
    heavy = ("work_learn", "work-learn", "enrich_model", "llm_agent", "minicpm", "session_insight")
    if any(k in text for k in heavy):
        return False
    if light_class(name, command):
        return True
    return any(k in text for k in ("embed", "brain2", "bge", "tag_new"))



def swap_total_mb() -> float:
    try:
        raw = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"],
                             capture_output=True, text=True, timeout=3).stdout
        part = raw.split("total =")[1].split()[0]
        return float(part.rstrip("M"))
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return 0.0


def cheap_blocked(level: int, swap_mb: float, total_mb: float) -> str:
    """Cheap ingest keeps running under pressure. Unreadable swap still fails closed.

    Near-full swap pauses idle_filler first. The batch cap keeps the ingest small.
    """
    del level
    if swap_mb >= 99999 or total_mb <= 0 or swap_mb < 0:
        return "swap"
    return ""


def _learning_job(name: str, command: list[str]) -> bool:
    text = (name + " " + " ".join(command)).lower()
    return any(k in text for k in ("work_learn", "work-learn", "idle_filler", "idle-filler", "ops_draft_judge", "draft-judge"))


def _learn_window() -> bool:
    path = HOME / "claude-stack" / "data" / "runtime" / "learn_fairshare_window.json"
    try:
        doc = json.loads(path.read_text())
        return float(doc.get("until") or 0) > time.time()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def _duty_cycle(proc, on_s: float = 0.4, off_s: float = 0.6) -> int:
    """~40% CPU cap via SIGSTOP/SIGCONT while the child runs."""
    import signal as _sig
    while True:
        try:
            return proc.wait(timeout=on_s)
        except subprocess.TimeoutExpired:
            pass
        if proc.pid in _db_holder_pids():
            if _state(proc.pid).startswith("T"):
                try:
                    os.kill(proc.pid, _sig.SIGCONT)
                except ProcessLookupError:
                    return proc.wait()
            continue
        try:
            os.kill(proc.pid, _sig.SIGSTOP); time.sleep(off_s); os.kill(proc.pid, _sig.SIGCONT)
        except ProcessLookupError:
            return proc.wait()


def _rubric_nightly(name: str, command: list[str]) -> bool:
    """The no-decode status rubric. It must start even when swap is over the line.

    Mail replay inside the script waits for an idle slot and swap under the heal line.
    """
    blob = (str(name) + " " + " ".join(str(x) for x in (command or []))).lower()
    return "realeval_nightly" in blob


def cmd_run(name: str, command: list[str]) -> int:
    if not command:
        print("no command", file=sys.stderr)
        return 2
    if _rubric_nightly(name, command):
        log(f"RUN {name} rubric-nightly")
        proc = subprocess.Popen(command, env=os.environ.copy())
        rc = proc.wait()
        log(f"DONE {name} rc={rc}")
        print(json.dumps({"ran": True, "rc": rc, "rubric": True}))
        return rc if isinstance(rc, int) else 1
    pol = biz_policy(command)
    if pol == "defer":
        log(f"DEFER {name} business-hours (runs after 18:00) cmd={' '.join(command)[:120]}")
        print(json.dumps({"ran": False, "reason": "business-hours-defer"}))
        return 0
    cheap_job = cheap_no_llm(name, command)
    if cheap_job:
        try:
            sys.path.insert(0, str(HOME / "claude-stack" / "tools"))
            import uptime_guard
            uptime_guard.arm_cheap()
        except Exception as exc:
            log(f"CHEAP_ARM {exc.__class__.__name__}")
    # The night 2400MB stagger is for heavy jobs. Cheap ingest ignores it.
    # Level 2 is not a stop. Urgent pressure and real swap danger still are.
    night = night_active() and not cheap_job
    deadline = time.time() + (NIGHT_WAIT_S if night else 0)
    waited = False
    priowait_logged = False
    ok_since = None
    while True:
        level = pressure_level()
        preempt = priority_inflight()
        prio_wait = False
        reason = None
        swap_mb = swap_used_mb() if (night or cheap_job) else 0.0
        # Level 2 is a warning: cheap ingest and embed still run. Level 4+
        # (urgent/critical) and a live draft still hold the heavy lane.
        cheap = cheap_job
        cheap_why = cheap_blocked(level, swap_mb, swap_total_mb()) if cheap else ""
        learn = _learning_job(name, command)
        window = _learn_window() if learn else False
        blocked_pressure = (
            bool(cheap_why)
            or ((not cheap) and (not learn) and level > 1)
            or (learn and level >= 4 and not window)
        )
        blocked_priority = preempt and not (cheap or light_class(name, command) or (learn and window))
        if blocked_pressure or blocked_priority:
            if cheap_why and level < 4 and not blocked_priority:
                reason = cheap_why
            else:
                reason = "pressure" if blocked_pressure else "priority"
            ok_since = None
        elif night and swap_mb > NIGHT_SWAP_MAX_MB:
            reason = "swap"
            ok_since = None
        elif night and time.time() - _last_done() < NIGHT_GAP_S:
            reason = "stagger"
        elif night and (ok_since is None or time.time() - ok_since < NIGHT_SETTLE_S):
            if ok_since is None:
                ok_since = time.time()
            reason = "settle"
        else:
            if preempt and not priowait_logged:
                log(f"PRIOWAIT {name} level={level} light job runs; ingester/daemon yields to draft")
                priowait_logged = True
            lane = HeavyLane()
            if lane.try_enter(name, os.getpid()):
                break
            reason = "lane-busy"
        if time.time() >= deadline:
            if reason == "lane-busy":
                log(f"SKIP {name} lane-busy" + (" night-wait-expired" if night else ""))
            else:
                log(f"SKIP {name} level={level} preempt={int(preempt)} cmd={command[0]}" + (f" swap={swap_mb:.0f}MB night-wait-expired" if night else ""))
            print(json.dumps({"ran": False, "reason": reason, "level": level}))
            return 0
        if not waited:
            log(f"NIGHTWAIT {name} reason={reason} level={level} swap={swap_mb:.0f}MB budget={NIGHT_WAIT_S}s")
            waited = True
        time.sleep(min(NIGHT_POLL_S, NIGHT_SETTLE_S) if reason in ("settle", "stagger") else NIGHT_POLL_S)
    if preempt:
        prio_wait = True
        if not priowait_logged:
            log(f"PRIOWAIT {name} level={level} light job runs; ingester/daemon yields to draft")
    log(f"RUN {name} level={level}")
    try:
        if pol == "light":
            command = ["/usr/sbin/taskpolicy", "-b", "/usr/bin/nice", "-n", "15"] + command
            log(f"LIGHT {name} business-hours: taskpolicy -b nice 15 cpu~40%")
        proc = subprocess.Popen(command, env=_child_env())
        lane.update_pid(name, proc.pid)
        rc = _duty_cycle(proc) if pol == "light" else proc.wait()
        log(f"DONE {name} rc={rc}")
        done = {"ran": True, "rc": rc, "level": level}
        if prio_wait:
            done["prio_wait"] = True
        print(json.dumps(done))
        return rc if isinstance(rc, int) else 1
    finally:
        _stamp_done()
        lane.leave()


def _heavy_pids() -> list[tuple[int, str]]:
    try:
        out = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=4,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        pid_s, _, cmd = ln.partition(" ")
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        if pid == os.getpid():
            continue
        if any(m in cmd for m in NEVER_MARKERS):
            continue
        if any(m in cmd for m in HEAVY_MARKERS):
            found.append((pid, cmd))
    return found


def _connected_8080(pids: set[int]) -> set[int]:
    return {pid for pid, _cmd in established_clients() if pid in pids}


def _load_stopped() -> dict:
    try:
        return json.loads(STOPPED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_stopped(doc: dict) -> None:
    STOPPED_PATH.parent.mkdir(parents=True, exist_ok=True)
    STOPPED_PATH.write_text(json.dumps(doc) + "\n")


def _signal(pid: int, sig: int) -> bool:
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def tick() -> dict:
    snap = mem_snapshot()
    level = snap["level"]
    preempt = priority_inflight()
    pause_for_pressure = level >= 4
    pause_for_priority = preempt
    heavies = _heavy_pids()
    on_slot = _connected_8080({pid for pid, _ in heavies})
    db_holders = _db_holder_pids()
    stopped = _load_stopped()
    actions = []
    for pid, cmd in heavies:
        key = str(pid)
        # A client inside llama-server decode must not be frozen: that pins the
        # only slot and stalls the draft behind it. Let that call finish.
        if pid in on_slot:
            continue
        state = _state(pid)
        # A stopped writer holds the sqlite lock for the whole stop. Resume it
        # and never stop it, even when the llama slot is busy.
        # work_context_index.py has to reach sqlite before the db-holder
        # exemption exists. Stopping it during preempt leaves the feed stamps
        # frozen for the whole chat. Resume it and do not stop it.
        if pid in db_holders or "work_context_index.py" in cmd:
            if state.startswith("T"):
                if _signal(pid, signal.SIGCONT):
                    actions.append(f"CONT {pid} db-lock")
                    log(f"SIGCONT pid={pid} reason=db-lock cmd={cmd[:140]}")
                stopped.pop(key, None)
            continue
        if cheap_no_llm(cmd, cmd.split()):
            if state.startswith("T"):
                if _signal(pid, signal.SIGCONT):
                    actions.append(f"CONT {pid} cheap")
                    log(f"SIGCONT pid={pid} reason=cheap cmd={cmd[:140]}")
                stopped.pop(key, None)
            continue
        if pause_for_pressure or pause_for_priority:
            if state.startswith("T"):
                stopped[key] = stopped.get(key) or {
                    "reason": "pressure" if pause_for_pressure else "priority",
                    "cmd": cmd[:180],
                    "since": time.time(),
                }
                continue
            if _signal(pid, signal.SIGSTOP):
                reason = "pressure" if pause_for_pressure else "priority"
                stopped[key] = {"reason": reason, "cmd": cmd[:180], "since": time.time()}
                actions.append(f"STOP {pid} {reason}")
                log(f"SIGSTOP pid={pid} reason={reason} cmd={cmd[:140]}")
        else:
            rec = stopped.get(key)
            if not rec:
                continue
            # Pressure pauses resume only at normal. Priority pauses resume
            # as soon as the draft/chat slot is free, even if level is still 2,
            # so a job that already held the lane can finish.
            if rec.get("reason") == "pressure" and level > 1:
                paused_cmd = rec.get("cmd") or ""
                # A cheap ingest frozen at urgent pressure may resume at
                # level 2. Heavy jobs stay frozen until level 1.
                if level >= 4 or not cheap_no_llm(paused_cmd, paused_cmd.split()):
                    continue
            if _signal(pid, signal.SIGCONT):
                actions.append(f"CONT {pid}")
                log(f"SIGCONT pid={pid} level={level}")
            stopped.pop(key, None)
    # Drop records whose process is gone.
    for key in list(stopped):
        if not _pid_alive(int(key)):
            stopped.pop(key, None)
    _save_stopped(stopped)
    holder = None
    if META_PATH.exists():
        try:
            holder = json.loads(META_PATH.read_text())
        except json.JSONDecodeError:
            holder = None
    state = {
        "ts": snap["ts"],
        "level": level,
        "swap": snap["swap"],
        "compressor_bytes": snap["compressor_bytes"],
        "priority_inflight": preempt,
        "foreground": foreground_live(),
        "server_busy": server_busy(),
        "priority_clients": [
            pid for pid, cmd in established_clients()
            if any(m in cmd for m in PRIORITY_CLIENTS)
        ],
        "holder": holder,
        "paused": stopped,
        "actions": actions,
        "heavy_count": len(heavies),
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state) + "\n")
    return state


def _launchd_pids() -> set[int]:
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=8,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = set()
    for ln in out.splitlines():
        parts = ln.split()
        if parts and parts[0].isdigit():
            pids.add(int(parts[0]))
    return pids


def _reap_orphans() -> list[int]:
    """SIGTERM leaked one-shot ingests reparented to pid 1.

    A live launchd job also has ppid 1, so only pids that launchctl is not
    tracking are eligible. Never sacred, never the priority lane.
    """
    reaped = []
    tracked = _launchd_pids()
    try:
        out = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=4,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return reaped
    for ln in out.splitlines():
        parts = ln.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, ppid_s, cmd = parts
        if not pid_s.isdigit() or ppid_s != "1":
            continue
        if "work_context_index.py" not in cmd and "multiprocessing.resource_tracker" not in cmd:
            continue
        if any(m in cmd for m in NEVER_MARKERS):
            continue
        pid = int(pid_s)
        if pid in tracked:
            continue
        # resource_tracker is a leak only when its parent is already gone.
        if "resource_tracker" in cmd:
            continue
        if _signal(pid, signal.SIGTERM):
            reaped.append(pid)
            log(f"TERM orphan pid={pid} cmd={cmd[:120]}")
    return reaped


def cmd_daemon() -> int:
    FLAG_OWNS.parent.mkdir(parents=True, exist_ok=True)
    FLAG_OWNS.write_text("memory-governor\n")
    log("daemon start")
    _reap_orphans()
    last_beat = 0.0
    while True:
        try:
            state = tick()
        except Exception as exc:
            log(f"tick error {type(exc).__name__}: {exc}")
            time.sleep(5)
            continue
        now = time.time()
        if state["actions"] or now - last_beat >= 60:
            log(
                f"beat level={state['level']} preempt={int(state['priority_inflight'])} "
                f"heavy={state['heavy_count']} paused={len(state['paused'])} "
                f"actions={','.join(state['actions']) or '-'}"
            )
            last_beat = now
        time.sleep(5)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in {"-h", "--help"}:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "daemon":
        return cmd_daemon()
    if cmd == "once":
        print(json.dumps(tick()))
        return 0
    if cmd == "status":
        print(STATE_PATH.read_text() if STATE_PATH.exists() else "{}")
        return 0
    if cmd == "check":
        return cmd_check()
    if cmd == "run":
        if "--" not in argv:
            print("usage: governor.py run <job> -- <cmd...>", file=sys.stderr)
            return 2
        name = argv[2]
        command = argv[argv.index("--") + 1 :]
        return cmd_run(name, command)
    if cmd == "reap":
        print(json.dumps({"termed": _reap_orphans()}))
        return 0
    print(f"unknown {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
