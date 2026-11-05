
import os, subprocess, time, urllib.request
from pathlib import Path
HOME = Path.home()
CLI = HOME / "llm-appliance/cli"
RUNTIME = HOME / "llm-appliance/runtime/local-cycle-browser"
py = HOME / "llm-appliance/runtime/venv/bin/python"
lock = RUNTIME / "idle-ticket-kick.lock"
stamp = RUNTIME / "idle-ticket-kick.stamp"
logp = HOME / "Library/Logs/claude-stack/ops-draft-immortal.log"

def log(msg):
    line = time.strftime("%Y-%m-%dT%H:%M:%S") + " " + msg + "\n"
    try:
        with open(logp, "a") as f:
            f.write(line)
    except Exception:
        pass

env = os.environ.copy()
env["LLM_WORK_LOOP_FORCE"] = "1"
env["ADO_VA_LIMIT"] = env.get("ADO_VA_LIMIT", "3")
try:
    lock.write_text(str(os.getpid()))
    ado_up = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:18820/json/version", timeout=2) as r:
            ado_up = r.status == 200
    except Exception:
        ado_up = False
    log(f"idle_ticket_bg start ado_up={ado_up}")
    if ado_up:
        w = CLI / "va_ado_worker.py"
        if w.exists():
            r = subprocess.run([str(py), str(w)], capture_output=True, text=True, timeout=180, env=env, cwd=str(CLI))
            log(f"idle_ticket_kick ado rc={r.returncode} out={(r.stdout or "")[:140]!r}")
    for script, key in (("work_real_packets.py", "real"), ("work_graph.py", "graph")):
        sp = CLI / script
        if not sp.exists():
            continue
        try:
            rr = subprocess.run([str(py), str(sp)], capture_output=True, text=True, timeout=240, env=env, cwd=str(CLI))
            log(f"idle_ticket_kick {key} rc={rr.returncode}")
        except Exception as ee:
            log(f"idle_ticket_kick_{key}_exc:{ee}")
    stamp.write_text(str(time.time()))
    log("idle_ticket_bg done")
finally:
    try:
        lock.unlink(missing_ok=True)
    except Exception:
        pass
