"""Self-heal for two local browser bridges. Not a daemon. No mail, no chat.

Ladder, and each rung is checked before the next one runs:
  1. Rebind the receipt to the service pid when that pid is this job.
  2. If the service is stuck (spawn failed or spawn scheduled, often after
     SIGTERM) or has no pid, rebootstrap the plist with launchctl asuser
     into gui/UID. kickstart -k from an SSH Background session can return
     already-in-progress or hang and does not unstick that state.
  3. Otherwise kickstart -k of this label only.
  4. Signal that owned pid only, and only when the port is still down.

Three heals per bridge per hour, then one local note.
"""
from __future__ import annotations

from dataclasses import dataclass

STALE_S = 600
MAX_HEALS = 3
HOUR_S = 3600
BACKOFF_S = (60, 120, 240)
FORBIDDEN_PORTS = (8080, 8462, 9227, 9331)


@dataclass
class Snap:
    port_up: bool = False
    cdp_up: bool = False
    ensure_line: str = ""
    receipt_pid: int | None = None
    service_pid: int | None = None
    service_state: str = ""
    cache_age_s: float | None = None
    unread: int | None = None


def kickstart_argv(label: str, uid: int, *, allowed: frozenset[str], kill: bool = True) -> list[str]:
    if label not in allowed:
        raise ValueError("refused label")
    argv = ["launchctl", "kickstart"]
    if kill:
        argv.append("-k")
    argv.append(f"gui/{int(uid)}/{label}")
    return argv


STUCK_STATES = frozenset({"spawn failed", "spawn scheduled"})


def launchd_is_stuck(state: str, pid: int | None, *, port_up: bool) -> bool:
    """True when the service manager will not respawn the job, often after SIGTERM."""
    st = (state or "").strip().lower().replace("_", " ")
    if st in STUCK_STATES:
        return True
    if (not port_up) and not pid:
        return True
    return False


def rungs_for(snap: Snap) -> tuple[str, ...]:
    if launchd_is_stuck(snap.service_state, snap.service_pid, port_up=snap.port_up):
        return ("adopt", "unstick", "kickstart")
    return ("adopt", "kickstart", "restart_pid")


def rebootstrap_argv(label: str, uid: int, plist: str, *, allowed: frozenset[str]) -> list[list[str]]:
    """bootout + bootstrap in the GUI domain, even from an SSH Background session."""
    if label not in allowed:
        raise ValueError("refused label")
    name = plist.rsplit("/", 1)[-1]
    if not plist.endswith(".plist") or name != f"{label}.plist":
        raise ValueError("refused plist")
    domain = f"gui/{int(uid)}"
    asuser = ["launchctl", "asuser", str(int(uid)), "launchctl"]
    return [
        asuser + ["bootout", domain, plist],
        asuser + ["bootstrap", domain, plist],
    ]


def command_matches(cmd: str, *, port: int, profile: str, browser_mark: str) -> bool:
    if not cmd or f"--remote-debugging-port={port}" not in cmd:
        return False
    if "--user-data-dir=" not in cmd or profile not in cmd:
        return False
    if browser_mark not in cmd:
        return False
    for forbidden in FORBIDDEN_PORTS:
        if f"--remote-debugging-port={forbidden}" in cmd:
            return False
    return True


def detect(snap: Snap) -> str | None:
    line = (snap.ensure_line or "").strip()
    if line.startswith("BLOCKED"):
        return "ensure:" + line.split()[0]
    if not snap.port_up:
        return "port_down"
    if not snap.cdp_up:
        return "cdp_down"
    owned = snap.service_state in ("running", "languishing") and bool(snap.service_pid)
    if owned and snap.receipt_pid != snap.service_pid:
        return "BLOCKED-not-owned"
    if (
        snap.cache_age_s is not None
        and snap.cache_age_s > STALE_S
        and snap.unread is not None
        and snap.unread > 0
    ):
        return "stale_cache"
    return None


def _backoff(n_already: int) -> int:
    if n_already <= 0:
        return 0
    return BACKOFF_S[min(n_already - 1, len(BACKOFF_S) - 1)]


def _recent(stamps: list[float], now: float) -> list[float]:
    return [float(t) for t in stamps if now - float(t) < HOUR_S]


def heal(bridge: str, snap: Snap, actions: dict, state: dict, *, now: float, allowed: frozenset[str]) -> dict:
    """One ladder pass. `actions` keys are callables: adopt, unstick, kickstart, restart_pid, ensure, refresh, snapshot."""
    if bridge not in allowed and bridge not in ("chat", "mail"):
        return {"action": "refused"}
    reason = detect(snap)
    row = state.setdefault(bridge, {"heals": [], "exhausted": False})
    if not reason:
        if row.get("exhausted"):
            row["exhausted"] = False
            row["heals"] = []
        return {"action": "none"}
    recent = _recent(list(row.get("heals") or []), now)
    row["heals"] = recent
    if len(recent) >= MAX_HEALS:
        row["exhausted"] = True
        row["last_reason"] = reason
        state["note"] = "HEAL_EXHAUSTED\nno email. no chat."
        return {"action": "HEAL_EXHAUSTED", "reason": reason}
    if recent and now - recent[-1] < _backoff(len(recent)):
        return {"action": "backoff", "reason": reason}
    row["heals"] = recent + [now]
    row["exhausted"] = False
    row["last_reason"] = reason
    current = snap
    stopped = None
    result = "still_bad"
    for rung in rungs_for(snap):
        if rung == "restart_pid" and reason == "stale_cache" and current.port_up and current.cdp_up:
            result = "stale_cache_bridge_up"
            stopped = "kickstart"
            break
        actions[rung](bridge)
        if rung in ("kickstart", "restart_pid"):
            actions.get("wait", lambda *_: None)(bridge)
        if rung == "unstick":
            actions.get("wait", lambda *_: None)(bridge)
        if reason == "stale_cache":
            actions["refresh"](bridge)
        current = actions["snapshot"](bridge)
        if current.cdp_up:
            line = actions["ensure"](bridge)
            if str(line).startswith(("BLOCKED", "FAILED", "DOWN")):
                current.ensure_line = line
                if "BLOCKED" in str(line):
                    actions["adopt"](bridge)
                    current = actions["snapshot"](bridge)
                    line = actions["ensure"](bridge)
                    if str(line).startswith(("BLOCKED", "FAILED", "DOWN")):
                        current.ensure_line = line
        if detect(current) is None:
            result = "cleared"
            stopped = rung
            break
        if reason != "stale_cache" and current.port_up and current.cdp_up:
            result = "bridge_up_residual"
            stopped = rung
            break
        stopped = rung
    return {"action": "heal", "reason": reason, "rung": stopped, "result": result}
