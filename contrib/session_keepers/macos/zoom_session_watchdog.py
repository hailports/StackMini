#!/usr/bin/env python3
"""zoom_session_watchdog -- catch the Zoom reader session going blind, heal in-tab, else page the operator once.

Added 2026-11-03 after the passive reader sat on zoom_passive_source_sidebar_absent (rows=0) from
~15:45 CT with nothing noticing. Root gaps it closes:
  * every existing probe (uptime_guard auth_tick, authlink scan, zoom_health_probe) watched the Mini
    Edge :18812 only, but under BRIDGES_REMOTE_PRIMARY the reader resolves Zoom to the remote box :9227;
  * they were URL-only: a signed-out landing page at example.zoom.us/wc/ ("Sign In / Sign Up") and a
    signed-in tab with zero conversation rows both looked healthy.

Every tick (launchd, 180 s):
  1. resolve the endpoint the readers use (stack.bridge_endpoints.resolve('zoom')).
  2. probe the reader DOM and, on an owned local browser, check home/chat renderer status.
  3. bad for MORE than 2 consecutive ticks (>=3) -> one in-tab soft recovery, existing stack paths only:
       rows0 + 'Chats & Channels' header found  -> one pointer click on that header
                                                  (zoom_web_send._DM_SECTION_STATE_JS / _expand_sections)
       rows0 stuck loading / sso_error          -> Page.navigate SAME tab to example.zoom.us/wc/team-chat (<=1 per 20 min)
       signed_out on a non-:18812 endpoint      -> tools/zoom_session_vault.py restore (<=1 per 30 min)
     :18812 page_missing for >=2 ticks -> PUT /json/new?team-chat (<=1 per 10 min; records last_open_at).
     Next tick uses normal logic (signed_out on a non-:18812 endpoint -> vault remint).
  4. still bad on the next bad tick after a heal -> authlink.login_needed('zoom') (private iMessage to the
     Mac's own handle with the Tailscale-only link), ONCE per break. ok again -> authlink.recovered('zoom').
HARD: never starts/kills/restarts any browser, never touches :18822, never types or sends in Zoom.
Set ZOOM_WD_HOST to the dedicated Zoom hostname. For local renderer recovery, also set
ZOOM_WD_BROWSER_LABEL to the browser LaunchAgent label. A reload is limited to the
same failed home or team-chat page on its verified loopback listener.
Usage: zoom_session_watchdog.py [tick|--dry|status|self-test]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from urllib.parse import SplitResult, urlsplit
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
RT = ROOT / "data" / "runtime"
STATE_PATH = Path(os.environ.get("ZOOM_WD_STATE", str(RT / "zoom_session_watchdog.json")))
LOG_PATH = Path(os.environ.get("ZOOM_WD_LOG", str(Path.home() / "Library/Logs/hailports/zoom-session-watchdog.log")))
ZOOM_HOST = os.environ.get("ZOOM_WD_HOST", "example.zoom.us").lower()
if not re.fullmatch(r"[a-z0-9-]+\.zoom\.us", ZOOM_HOST):
    raise ValueError("ZOOM_WD_HOST must be a single Zoom subdomain")
TEAM_CHAT = f"https://{ZOOM_HOST}/wc/team-chat"
MINI_ZOOM = "http://127.0.0.1:18812"
FORBIDDEN_PORTS = {"18822"}
THRESHOLD = 3            # "more than 2 consecutive checks"
PAGE_MISSING_THRESHOLD = 2  # :18812 open-tab after >=2 missing ticks
NAV_COOLDOWN_S = 6 * 60   # 2026-11-03: box :9227 Team Chat hang cleared on a later same-tab reload; retry sooner
VAULT_COOLDOWN_S = 30 * 60
OPEN_TAB_COOLDOWN_S = 10 * 60
BAD = ("rows0", "signed_out", "sso_error", "page_missing", "browser_down")
IDP_HOSTS = {"login.microsoftonline.com", "accounts.google.com", "idp.corp.example.com", "idp2.corp.example.com"}

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


def _https_route(url: str) -> SplitResult | None:
    try:
        route = urlsplit(url)
        if route.scheme == "https" and route.hostname and route.netloc.lower() in (
            route.hostname, f"{route.hostname}:443"
        ):
            return route
    except (TypeError, ValueError):
        pass
    return None


def _zoom_host(host: str) -> bool:
    return bool(re.fullmatch(r"(?:[a-z0-9-]+\.)*zoom\.us", host))


def classify(probe: dict | None, *, pages_ok: bool = True) -> str:
    """Pure classifier (unit-tested). probe = PROBE_JS result for the Zoom tab, or None."""
    if not pages_ok:
        return "browser_down"
    if probe is None:
        return "page_missing"
    href = probe.get("href") or ""
    if href == "chrome-error://chromewebdata/":
        return "sso_error"
    route = _https_route(href)
    if not route:
        return "page_missing"
    host = route.hostname
    path_root = route.path.strip("/").partition("/")[0].lower()
    if host in IDP_HOSTS or (_zoom_host(host) and path_root in {"signin", "login", "saml", "sso"}):
        return "signed_out"
    if host != ZOOM_HOST:
        return "page_missing"
    if probe.get("err") in (True, "true"):
        return "sso_error"
    if probe.get("signin_btns") and not probe.get("workplace"):
        return "signed_out"
    if int(probe.get("rows") or 0) > 0:
        return "ok"
    return "rows0"


def _ws(page: dict, timeout: float):
    import websocket
    return websocket.create_connection(page["webSocketDebuggerUrl"], timeout=timeout, suppress_origin=True)


class _Tab:
    def __init__(self, page: dict, timeout: float = 20):
        self.ws = _ws(page, timeout)
        self.timeout = timeout
        self.n = 0
        self.crashed = False

    def call(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        deadline = time.monotonic() + self.timeout
        while (remaining := deadline - time.monotonic()) > 0:
            self.ws.settimeout(remaining)
            r = json.loads(self.ws.recv())
            if r.get("method") == "Inspector.targetCrashed":
                self.crashed = True
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


def _pages(ep: str) -> list[dict]:
    with urllib.request.urlopen(ep + "/json/list", timeout=6) as resp:
        return [r for r in json.load(resp) if r.get("type") == "page"]


def _zoom_page(ep: str, rows: list[dict] | None = None) -> tuple[bool, dict | None]:
    try:
        rows = _pages(ep) if rows is None else rows
    except Exception:
        return False, None
    candidates = []
    for page in rows:
        route = _https_route(page.get("url") or "")
        if route and _zoom_host(route.hostname):
            priority = (route.hostname != ZOOM_HOST, route.path.rstrip("/") != "/wc/team-chat")
            candidates.append((priority, page))
    selected = min(candidates, key=lambda item: item[0])[1] if candidates else None
    return True, selected


def _managed_route(url: str) -> bool:
    route = _https_route(url)
    return bool(route and route.hostname == ZOOM_HOST and
                route.path.rstrip("/") in ("/wc/home", "/wc/team-chat") and not route.query and not route.fragment)


def _managed_target(page: dict) -> bool:
    target = page.get("id")
    if not isinstance(target, str) or not target.isalnum() or not _managed_route(page.get("url") or ""):
        return False
    return page.get("webSocketDebuggerUrl") == MINI_ZOOM.replace("http:", "ws:") + "/devtools/page/" + target


def _owns_listener(ep: str) -> bool:
    label = os.environ.get("ZOOM_WD_BROWSER_LABEL", "")
    if ep != MINI_ZOOM or not re.fullmatch(r"[A-Za-z0-9._-]+", label):
        return False
    try:
        job = subprocess.check_output(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
                                      text=True, stderr=subprocess.DEVNULL, timeout=3)
        pid = re.search(r"^\s*pid = ([1-9][0-9]*)\s*$", job, re.M)
        if not pid:
            return False
        port = urlsplit(ep).port
        listeners = subprocess.check_output(
            ["/usr/sbin/lsof", "-nP", "-a", "-p", pid[1], f"-iTCP:{port}", "-sTCP:LISTEN", "-Fn"],
            text=True, stderr=subprocess.DEVNULL, timeout=3)
        return {line[1:] for line in listeners.splitlines() if line.startswith("n")} == {f"127.0.0.1:{port}"}
    except (OSError, subprocess.SubprocessError):
        return False


def _renderer_fault(tab: _Tab) -> bool:
    try:
        tab.call("Inspector.enable")
        if tab.crashed:
            return True
        reply = tab.call("Page.getFrameTree")
        frame = reply.get("result", {}).get("frameTree", {}).get("frame", {})
        return tab.crashed or _managed_route(frame.get("unreachableUrl") or "")
    except Exception:
        # A slow page alone gives no evidence that it crashed.
        return tab.crashed


def probe(ep: str) -> tuple[str, dict]:
    try:
        pages = _pages(ep)
    except Exception:
        return "browser_down", {"page": None}
    if _owns_listener(ep):
        for candidate in pages:
            if not _managed_target(candidate):
                continue
            tab = None
            try:
                tab = _Tab(candidate, timeout=3)
                if _renderer_fault(tab):
                    return "page_crashed", {"target": candidate["id"]}
            except Exception:
                pass
            finally:
                if tab is not None:
                    tab.close()

    _, page = _zoom_page(ep, pages)
    if page is None:
        return "page_missing", {"page": None}
    tab = None
    target = str(page.get("id") or "")
    try:
        tab = _Tab(page)
        raw = tab.ev(PROBE_JS)
        data = json.loads(raw) if raw else None
        if not isinstance(data, dict):
            return "probe_error", {"target": target}
    except Exception as exc:
        return "probe_error", {"target": target, "probe_error": type(exc).__name__}
    finally:
        if tab is not None:
            tab.close()
    data["target"] = target
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


def _open_team_chat_tab(ep: str) -> str:
    """Open one Team Chat tab via CDP /json/new. Never restarts the browser."""
    _guard(ep)
    req = urllib.request.Request(f"{ep.rstrip('/')}/json/new?{TEAM_CHAT}", method="PUT")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()
    return "opened_team_chat_tab"


def heal(ep: str, state: str, st: dict, *, dry: bool, target: str | None = None) -> str:
    _guard(ep)
    now = _now()
    if state == "page_crashed":
        if not target or not _owns_listener(ep):
            return "reload_unowned"
        if st.get("reloaded_target") == target and now - st.get("last_reload_at", 0) < NAV_COOLDOWN_S:
            return "reload_cooldown"
        if dry:
            return "would_reload_crashed_tab"
        tab = None
        try:
            page = next((p for p in _pages(ep) if p.get("id") == target and _managed_target(p)), None)
            if page is None:
                return "reload_target_changed"
            tab = _Tab(page, timeout=3)
            if not _renderer_fault(tab):
                return "reload_fault_cleared"
            st.update(reloaded_target=target, last_reload_at=now)
            reply = tab.call("Page.reload")
            return "reloaded_tab" if "result" in reply else "reload_failed"
        except Exception as exc:
            return f"reload_failed={type(exc).__name__}"
        finally:
            if tab is not None:
                tab.close()
    # :18812 page_missing: open one Team Chat tab (rate-limited). No restart.
    if state == "page_missing" and ep.rstrip("/").endswith(":18812"):
        if now - float(st.get("last_open_at") or 0) < OPEN_TAB_COOLDOWN_S:
            return "open_tab_cooldown"
        if dry:
            return "would_open_team_chat_tab"
        try:
            how = _open_team_chat_tab(ep)
        except Exception as exc:
            return f"open_tab_err={type(exc).__name__}"
        st["last_open_at"] = now
        return how
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
    _, page = _zoom_page(ep)
    if not page or not target or page.get("id") != target or not _managed_route(page.get("url") or ""):
        return "none_no_page"
    if dry:
        return "would_soft_heal"
    with _reader_lock() as held:
        if not held:
            return "busy_lock_held"
        tab = _Tab(page)
        try:
            frame_tree = tab.call("Page.getFrameTree")
            frame = frame_tree.get("result", {}).get("frameTree", {}).get("frame", {})
            if not _managed_route(frame.get("unreachableUrl") or frame.get("url") or ""):
                return "none_target_changed"
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
    if state == "probe_error":
        return []
    if state == "page_crashed":
        return ["heal"]
    if state == "ok":
        if st.get("alerted"):
            actions.append("recovered")
        st.update(consecutive_bad=0, break_since=None, healed_at=None, alerted=False, last_ok=now)
        return actions
    st["consecutive_bad"] = int(st.get("consecutive_bad") or 0) + 1
    st.setdefault("break_since", None)
    if not st.get("break_since"):
        st["break_since"] = now
    need = PAGE_MISSING_THRESHOLD if state == "page_missing" else THRESHOLD
    if st["consecutive_bad"] < need:
        return actions
    if state == "page_missing":
        # Open tab only; alert/remint wait for the next tick's real state.
        actions.append("heal")
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
            how = heal(ep, state, st, dry=dry, target=data.get("target"))
            if state != "page_crashed":
                st["healed_at"] = st.get("healed_at") or now
            st["last_heal"] = how
            notes.append(f"heal={how}")
            if not dry and how.startswith(("expand", "navigate", "vault", "opened", "reloaded_tab")):
                time.sleep(20)
                state2, checked = probe(ep)
                notes.append(f"recheck={state2}")
                if state2 == "ok":
                    state, data = state2, checked
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
    assert classify({"href": f"https://{ZOOM_HOST}/wc/", "signin_btns": True, "workplace": False, "rows": 0}) == "signed_out"
    assert classify({"href": TEAM_CHAT, "workplace": True, "rows": 0}) == "rows0"
    assert classify({"href": TEAM_CHAT, "workplace": True, "rows": 27}) == "ok"
    assert classify({"href": TEAM_CHAT, "err": "true"}) == "sso_error"
    st: dict = {}
    assert step(st, "rows0", now=1) == [] and step(st, "rows0", now=2) == []
    assert step(st, "rows0", now=3) == ["heal"]            # 3rd consecutive bad -> soft heal
    st["healed_at"] = 3
    assert step(st, "rows0", now=4) == ["alert", "heal"]    # heal did not fix -> page once
    st["alerted"] = True
    assert step(st, "rows0", now=5) == ["heal"]             # no second page this break; heal keeps trying
    assert step(st, "ok", now=6) == ["recovered"] and st["consecutive_bad"] == 0
    assert step(st, "signed_out", now=7) == [] and step(st, "ok", now=8) == []   # blip <3 never acts
    # page_missing: open one Team Chat tab at 2nd tick; cooldown via last_open_at
    pm: dict = {}
    assert step(pm, "page_missing", now=1) == []
    assert step(pm, "page_missing", now=2) == ["heal"]
    assert heal(MINI_ZOOM, "page_missing", {}, dry=True) == "would_open_team_chat_tab"
    assert heal(MINI_ZOOM, "page_missing", {"last_open_at": 1e12}, dry=True) == "open_tab_cooldown"
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
