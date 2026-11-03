"""Check local services and heal them through the service manager."""

from __future__ import annotations

import urllib.request

from hailports.osapi import ServiceManager, restart_exact


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def check_one(mgr: ServiceManager, label: str, url: str, recorded_pid: int | None, current_pid: int | None) -> str:
    healthy = http_ok(url)
    return restart_exact(mgr, label, recorded_pid, current_pid, healthy)


def run_once(cfg: dict, mgr: ServiceManager, pid_for, now: float | None = None, brain=None, alerter=None) -> list[str]:
    """Health of the model or web chat, the embed server, note freshness, and a login probe."""
    import time
    from pathlib import Path

    from hailports.alerts import alert_text, deliver, probe_login
    from hailports.notes import stale_ids

    mode = str(cfg.get("mode") or "local")
    label = str(cfg.get("label") or "com.hailports.local")
    outcomes = []
    if mode == "api":
        web = cfg.get("webchat") or {}
        url = f"http://{web.get('bind') or '127.0.0.1'}:{int(web.get('port') or 8080)}/health"
        service = label + ".webchat"
        kind = "webchat"
    else:
        url = ((cfg.get("llm") or {}).get("upstream_url") or "http://127.0.0.1:18080").rstrip("/") + "/health"
        service = label + ".model"
        kind = "model"
    recorded = pid_for(kind)
    outcomes.append(kind + " " + check_one(mgr, service, url, recorded, recorded))
    embed_url = str(((cfg.get("notes") or {}).get("embed_url") or ""))
    if embed_url and mode != "api":
        embed_pid = pid_for("embed")
        embed_health = embed_url.rstrip("/") + "/health"
        outcomes.append("embed " + check_one(mgr, label + ".embed", embed_health, embed_pid, embed_pid))
    if brain is not None:
        stamp = time.time() if now is None else now
        outcomes.append("stale " + str(len(stale_ids(brain, stamp, float((cfg.get("notes") or {}).get("max_age_s") or 86400)))))
    probe = str(((cfg.get("alerts") or {}).get("probe_url") or ""))
    if probe:
        state = probe_login(probe)
        outcomes.append("login " + state)
        if state == "login-broken" and alerter is not None:
            try:
                reply = alert_text(alerter, "probe")
                text = reply.text or reply.render()
            except Exception:
                text = "login-broken service=probe"
            private_url = str(((cfg.get("alerts") or {}).get("private_url") or ""))
            outbox = Path(str((cfg.get("alerts") or {}).get("outbox") or "alerts.log"))
            info = deliver(text, private_url, outbox)
            outcomes.append("alert " + info["reason"])
    return outcomes


def main(argv: list[str] | None = None) -> int:
    import argparse
    import time
    from pathlib import Path

    from hailports.config import load_file
    from hailports.osapi import current_os, read_pid

    parser = argparse.ArgumentParser(prog="hailports.sentinel")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args(argv)
    cfg = load_file(Path(args.config))
    parent = Path(args.config).resolve().parent
    mgr = ServiceManager(current_os(), apply=True)
    router = None
    try:
        from hailports.providers.factory import build_router
        from hailports.secrets_store import EnvAndKeychain
        router = build_router(cfg, EnvAndKeychain())
    except Exception:
        router = None
    brain = None
    try:
        from hailports.notes import connect as brain_connect
        db = parent / "brain.sqlite"
        if db.exists():
            brain = brain_connect(db)
    except Exception:
        brain = None
    while True:
        lines = run_once(cfg, mgr, lambda kind: read_pid(parent / f"{kind}.pid"), brain=brain, alerter=router)
        print("\n".join(lines))
        if args.once:
            return 0
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
