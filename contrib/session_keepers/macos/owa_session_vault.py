#!/usr/bin/env python3
"""owa_session_vault -- save / restore the Outlook web (OWA) + Microsoft SSO cookie jar.

Added 2026-11-03 (Outlook twin of zoom_session_vault.py). Uses the SAME vault the
owa_session_guardian reads: ~/.cdp_session_vault/.owa-edge-cdp-profile-outlook/cookies-<UTC>.json,
format {saved_at, port, cookies}, 0600 files in a 0700 dir, rotated 3 deep. Values are never printed.

  save    <endpoint>  read-only Storage.getCookies; keeps Outlook/Office/Microsoft-login/corp-IdP
                      domains; refuses unless an auth cookie is present.
  restore <endpoint>  Storage.setCookies of the newest snapshot. REFUSES :18822 (sacred OWA bridge).
  status              newest snapshot age + count.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path.home() / ".cdp_session_vault" / ".owa-edge-cdp-profile-outlook"
KEEP = 3
DOMAINS = ("outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft", "login.microsoftonline.com",
           "login.microsoft.com", "login.live.com", "office.com", "microsoft365.com", "m365.cloud.microsoft",
           "idp.corp.example.com", "idp2.corp.example.com", "sso.corp.example.com")
AUTH_NAMES = ("ESTSAUTH", "ESTSAUTHPERSISTENT", "MSISAuth", "OIDCAuthCookie", "X-OWA-CANARY", "SignInStateCookie")
FORBIDDEN = (":18822",)


def _keep(c: dict) -> bool:
    d = str(c.get("domain") or "").lstrip(".").lower()
    return any(d == x or d.endswith("." + x) for x in DOMAINS)


def _browser_call(ep: str, method: str, params: dict | None = None) -> dict:
    import websocket
    with urllib.request.urlopen(ep.rstrip("/") + "/json/version", timeout=6) as r:
        ws_url = json.load(r)["webSocketDebuggerUrl"]
    ws = websocket.create_connection(ws_url, timeout=20, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == 1:
                return r
    finally:
        ws.close()


def save(ep: str) -> int:
    r = _browser_call(ep, "Storage.getCookies")
    cookies = [c for c in (r.get("result") or {}).get("cookies") or [] if _keep(c)]
    if not any(any(c.get("name", "").startswith(a) for a in AUTH_NAMES) for c in cookies):
        print(f"[owa-vault] refusing to save from {ep}: no auth cookie")
        return 2
    VAULT.mkdir(parents=True, exist_ok=True)
    os.chmod(VAULT, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = VAULT / f"cookies-{stamp}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    port = ep.rsplit(":", 1)[-1]
    slim = [{k: c[k] for k in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite") if k in c}
            for c in cookies]
    with os.fdopen(fd, "w") as fh:
        json.dump({"saved_at": datetime.now(timezone.utc).isoformat(), "port": port, "cookies": slim}, fh)
    for old in sorted(VAULT.glob("cookies-*.json"))[:-KEEP]:
        old.unlink(missing_ok=True)
    print(f"[owa-vault] saved {len(slim)} cookies from :{port}")
    return 0


def newest() -> Path | None:
    snaps = sorted(VAULT.glob("cookies-*.json")) if VAULT.exists() else []
    return snaps[-1] if snaps else None


def restore(ep: str, *, dry: bool = False) -> int:
    if any(f in ep for f in FORBIDDEN):
        print("[owa-vault] refusing: :18822 is the sacred OWA bridge (detect/alert only)")
        return 3
    snap = newest()
    if snap is None:
        print("[owa-vault] no snapshot")
        return 2
    cookies = json.loads(snap.read_text()).get("cookies") or []
    age_h = int((time.time() - snap.stat().st_mtime) // 3600)
    if dry:
        print(f"[owa-vault] DRY would restore {len(cookies)} cookies ({age_h}h old) into {ep}")
        return 0
    r = _browser_call(ep, "Storage.setCookies", {"cookies": cookies})
    if "error" in r:
        print(f"[owa-vault] restore failed: {r['error'].get('message', '')[:120]}")
        return 1
    print(f"[owa-vault] restored {len(cookies)} cookies ({age_h}h old) into {ep}")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "save" and len(argv) > 2:
        return save(argv[2])
    if cmd == "restore" and len(argv) > 2:
        return restore(argv[2], dry="--dry" in argv)
    if cmd == "status":
        s = newest()
        if not s:
            print("no snapshot")
            return 1
        print(f"newest {s.name} ({int((time.time() - s.stat().st_mtime) // 3600)}h old)")
        return 0
    print("usage: owa_session_vault.py save <ep> | restore <ep> [--dry] | status")
    return 64


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
