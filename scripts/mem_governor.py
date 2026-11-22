#!/usr/bin/env python3
"""Enforced Mini memory budget governor (launchd com.claude-stack.mem-governor).

Writes a throttle file that background lanes obey:
  normal   -> rate 1.00
  warning  -> rate 0.25
  critical -> rate 0.00 (pause)

Alerts agent-bus when swap used stays above 1.5 GB for 10 minutes.
NEVER kills the model, CDP bridges, OneDrive, desktop-bot-app, or local-drafter.
NEVER reboots. NEVER sets minicpm_n_cpu_moe=20.
Cheap ingest/embed lanes ignore the throttle file on purpose.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
FLAGS = HOME / "claude-stack/data/flags"
RUNTIME = HOME / "llm-appliance/runtime/mem-governor"
THROTTLE = FLAGS / "bg_throttle.json"
STATE = RUNTIME / "state.json"
LOG = HOME / "Library/Logs/claude-stack/mem-governor.log"
BUS_ENQUEUE = HOME / "claude-stack/bin/agent-bus-enqueue.py"

SWAP_ALERT_MB = 1536.0
SWAP_ALERT_SUSTAIN_S = 10 * 60
ALERT_COOLDOWN_S = 6 * 3600

# Protected process substrings — never signal/kill these from this governor.
PROTECTED = (
    "splash",
    "splash_shim",
    "splash_serve",
    "mlx-qwen",
    "llama-server",
    "OneDrive",
    "desktop-bot",
    "local-drafter",
    "local_drafter",
    "owa-edge-cdp",
    "zoom",
    "tracker-bridge",
)


def _log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    try:
        with LOG.open("a", encoding="utf-8") as fh:
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
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def swap_used_mb() -> float:
    raw = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout
    m = re.search(r"used\s*=\s*([0-9.]+)M", raw or "")
    return float(m.group(1)) if m else 0.0


def kern_pressure_level() -> int:
    raw = subprocess.run(
        ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def splash_pressure() -> str:
    """Prefer Splash engine pressure; kernel only when metrics are unreachable.

    Kernel level stays at 1 for hours while stale swap drains, which wrongly
    holds background lanes at 25% after the model budget is already healthy.
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:18085/metrics", timeout=2) as resp:
            raw = resp.read().decode("utf-8", "replace")
        if 'splash_memory_pressure{state="critical"} 1' in raw:
            return "critical"
        if 'splash_memory_pressure{state="warning"} 1' in raw:
            return "warning"
        if "splash_memory_pressure" in raw:
            return "normal"
    except Exception:
        pass
    # Metrics down: fall back to kernel pressure (0 normal, 1 warn, 2+ critical)
    lvl = kern_pressure_level()
    if lvl >= 2:
        return "critical"
    if lvl == 1:
        return "warning"
    return "normal"


def level_to_rate(level: str) -> float:
    if level == "critical":
        return 0.0
    if level == "warning":
        return 0.25
    return 1.0


def enqueue_swap_alert(swap_mb: float, sustained_s: float) -> bool:
    if not BUS_ENQUEUE.is_file():
        _log("alert skip: agent-bus-enqueue missing")
        return False
    job = {
        "goal": (
            f"Mini swap alert: used {swap_mb:.0f} MB for {int(sustained_s)}s "
            f"(threshold {SWAP_ALERT_MB:.0f} MB / {SWAP_ALERT_SUSTAIN_S}s). "
            "Do not kill the model. Inspect mem-governor throttle and splash budget."
        ),
        "lane": "local",
        "success_criteria": "Operator or local lane acknowledges the swap alert and notes current throttle level.",
        "payload": {
            "kind": "mini_swap_alert",
            "swap_mb": swap_mb,
            "sustained_s": sustained_s,
            "threshold_mb": SWAP_ALERT_MB,
        },
        "dry_run": False,
    }
    try:
        subprocess.run(
            ["/opt/homebrew/bin/python3", str(BUS_ENQUEUE), "-"],
            input=json.dumps(job),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        _log(f"agent-bus alert enqueued swap={swap_mb:.0f}MB")
        return True
    except (OSError, subprocess.SubprocessError) as e:
        _log(f"agent-bus alert failed: {e}")
        return False


def tick(now: float | None = None) -> dict:
    now = float(now if now is not None else time.time())
    state = _read_json(STATE, {})
    level = splash_pressure()
    rate = level_to_rate(level)
    swap_mb = swap_used_mb()
    throttle = {
        "level": level,
        "rate": rate,
        "swap_mb": swap_mb,
        "ts": now,
        "policy": {
            "normal": 1.0,
            "warning": 0.25,
            "critical": 0.0,
        },
        "exempt": ["cheap_ingest", "cheap_embed", "bge_embed"],
        "never_kill": list(PROTECTED),
    }
    _write_json(THROTTLE, throttle)
    # also a plain rate file for shell readers
    (FLAGS / "bg_throttle_rate").write_text(f"{rate}\n", encoding="utf-8")

    high_since = state.get("swap_high_since")
    last_alert = float(state.get("last_alert_ts") or 0)
    if swap_mb > SWAP_ALERT_MB:
        if high_since is None:
            high_since = now
        elif now - float(high_since) >= SWAP_ALERT_SUSTAIN_S and (now - last_alert) >= ALERT_COOLDOWN_S:
            enqueue_swap_alert(swap_mb, now - float(high_since))
            last_alert = now
            high_since = now  # reset sustain window after alert
    else:
        high_since = None

    state = {
        "level": level,
        "rate": rate,
        "swap_mb": swap_mb,
        "swap_high_since": high_since,
        "last_alert_ts": last_alert,
        "ts": now,
    }
    _write_json(STATE, state)
    _log(f"level={level} rate={rate} swap_mb={swap_mb:.0f}")
    return state


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if argv and argv[0] == "daemon":
        while True:
            try:
                tick()
            except Exception as e:
                _log(f"tick_error {e}")
            time.sleep(30)
    else:
        print(json.dumps(tick()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
