#!/usr/bin/env python3
"""outlook_session_watchdog -- Outlook twin of zoom_session_watchdog (added 2026-11-03).

Endpoints watched every tick (launchd com.hailports.outlook-session-watchdog, 180 s):
  * Mini :18822 -- the live OWA bridge (ops-draft-immortal / work-loop / owa health pin OWA_WEB_CDP here).
    SACRED: detect + alert only. Read-only: /json/list plus one Runtime.evaluate count. Never reload,
    restore, relaunch, click or touch read state.
  * box :9227 -- what bridge_endpoints.resolve('owa') returns under BRIDGES_REMOTE_PRIMARY. Watched only when
    an Outlook / Microsoft-login tab exists there (today the JS writers dead-sink :9227 because
    SAFARI_OPS_PRIMARY is set, so no tab = not served, not a break). Heals after 3 bad ticks:
    same-tab reload to outlook.office.com/mail (<=1/20 min); signed_out -> owa_session_vault restore
    then reload (<=1/30 min).
Classes: ok (message list rendered) | rows0 | signed_out | sso_error | page_missing | browser_down | not_served.
Still bad on the next bad tick after a heal (or any break on :18822 after 3 ticks) ->
authlink.login_needed('outlook') ONCE per break; all endpoints ok again -> authlink.recovered('outlook').
While :9227 or :18822 is ok, refresh the OWA cookie vault at most every 6 h (read-only Storage.getCookies).
Never sends mail, never changes read/unread state, never prints cookies.
Usage: outlook_session_watchdog.py [tick|--dry|status|self-test]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
RT = ROOT / "data" / "runtime"
STATE_PATH = Path(os.environ.get("OUTLOOK_WD_STATE", str(RT / "outlook_session_watchdog.json")))
LOG_PATH = Path(os.environ.get("OUTLOOK_WD_LOG", str(Path.home() / "Library/Logs/hailports/outlook-session-watchdog.log")))
MAIL_URL = "https://outlook.office.com/mail/"
SACRED = "http://127.0.0.1:18822"
THRESHOLD = 3
NAV_COOLDOWN_S = 20 * 60
VAULT_COOLDOWN_S = 30 * 60
VAULT_SAVE_EVERY_S = 6 * 3600
OUTLOOK_HOSTS = ("outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft", "outlook.live.com")
LOGIN_BITS = ("login.microsoftonline.com", "login.microsoft.com", "login.live.com", "/adfs/", "idp.corp.example.com",
              "idp2.corp.example.com", "sso.corp.example.com", "/owa/auth/", "logout")
PROBE_JS = r"""JSON.stringify({href:location.href,
rows:document.querySelectorAll('[data-convid]').length+document.querySelectorAll('[role=listbox] [role=option]').length,
mlist:!!document.querySelector('[aria-label*="Message list" i]'),
err:/HTTP ERROR \d{3}|This page isn.t working|can.t be reached|ERR_[A-Z_]+/.test((document.body&&document.body.innerText)||'').toString()})"""


def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as fh:
        fh.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _write_state(doc: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True))
    tmp.replace(STATE_PATH)


def box_endpoint() -> str:
    try:
        resolve = __import__("importlib").import_module("stack.bridge_endpoints").resolve
        return resolve("owa", force_probe=False).rstrip("/")
    except Exception:
        return "http://box.tailnet-host.example.invalid:9227"


def is_sacred(ep: str) -> bool:
    return ep.rstrip("/").endswith(":18822")


def classify(probe: dict | None, *, pages_ok: bool = True, has_tab: bool = True, optional: bool = False) -> str:
    """Pure classifier (unit-tested)."""
    if not pages_ok:
        return "browser_down"
    if not has_tab:
        return "not_served" if optional else "page_missing"
    if probe is None:
        return "page_missing"
    href = str(probe.get("href") or "").lower()
    if href.startswith("chrome-error://") or probe.get("err") in (True, "true"):
        return "sso_error"
    if any(b in href for b in LOGIN_BITS):
        return "signed_out"
    if not any(h in href for h in OUTLOOK_HOSTS):
        return "page_missing"
    if int(probe.get("rows") or 0) > 0 or probe.get("mlist") is True and int(probe.get("rows") or 0) > 0:
        return "ok"
    return "rows0"


def _pages(ep: str) -> list[dict] | None:
    try:
        with urllib.request.urlopen(ep + "/json/list", timeout=6) as r:
            return [p for p in json.load(r) if p.get("type") == "page"]
    except Exception:
        return None


def _mail_tabs(pages: list[dict]) -> list[dict]:
    out = [p for p in pages if any(h in str(p.get("url", "")) for h in OUTLOOK_HOSTS)]
    out += [p for p in pages if p not in out and any(b in str(p.get("url", "")).lower() for b in LOGIN_BITS[:3])]
    return out


def _eval(page: dict, expr: str, timeout_s: float = 8):
    import websocket
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=timeout_s, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True, "timeout": int(timeout_s * 1000)}}))
        end = time.monotonic() + timeout_s + 2
        while time.monotonic() < end:
            r = json.loads(ws.recv())
            if r.get("id") == 1:
                return ((r.get("result") or {}).get("result") or {}).get("value")
    finally:
        ws.close()
    return None


def _navigate(page: dict, url: str) -> None:
    import websocket
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}))
        ws.recv()
    finally:
        ws.close()


def probe(ep: str, *, optional: bool) -> tuple[str, dict]:
    pages = _pages(ep)
    if pages is None:
        return classify(None, pages_ok=False), {}
    tabs = _mail_tabs(pages)
    if not tabs:
        return classify(None, has_tab=False, optional=optional), {"tabs": 0}
    tab = tabs[0]
    data: dict = {"href": tab.get("url"), "tabs": len(tabs)}
    try:
        raw = _eval(tab, PROBE_JS)
        if raw:
            data.update(json.loads(raw))
    except Exception as exc:
        data["probe_error"] = type(exc).__name__
        data.setdefault("rows", 0)
    return classify(data), data


def step(st: dict, state: str, *, now: float) -> list[str]:
    """Pure per-endpoint state machine (unit-tested): returns heal | alert | recovered."""
    if state in ("ok", "not_served"):
        acts = ["recovered"] if st.get("alerted") else []
        st.update(consecutive_bad=0, break_since=None, healed_at=None, alerted=False, last_ok=now)
        return acts
    st["consecutive_bad"] = int(st.get("consecutive_bad") or 0) + 1
    if not st.get("break_since"):
        st["break_since"] = now
    if st["consecutive_bad"] < THRESHOLD:
        return []
    acts = []
    if not st.get("alerted") and (st.get("healed_at") or st.get("sacred")):
        acts.append("alert")
    if not st.get("sacred"):
        acts.append("heal")
    return acts


def heal(ep: str, state: str, st: dict, *, dry: bool) -> str:
    if is_sacred(ep):
        return "none_sacred"
    if state in ("browser_down", "page_missing"):
        return "none_no_restart_allowed"
    now = time.time()
    pages = _pages(ep) or []
    tabs = _mail_tabs(pages)
    if not tabs:
        return "none_no_tab"
    notes = []
    if state == "signed_out":
        if now - float(st.get("last_vault_at") or 0) < VAULT_COOLDOWN_S:
            return "vault_cooldown"
        st["last_vault_at"] = now
        cmd = [sys.executable, str(ROOT / "tools" / "owa_session_vault.py"), "restore", ep] + (["--dry"] if dry else [])
        rc = subprocess.run(cmd, capture_output=True, text=True, timeout=120).returncode
        notes.append(f"vault_restore_rc={rc}")
    elif now - float(st.get("last_nav_at") or 0) < NAV_COOLDOWN_S:
        return "nav_cooldown"
    st["last_nav_at"] = now
    if dry:
        notes.append("dry:reload_same_tab")
    else:
        _navigate(tabs[0], MAIL_URL)
        notes.append("reload_same_tab")
    return "+".join(notes)


def _authlink():
    import importlib.util
    spec = importlib.util.spec_from_file_location("authlink_mod", ROOT / "tools" / "authlink.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _maybe_save_vault(doc: dict, results: dict, *, dry: bool) -> str:
    now = time.time()
    if now - float(doc.get("last_vault_save") or 0) < VAULT_SAVE_EVERY_S:
        return ""
    src = next((ep for ep, (s, _) in results.items() if s == "ok" and not is_sacred(ep)), None) \
        or next((ep for ep, (s, _) in results.items() if s == "ok"), None)
    if not src:
        return ""
    if dry:
        return f"dry:vault_save:{src.rsplit(':', 1)[-1]}"
    rc = subprocess.run([sys.executable, str(ROOT / "tools" / "owa_session_vault.py"), "save", src],
                        capture_output=True, text=True, timeout=60).returncode
    if rc == 0:
        doc["last_vault_save"] = now
    return f"vault_save:{src.rsplit(':', 1)[-1]}:rc={rc}"


def tick(*, dry: bool = False) -> int:
    doc = _read_state()
    eps = {SACRED: False, box_endpoint(): True}       # endpoint -> optional?
    results = {}
    notes = []
    any_alert_open = bool(doc.get("alerted"))
    for ep, optional in eps.items():
        st = doc.setdefault("endpoints", {}).setdefault(ep, {})
        st["sacred"] = is_sacred(ep)
        state, data = probe(ep, optional=optional)
        results[ep] = (state, data)
        acts = step(st, state, now=time.time())
        for act in acts:
            if act == "heal":
                how = heal(ep, state, st, dry=dry)
                st["healed_at"] = st.get("healed_at") or time.time()
                st["last_heal"] = how
                notes.append(f"{ep.rsplit(':',1)[-1]}:heal={how}")
            elif act == "alert":
                if any_alert_open:
                    how = "already_open"
                else:
                    how = "dry" if dry else _authlink().login_needed("outlook")
                if how in ("sent", "deduped", "dry", "already_open"):
                    st["alerted"] = True
                    doc["alerted"] = True
                    any_alert_open = True
                notes.append(f"{ep.rsplit(':',1)[-1]}:alert={how}")
        st.update(last_state=state, last_rows=data.get("rows"), last_probe=time.time())
    if doc.get("alerted") and all(s in ("ok", "not_served") for s, _ in results.values()):
        how = "dry" if dry else _authlink().recovered("outlook")
        doc["alerted"] = False
        for st in doc["endpoints"].values():
            st["alerted"] = False
        notes.append(f"recovered={how}")
    v = _maybe_save_vault(doc, results, dry=dry)
    if v:
        notes.append(v)
    if not dry:
        _write_state(doc)
    line = " ".join(f"{ep.rsplit(':',1)[-1]}={s}(rows={d.get('rows')})" for ep, (s, d) in results.items())
    line = f"{line} {' '.join(notes)}".strip()
    _log(("DRY " if dry else "") + line)
    print(line)
    return 0 if all(s in ("ok", "not_served") for s, _ in results.values()) else 1


def self_test() -> int:
    assert classify(None, pages_ok=False) == "browser_down"
    assert classify(None, has_tab=False, optional=True) == "not_served"
    assert classify(None, has_tab=False) == "page_missing"
    assert classify({"href": "chrome-error://chromewebdata/"}) == "sso_error"
    assert classify({"href": "https://outlook.office.com/mail/", "err": "true"}) == "sso_error"
    assert classify({"href": "https://login.microsoftonline.com/common/oauth2/authorize"}) == "signed_out"
    assert classify({"href": "https://idp.corp.example.com/adfs/ls/?wa=wsignin1.0"}) == "signed_out"
    assert classify({"href": "https://www.bing.com/"}) == "page_missing"
    assert classify({"href": "https://outlook.cloud.microsoft/mail/", "rows": 0, "mlist": True}) == "rows0"
    assert classify({"href": "https://outlook.cloud.microsoft/mail/", "rows": 12, "mlist": True}) == "ok"
    # box endpoint: heal at 3rd bad, page on the next bad tick after the heal, once per break
    st: dict = {"sacred": False}
    assert step(st, "signed_out", now=1) == [] and step(st, "signed_out", now=2) == []
    assert step(st, "signed_out", now=3) == ["heal"]
    st["healed_at"] = 3
    assert step(st, "signed_out", now=4) == ["alert", "heal"]
    st["alerted"] = True
    assert step(st, "signed_out", now=5) == ["heal"]
    assert step(st, "ok", now=6) == ["recovered"] and st["consecutive_bad"] == 0
    # sacred :18822: never heals, pages at the 3rd bad tick
    sv: dict = {"sacred": True}
    assert step(sv, "rows0", now=1) == [] and step(sv, "rows0", now=2) == []
    assert step(sv, "rows0", now=3) == ["alert"]
    sv["alerted"] = True
    assert step(sv, "rows0", now=4) == []
    assert heal(SACRED, "signed_out", {}, dry=True) == "none_sacred"
    # optional box endpoint with no Outlook tab is never a break
    so: dict = {"sacred": False}
    assert all(step(so, "not_served", now=i) == [] for i in range(5))
    print("self-test ok")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "tick"
    if cmd == "self-test":
        return self_test()
    if cmd in ("--dry", "dry"):
        return tick(dry=True)
    if cmd == "status":
        print(json.dumps(_read_state(), indent=1))
        return 0
    return tick()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
