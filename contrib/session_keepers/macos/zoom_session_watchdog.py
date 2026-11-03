#!/usr/bin/env python3
"""zoom_session_watchdog -- catch the Zoom reader session going blind, heal in-tab, else page the operator once.

Added 2026-11-03 after the passive reader sat on zoom_passive_source_sidebar_absent (rows=0) from
~15:45 CT with nothing noticing. Root gaps it closes:
  * every existing probe (uptime_guard auth_tick, authlink scan, zoom_health_probe) watched the Mini
    Edge :18812 only, but under BRIDGES_REMOTE_PRIMARY the reader resolves Zoom to the remote box :9227;
  * they were URL-only: a signed-out landing page at app.zoom.us/wc/ ("Sign In / Sign Up") and a
    signed-in tab with zero conversation rows both looked healthy.

Every tick (launchd, 180 s):
  1. resolve the endpoint the readers use (stack.bridge_endpoints.resolve('zoom')).
  2. read-only CDP (Runtime.evaluate only): ok | rows0 | signed_out | sso_error | page_missing | browser_down.
  3. bad for MORE than 2 consecutive ticks (>=3) -> one in-tab soft recovery, existing stack paths only:
       rows0 + 'Chats & Channels' header found  -> one pointer click on that header
                                                  (zoom_web_send._DM_SECTION_STATE_JS / _expand_sections)
       rows0 stuck loading / sso_error          -> Page.navigate SAME tab to app.zoom.us/wc/team-chat (<=1 per 20 min)
       signed_out on a non-:18812 endpoint      -> tools/zoom_session_vault.py restore (<=1 per 30 min)
  4. still bad on the next bad tick after a heal -> authlink.login_needed('zoom') (private iMessage to the
     Mac's own handle with the Tailscale-only link), ONCE per break. ok again -> authlink.recovered('zoom').
HARD: never starts/kills/restarts any browser, never touches :18822, never types or sends in Zoom.
Usage: zoom_session_watchdog.py [tick|--dry|status|self-test]
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
STATE_PATH = Path(os.environ.get("ZOOM_WD_STATE", str(RT / "zoom_session_watchdog.json")))
LOG_PATH = Path(os.environ.get("ZOOM_WD_LOG", str(Path.home() / "Library/Logs/hailports/zoom-session-watchdog.log")))
TEAM_CHAT = "https://app.zoom.us/wc/team-chat"
MINI_ZOOM = "http://127.0.0.1:18812"
FORBIDDEN_PORTS = {"18822"}
THRESHOLD = 3            # "more than 2 consecutive checks"
NAV_COOLDOWN_S = 6 * 60   # 2026-11-03: box :9227 Team Chat hang cleared on a later same-tab reload; retry sooner
VAULT_COOLDOWN_S = 30 * 60
BAD = ("rows0", "signed_out", "sso_error", "page_missing", "browser_down")
LOGIN_BITS = ("zoom.us/signin", "zoom.us/login", "zoom.us/saml", "login.microsoftonline", "/adfs/",
              "idp.corp.example.com", "idp2.corp.example.com", "accounts.google.com")

PROBE_JS = r"""(()=>{const b=document.body;const t=b?(b.innerText||''):'';
return JSON.stringify({href:location.href,title:document.title,
rows:document.querySelectorAll('li[class*="_sessionItem_"]').length,
signin_btns:/(^|\n)\s*Sign In\s*(\n|$)/.test(t)&&/(^|\n)\s*Sign Up\s*(\n|$)/.test(t),
workplace:/Workplace/.test(t), chat_rail:!!document.querySelector('[role=tab]') && /(^|\n)\s*Chat\s*(\n|$)/.test(t),
err:/HTTP ERROR \d{3}|This page isn.t working|can.t be reached|ERR_[A-Z_]+/.test(t).toString(),
text:t.slice(0,160)})})()"""


def _now() -> float:
    return time.time()


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


def endpoint() -> str:
    explicit = (os.environ.get("ZOOM_WEB_CDP") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        resolve = __import__("importlib").import_module("stack.bridge_endpoints").resolve
        return resolve("zoom", force_probe=False).rstrip("/")
    except Exception:
        return MINI_ZOOM


def _guard(ep: str) -> None:
    if any(f":{p}" in ep for p in FORBIDDEN_PORTS):
        raise SystemExit(f"refusing forbidden endpoint {ep}")


def classify(probe: dict | None, *, pages_ok: bool = True) -> str:
    """Pure classifier (unit-tested). probe = PROBE_JS result for the Zoom tab, or None."""
    if not pages_ok:
        return "browser_down"
    if probe is None:
        return "page_missing"
    href = str(probe.get("href") or "").lower()
    if href.startswith("chrome-error://") or probe.get("err") in (True, "true"):
        return "sso_error"
    if any(bit in href for bit in LOGIN_BITS):
        return "signed_out"
    if "app.zoom.us" not in href:
        return "page_missing"
    if probe.get("signin_btns") and not probe.get("workplace"):
        return "signed_out"
    if int(probe.get("rows") or 0) > 0:
        return "ok"
    return "rows0"


def _ws(page: dict):
    import websocket
    return websocket.create_connection(page["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)


class _Tab:
    def __init__(self, page: dict):
        self.ws = _ws(page)
        self.n = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            r = json.loads(self.ws.recv())
            if r.get("id") == self.n:
                return r
        return {}

    def ev(self, expr: str):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return ((r.get("result") or {}).get("result") or {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _zoom_page(ep: str) -> tuple[bool, dict | None]:
    try:
        with urllib.request.urlopen(ep + "/json/list", timeout=6) as resp:
            rows = json.load(resp)
    except Exception:
        return False, None
    pages = [r for r in rows if r.get("type") == "page" and "zoom.us" in str(r.get("url") or "")]
    pages.sort(key=lambda r: ("team-chat" not in r.get("url", ""), "app.zoom.us" not in r.get("url", "")))
    if not pages:
        errs = [r for r in rows if r.get("type") == "page" and str(r.get("url", "")).startswith("chrome-error")]
        return True, (errs[0] if errs else None)
    return True, pages[0]


def probe(ep: str) -> tuple[str, dict]:
    pages_ok, page = _zoom_page(ep)
    if not pages_ok or page is None:
        return classify(None, pages_ok=pages_ok), {"page": None}
    tab = _Tab(page)
    try:
        raw = tab.ev(PROBE_JS)
        data = json.loads(raw) if raw else {"href": page.get("url")}
    except Exception as exc:
        data = {"href": page.get("url"), "probe_error": type(exc).__name__}
    finally:
        tab.close()
    data["target"] = str(page.get("id") or "")[:8]
    return classify(data), data


def _dm_js() -> str | None:
    import re
    try:
        src = (ROOT / "tools" / "zoom_web_send.py").read_text()
        m = re.search(r'_DM_SECTION_STATE_JS = r"""(.*?)"""', src, re.S)
        return m.group(1) if m else None
    except Exception:
        return None


def _reader_lock():
    from core import zoom_transport_lock
    return zoom_transport_lock.reader_transport_lock(timeout=8)


def heal(ep: str, state: str, st: dict, *, dry: bool) -> str:
    _guard(ep)
    now = _now()
    if state in ("browser_down", "page_missing"):
        return "none_no_restart_allowed"
    if state == "signed_out":
        if ep.endswith(":18812"):
            return "none_mini_signed_out"
        if now - float(st.get("last_vault_at") or 0) < VAULT_COOLDOWN_S:
            return "vault_cooldown"
        if dry:
            return "would_vault_restore"
        st["last_vault_at"] = now
        rc = subprocess.run([sys.executable, str(ROOT / "tools" / "zoom_session_vault.py"), "restore", "--quiet"],
                            cwd=str(ROOT), env={**os.environ, "ZOOM_WEB_CDP": ep},
                            capture_output=True, text=True, timeout=240).returncode
        return f"vault_restore_rc={rc}"
    pages_ok, page = _zoom_page(ep)
    if not page:
        return "none_no_page"
    if dry:
        return "would_soft_heal"
    with _reader_lock() as held:
        if not held:
            return "busy_lock_held"
        tab = _Tab(page)
        try:
            js = _dm_js()
            dm = tab.ev("(" + js + ")()") if (js and state == "rows0") else None
            if isinstance(dm, dict) and dm.get("found") and not dm.get("expanded"):
                for typ in ("mousePressed", "mouseReleased"):
                    tab.call("Input.dispatchMouseEvent", {"type": typ, "x": dm["x"], "y": dm["y"],
                                                          "button": "left", "clickCount": 1})
                time.sleep(3)
                rows = tab.ev('document.querySelectorAll(\'li[class*="_sessionItem_"]\').length')
                return f"expand_dm_section rows={rows}"
            if now - float(st.get("last_nav_at") or 0) < NAV_COOLDOWN_S:
                return "nav_cooldown"
            st["last_nav_at"] = now
            tab.call("Page.navigate", {"url": TEAM_CHAT})
            return "navigate_same_tab_team_chat"
        finally:
            tab.close()


def _authlink():
    import importlib.util
    spec = importlib.util.spec_from_file_location("authlink_mod", ROOT / "tools" / "authlink.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def step(st: dict, state: str, *, now: float) -> list[str]:
    """Pure state machine (unit-tested). Mutates st, returns actions: heal | alert | recovered."""
    actions: list[str] = []
    if state == "ok":
        if st.get("alerted"):
            actions.append("recovered")
        st.update(consecutive_bad=0, break_since=None, healed_at=None, alerted=False, last_ok=now)
        return actions
    st["consecutive_bad"] = int(st.get("consecutive_bad") or 0) + 1
    st.setdefault("break_since", None)
    if not st.get("break_since"):
        st["break_since"] = now
    if st["consecutive_bad"] < THRESHOLD:
        return actions
    if st.get("healed_at") and not st.get("alerted"):
        actions.append("alert")
    actions.append("heal")   # cooldowns inside heal() bound the rate
    return actions


def tick(*, dry: bool = False) -> int:
    ep = endpoint()
    _guard(ep)
    st = _read_state()
    state, data = probe(ep)
    now = _now()
    actions = step(st, state, now=now)
    notes = []
    for act in actions:
        if act == "alert":
            how = "dry" if dry else _authlink().login_needed("zoom")
            if how in ("sent", "deduped", "dry"):
                st["alerted"] = True
                st["alerted_at"] = now
            notes.append(f"alert={how}")
        elif act == "heal":
            how = heal(ep, state, st, dry=dry)
            st["healed_at"] = st.get("healed_at") or now
            st["last_heal"] = how
            notes.append(f"heal={how}")
            if not dry and how.startswith(("expand", "navigate", "vault")):
                time.sleep(20)
                state2, _ = probe(ep)
                notes.append(f"recheck={state2}")
                if state2 == "ok":
                    if "recovered" in step(st, "ok", now=_now()):
                        notes.append(f"recovered={_authlink().recovered('zoom')}")
        elif act == "recovered":
            how = "dry" if dry else _authlink().recovered("zoom")
            notes.append(f"recovered={how}")
    st.update(endpoint=ep, last_state=state, last_probe=now, last_target=data.get("target"),
              last_rows=data.get("rows"))
    if not dry:
        _write_state(st)
    line = f"ep={ep} state={state} rows={data.get('rows')} bad={st.get('consecutive_bad')} {' '.join(notes)}".strip()
    _log(("DRY " if dry else "") + line)
    print(line)
    return 0 if state == "ok" else 1


def self_test() -> int:
    assert classify(None, pages_ok=False) == "browser_down"
    assert classify(None) == "page_missing"
    assert classify({"href": "chrome-error://chromewebdata/"}) == "sso_error"
    assert classify({"href": "https://idp.corp.example.com/adfs/ls/wia"}) == "signed_out"
    assert classify({"href": "https://app.zoom.us/wc/", "signin_btns": True, "workplace": False, "rows": 0}) == "signed_out"
    assert classify({"href": "https://app.zoom.us/wc/team-chat", "workplace": True, "rows": 0}) == "rows0"
    assert classify({"href": "https://app.zoom.us/wc/team-chat", "workplace": True, "rows": 27}) == "ok"
    assert classify({"href": "https://app.zoom.us/wc/team-chat", "err": "true"}) == "sso_error"
    st: dict = {}
    assert step(st, "rows0", now=1) == [] and step(st, "rows0", now=2) == []
    assert step(st, "rows0", now=3) == ["heal"]            # 3rd consecutive bad -> soft heal
    st["healed_at"] = 3
    assert step(st, "rows0", now=4) == ["alert", "heal"]    # heal did not fix -> page once
    st["alerted"] = True
    assert step(st, "rows0", now=5) == ["heal"]             # no second page this break; heal keeps trying
    assert step(st, "ok", now=6) == ["recovered"] and st["consecutive_bad"] == 0
    assert step(st, "signed_out", now=7) == [] and step(st, "ok", now=8) == []   # blip <3 never acts
    try:
        _guard("http://127.0.0.1:18822")
        raise AssertionError("18822 guard failed")
    except SystemExit:
        pass
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
