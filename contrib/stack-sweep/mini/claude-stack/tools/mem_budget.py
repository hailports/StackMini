#!/usr/bin/env python3
"""One-minute memory budget for the Mini.

Measures resident processes, compares them to hard budgets, and restarts the
single worst offender that is over its budget. OneDrive is never touched.
The :8080 model is restarted only when /health has failed twice, the slot is
idle, and the local hour is outside 07:00-18:00. Sleep settings are not written.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
PY = "/opt/homebrew/bin/python3"
RULES_PATH = HOME / "claude-stack/data/runtime/mem_budget.json"
STATE_PATH = HOME / "claude-stack/data/runtime/mem_budget_state.json"
TREND_PATH = HOME / "claude-stack/data/runtime/mem_trend.jsonl"
LOG_PATH = HOME / "claude-stack/data/logs/mem_budget.log"
KEEPER = HOME / "claude-stack/tools/cdp_login_keeper.py"
SWAP_TARGET_MB = 2800.0
PAGE = 16384

# Size enforcement never restarts these during the workday. The model is also
# refused overnight unless it is actually hung.
WORK_DEFER = {
    "com.llm-appliance.mlx-qwen",
    "com.llm-appliance.ops-draft-immortal",
}
NEVER_RESTART = {
    "com.claude-stack.stay-awake",
    "com.llm-appliance.mem-budget",
    "com.claude-stack.caffeinate",
    "com.claude-stack.operator-onedrive-immortal",
    "com.claude-stack.onedrive-warden",
    "com.claude-stack.onedrive-health-guard",
}


def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)


def swap_target_mb() -> float:
    doc = _read_json(RULES_PATH, {})
    try:
        return float(doc.get("swap_target_mb") or SWAP_TARGET_MB)
    except (TypeError, ValueError):
        return SWAP_TARGET_MB


def swap_mb() -> tuple[float, float]:
    raw = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout
    total = used = -1.0
    for part in raw.replace("=", " ").split():
        pass
    import re
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", raw)
    if m:
        total, used = float(m.group(1)), float(m.group(2))
    return used, total


def pressure() -> str:
    raw = subprocess.run(
        ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()
    return raw or "unreadable"


def vm_counts() -> dict[str, int]:
    out = {}
    raw = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        digits = "".join(ch for ch in val if ch.isdigit())
        if digits:
            out[key.strip()] = int(digits)
    return out


def work_hours(now: float | None = None) -> bool:
    lt = time.localtime(now or time.time())
    return 7 <= lt.tm_hour < 18


def labels_by_pid() -> dict[int, str]:
    raw = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
    found = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit():
            found[int(parts[0])] = parts[-1]
    return found


def processes() -> list[dict]:
    raw = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss=,command="],
        capture_output=True, text=True, timeout=15,
    ).stdout
    rows = []
    for line in raw.splitlines():
        bits = line.split(None, 3)
        if len(bits) < 4 or not bits[0].isdigit():
            continue
        rows.append({
            "pid": int(bits[0]),
            "ppid": int(bits[1]),
            "rss_mb": int(bits[2]) / 1024.0,
            "cmd": bits[3],
        })
    return rows


def footprint_mb(pid: int) -> float | None:
    try:
        raw = subprocess.run(
            ["footprint", "-p", str(pid)],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    import re
    m = re.search(r"Footprint:\s+([\d.]+)\s+([KMG])B", raw)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "G":
        return val * 1024
    if unit == "K":
        return val / 1024
    return val


def load_rules() -> list[dict]:
    doc = _read_json(RULES_PATH, {})
    rules = doc.get("rules") if isinstance(doc, dict) else None
    return rules or []


def match_rule(cmd: str, rules: list[dict]) -> dict | None:
    low = cmd.lower()
    for rule in rules:
        token = str(rule.get("match") or "").lower()
        if token and token in low:
            return rule
    return None


def exempt(cmd: str, rule: dict | None) -> bool:
    low = cmd.lower()
    if any(tok in low for tok in ("onedrive", "cloudstorage", "fileprovider")):
        return True
    return bool(rule and rule.get("exempt"))


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def model_busy() -> bool | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/slots", timeout=4) as resp:
            rows = json.loads(resp.read().decode("utf-8", "replace"))
        return any(bool(row.get("is_processing")) for row in rows)
    except Exception:
        return None


def cmd_of(pid: int) -> str:
    raw = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()
    return raw


def kick_label(label: str) -> str:
    uid = os.getuid()
    proc = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
        capture_output=True, text=True, timeout=30,
    )
    return f"kickstart {label} rc={proc.returncode}"


def term_pid(pid: int, expect: str) -> str:
    live = cmd_of(pid)
    if not live or expect not in live:
        return f"skip pid={pid} command changed"
    os.kill(pid, signal.SIGTERM)
    return f"SIGTERM pid={pid}"


def choose(rows: list[dict], rules: list[dict], labels: dict[int, str], only_match: str | None, budget_override: float | None) -> dict | None:
    best = None
    checked = 0
    ranked = sorted(rows, key=lambda r: r["rss_mb"], reverse=True)
    for row in ranked:
        if only_match and only_match not in row["cmd"]:
            continue
        rule = match_rule(row["cmd"], rules)
        if exempt(row["cmd"], rule):
            continue
        if rule and rule.get("model"):
            continue
        budget = budget_override if budget_override is not None and only_match else (None if rule is None else rule.get("mb"))
        if budget is None:
            continue
        budget = float(budget)
        # Browsers hide a lot of their real size in footprint, not RSS.
        need_fp = (
            "--type=renderer" in row["cmd"]
            or "Microsoft Edge Helper (Renderer)" in row["cmd"]
            or row["rss_mb"] >= max(80.0, budget * 0.7)
        )
        used = row["rss_mb"]
        if need_fp and checked < 8:
            fp = footprint_mb(row["pid"])
            checked += 1
            if fp is not None:
                used = fp
                row["footprint_mb"] = round(fp, 1)
        row["used_mb"] = round(used, 1)
        over = used - budget
        if over <= 0:
            continue
        label = labels.get(row["pid"]) or labels.get(row["ppid"]) or ""
        if label in WORK_DEFER and work_hours():
            continue
        row["label"] = label
        row["budget_mb"] = budget
        row["over_mb"] = round(over, 1)
        if best is None or over > best["over_mb"]:
            best = row
    return best


PROFILE_LABELS = (
    ("zoom-edge-cdp-profile-team-chat", "com.localhost-bridge-browser.zoom"),
    ("owa-edge-cdp-profile-outlook", "com.localhost-bridge-browser.outlook"),
    ("edge-cdp-profile-owa-managed", "com.localhost-bridge-browser.ado"),
)
SAFE_PREFIXES = (
    "com.llm-appliance.",
    "com.claude-stack.",
    "com.localhost-bridge-browser.",
)


def owned_label(cmd: str, label: str) -> str:
    """Only restart a job we installed. Never an Apple application.* service."""
    for token, lab in PROFILE_LABELS:
        if token in cmd:
            return lab
    if label.startswith(SAFE_PREFIXES) and "onedrive" not in label.lower():
        return label
    return ""


def enforce(victim: dict, now: float) -> str:
    label = owned_label(victim.get("cmd") or "", victim.get("label") or "")
    if not label:
        if "Microsoft Edge" in (victim.get("cmd") or "") or "OneDrive" in (victim.get("cmd") or ""):
            return f"no owned label pid={victim['pid']} left it"
        token = victim["cmd"].split(" --")[0][-80:]
        return term_pid(victim["pid"], token) + f" over={victim['over_mb']}MB"
    if label in NEVER_RESTART or "onedrive" in label.lower():
        return f"exempt label {label} pid={victim['pid']}"
    if label in WORK_DEFER and work_hours(now):
        return f"deferred until after hours label={label} pid={victim['pid']} over={victim['over_mb']}MB"
    return kick_label(label) + f" pid={victim['pid']} over={victim['over_mb']}MB"


def model_action(state: dict) -> str:
    ok = http_ok("http://127.0.0.1:8080/health")
    if ok:
        state["model_fails"] = 0
        return ""
    state["model_fails"] = int(state.get("model_fails") or 0) + 1
    if state["model_fails"] < 2:
        return f"model health miss {state['model_fails']}"
    if work_hours():
        return "model health failed during work hours, left it"
    busy = model_busy()
    if busy is None or busy:
        return "model health failed while slot is busy or unknown, left it"
    return kick_label("com.llm-appliance.mlx-qwen") + " hung"


def _pages(port: int) -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
            rows = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []
    return [row for row in rows if row.get("type") == "page"]


def tabs_need_trim() -> bool:
    zoom = _pages(18812)
    chat = [p for p in zoom if "team-chat" in (p.get("url") or "")]
    if len(chat) > 1:
        return True
    for port in (18812, 18822, 18820):
        pages = _pages(port) if port != 18812 else zoom
        http_pages = [p for p in pages if (p.get("url") or "").startswith("http")]
        orphan = [p for p in pages if (p.get("url") or "").lower().startswith(("edge://", "chrome://"))]
        if orphan and http_pages:
            return True
    return False


def tab_hygiene() -> str:
    """Collapse duplicate Zoom team-chat tabs and drop orphan settings pages.

    The cheap list check runs every minute. The keeper, which keeps the tab that
    actually rendered, runs only when a duplicate or an orphan settings page is there.
    """
    if not KEEPER.exists() or not tabs_need_trim():
        return ""
    try:
        proc = subprocess.run(
            [PY, str(KEEPER), "trim"],
            capture_output=True, text=True, timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"trim-fail {exc.__class__.__name__}"
    line = (proc.stdout or "").strip().splitlines()
    text = line[-1] if line else f"trim rc={proc.returncode}"
    if "closed 0" in text:
        return ""
    return text


def rotate_logs() -> str:
    root = HOME / "Library/Logs/claude-stack"
    if not root.is_dir():
        return ""
    limit = 200 * 1024 * 1024
    keep = 20 * 1024 * 1024
    for path in sorted(root.glob("*.log")):
        if "onedrive" in path.name.lower():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < limit:
            continue
        dest = path.with_name(path.name + ".1")
        try:
            with dest.open("wb") as out:
                subprocess.run(["/usr/bin/tail", "-c", str(keep), str(path)], stdout=out, check=False, timeout=60)
            os.replace(dest, path)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"rotate-fail {path.name} {exc.__class__.__name__}"
        _log(f"rotated {path.name} was={size}")
        return f"rotated {path.name}"
    return ""


def learn_mark() -> str:
    path = HOME / "claude-stack/data/logs/learn_fairshare.log"
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - 4000))
            blob = fh.read().decode("utf-8", "replace")
    except OSError:
        return "nolog"
    last = ""
    for line in blob.splitlines():
        if "learn-out" in line or "SIGCONT" in line or "credit=" in line:
            last = line[-140:]
    return last or "no-recent-slice"


def tick(only_match: str | None = None, budget_override: float | None = None, hygiene: bool = True) -> str:
    now = time.time()
    state = _read_json(STATE_PATH, {})
    used, total = swap_mb()
    target = swap_target_mb()
    counts = vm_counts()
    wired = counts.get("Pages wired down", 0) * PAGE / 1024 / 1024
    free_pages = counts.get("Pages free", 0)
    action = ""
    if hygiene and not only_match:
        action = tab_hygiene()
        if not action:
            action = rotate_logs()
    victim = None
    if not action:
        victim = choose(processes(), load_rules(), labels_by_pid(), only_match, budget_override)
        if victim:
            action = enforce(victim, now)
            _log(
                f"offender pid={victim['pid']} used={victim.get('used_mb')}MB "
                f"budget={victim.get('budget_mb')}MB {action}"
            )
    if not action and not only_match:
        action = model_action(state)
        if action:
            _log(action)
    if not action:
        if used <= target:
            action = "hold"
        else:
            proc = subprocess.run(
                [PY, str(HOME / "llm-appliance/cli/mem_heal.py"), "reclaim"],
                capture_output=True, text=True, timeout=180,
            )
            tail = (proc.stdout or proc.stderr or "").strip().splitlines()
            action = "reclaim rc={0} {1}".format(proc.returncode, (tail[-1] if tail else "")[:160])
        if not action.startswith("hold"):
            _log(action)
    state["last_action"] = action
    state["last_ts"] = int(now)
    _write_json(STATE_PATH, state)
    row = {
        "t": int(now),
        "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "swap_used_mb": round(used, 1),
        "swap_total_mb": round(total, 1),
        "pressure": pressure(),
        "pages_free": free_pages,
        "wired_mb": round(wired, 0),
        "action": action,
        "learn": learn_mark() if not only_match else "",
    }
    if victim:
        row["offender_pid"] = victim["pid"]
        row["offender_over_mb"] = victim.get("over_mb")
    TREND_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TREND_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return action


def main(argv: list[str]) -> int:
    only = None
    budget = None
    hygiene = "--no-hygiene" not in argv
    if "--only-match" in argv:
        only = argv[argv.index("--only-match") + 1]
    if "--budget-mb" in argv:
        budget = float(argv[argv.index("--budget-mb") + 1])
    if "--once" in argv or "--only-match" in argv:
        print(tick(only, budget, hygiene=hygiene and only is None))
        return 0
    print(tick())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
