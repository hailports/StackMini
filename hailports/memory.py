"""Memory budget. Over budget, ask the service manager to restart the recorded PID only."""

from __future__ import annotations

from hailports.osapi import ServiceManager, restart_exact


def used_ratio(total: int, available: int) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, (total - available) / total))


def over_budget(used_mb: float, budget_mb: float) -> bool:
    return budget_mb > 0 and used_mb > budget_mb


def model_command_matches(text: str, model_token: str = "") -> bool:
    """True for the draft server on either published port, or an explicit model token.

    A different port, such as an embed server, does not match unless the token is in the command.
    """
    raw = text or ""
    if "llama-server" not in raw:
        return False
    if "--port 18080" in raw or "--port 8080" in raw:
        return True
    token = (model_token or "").strip()
    return bool(token) and token in raw


def refuses_model_or_reboot(command: str, model_token: str = "") -> bool:
    """A healer may reclaim memory. It may not kill the model server or reboot."""
    import re
    low = (command or "").lower()
    if re.search(r"\b(reboot|shutdown|halt)\b", low):
        return True
    if re.search(r"\b(kill|killall|pkill|kickstart)\b", low) and model_command_matches(command, model_token):
        return True
    return False


def tick(mgr: ServiceManager, label: str, recorded_pid: int | None, current_pid: int | None, used_mb: float, budget_mb: float, model_healthy: bool) -> str:
    if not over_budget(used_mb, budget_mb):
        return "within-budget"
    # Over budget counts as unhealthy. Exact-PID rules still apply.
    del model_healthy
    return restart_exact(mgr, label, recorded_pid, current_pid, healthy=False)


def watch_pid(mgr: ServiceManager, label: str, pid_path, budget_mb: float, model_healthy: bool, rss_fn=None) -> str:
    """Read the recorded process id and restart through the service manager only when it matches."""
    from hailports.osapi import pid_alive, process_rss_mb, read_pid

    recorded = read_pid(pid_path)
    current = recorded if recorded and pid_alive(recorded) else None
    reader = rss_fn or process_rss_mb
    used = reader(recorded) if recorded else 0
    return tick(mgr, label, recorded, current, float(used or 0), budget_mb, model_healthy)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import time
    from pathlib import Path

    from hailports.config import load_file
    from hailports.osapi import ServiceManager, current_os

    parser = argparse.ArgumentParser(prog="hailports.memory")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args(argv)
    cfg = load_file(Path(args.config))
    budget = float(((cfg.get("memory") or {}).get("budget_mb") or 0))
    label = str(cfg.get("label") or "com.hailports.local") + ".model"
    pid_path = Path(args.config).resolve().parent / "model.pid"
    mgr = ServiceManager(current_os(), apply=True)
    while True:
        print(watch_pid(mgr, label, pid_path, budget, model_healthy=True))
        if args.once:
            return 0
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
