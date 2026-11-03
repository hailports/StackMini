#!/usr/bin/env python3
"""zoom_post_launch -- after ensure_chrome_cdp.sh relaunches the :9227 Chrome, put Zoom back.

Added 2026-11-03 (the operator approved 4:43 PM CT). Steps, all idempotent and quiet when healthy:
  1. wait for CDP on 127.0.0.1:9227 (only this port; never 18812/18822/others).
  2. exactly one app.zoom.us/wc/team-chat tab: close extras (the launcher's --new-window plus
     session restore made two on 2026-11-03); never opens a duplicate; if none exists, navigate an
     existing zoom tab (or open one only when there is no zoom tab at all).
  3. classify like the Mini zoom_session_watchdog (ok | rows0 | signed_out | sso_error | page_missing).
  4. signed_out -> run the existing Mini tools/zoom_session_vault.py restore targeting :9227
     (newest snapshot ~/.zoom_web_session_vault/cookies.json) over the Tailscale ssh hop.
  5. rows0 -> one pointer click on the collapsed 'Chats & Channels' header.
Never types or sends anything in Zoom. Never restarts or kills a browser.
Usage: zoom_post_launch.py [run|check|--dry [--simulate STATE] [--simulate-outlook STATE]|self-test]
Outlook: see outlook_pass() -- same idea for an Outlook tab on :9227 (vault restore via Mini owa_session_vault.py).
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

PORT = 9227
BASE = f"http://127.0.0.1:{PORT}"
TEAM_CHAT = "https://app.zoom.us/wc/team-chat"
LOG = "/tmp/zoom_post_launch.log"
LOCK = "/tmp/zoom_post_launch.lock"
MINI_USER = "operator"
MINI_HOST = "mini.tailnet-host.example.invalid"
MINI = "@".join((MINI_USER, MINI_HOST))  # ssh target
SOCKS = "/opt/hailports/keeper/ts_socks.py"
LOGIN_BITS = ("zoom.us/signin", "zoom.us/login", "zoom.us/saml", "login.microsoftonline", "/adfs/",
              "idp.corp.example.com", "idp2.corp.example.com", "accounts.google.com")
ROWS_JS = 'document.querySelectorAll(\'li[class*="_sessionItem_"]\').length'
PROBE_JS = r"""(()=>{const b=document.body;const t=b?(b.innerText||''):'';
return JSON.stringify({href:location.href,
rows:document.querySelectorAll('li[class*="_sessionItem_"]').length,
signin_btns:/(^|\n)\s*Sign In\s*(\n|$)/.test(t)&&/(^|\n)\s*Sign Up\s*(\n|$)/.test(t),
workplace:/Workplace/.test(t),
err:/HTTP ERROR \d{3}|This page isn.t working|can.t be reached|ERR_[A-Z_]+/.test(t).toString()})})()"""
# Same header finder as Mini tools/zoom_web_send.py _DM_SECTION_STATE_JS.
DM_JS = r"""(()=>{const labels=new Set(['dms and channels','chats & channels','chats and channels']);
const label=[...document.querySelectorAll('span')].find(e=>e.offsetParent!==null&&labels.has(String(e.textContent||'').trim().toLowerCase()));
const header=label&&(label.closest('[class*="_groupItemWrapper_"]')||label.closest('[aria-expanded]'));
if(!header)return {found:false,expanded:false};const hr=header.getBoundingClientRect();
const rows=[...document.querySelectorAll('li[class*="_sessionItem_"]')].filter(e=>e.offsetParent!==null).map(e=>e.getBoundingClientRect());
return {found:true,expanded:rows.some(r=>r.top>=hr.bottom-1),x:Math.round(hr.left+Math.min(60,hr.width/2)),y:Math.round(hr.top+hr.height/2)};})()"""

QUIET = False
OUTLOOK_SIMULATE: str | None = None


def log(msg: str, *, always: bool = False) -> None:
    if QUIET and not always:
        return
    with open(LOG, "a") as fh:
        fh.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}\n")


def classify(probe: dict | None, *, pages_ok: bool = True) -> str:
    """Same rules as Mini tools/zoom_session_watchdog.classify."""
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


def plan(state: str, n_team_chat_tabs: int) -> list[str]:
    """Pure decision table (unit-tested)."""
    acts: list[str] = []
    if n_team_chat_tabs > 1:
        acts.append("close_extra_tabs")
    if state == "signed_out":
        acts.append("vault_restore")
    elif state == "rows0":
        acts.append("expand_dm")
    elif state in ("page_missing", "sso_error"):
        acts.append("navigate_team_chat")
    return acts


def _list() -> list[dict] | None:
    try:
        with urllib.request.urlopen(BASE + "/json/list", timeout=5) as r:
            return json.load(r)
    except Exception:
        return None


class Tab:
    def __init__(self, page: dict):
        import websocket
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
        self.n = 0

    def call(self, m: str, p: dict | None = None) -> dict:
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": m, "params": p or {}}))
        end = time.monotonic() + 30
        while time.monotonic() < end:
            r = json.loads(self.ws.recv())
            if r.get("id") == self.n:
                return r
        return {}

    def ev(self, e: str):
        return ((self.call("Runtime.evaluate", {"expression": e, "returnByValue": True}).get("result") or {})
                .get("result") or {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wait_cdp(secs: int = 60) -> list[dict] | None:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        rows = _list()
        if rows is not None:
            return rows
        time.sleep(2)
    return None


def zoom_tabs(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    pages = [r for r in rows if r.get("type") == "page"]
    tc = [r for r in pages if str(r.get("url", "")).startswith(TEAM_CHAT)]
    zm = [r for r in pages if "zoom.us" in str(r.get("url", ""))]
    return tc, zm


def probe_tab(page: dict) -> tuple[str, dict]:
    t = Tab(page)
    try:
        raw = t.ev(PROBE_JS)
        d = json.loads(raw) if raw else {"href": page.get("url")}
    finally:
        t.close()
    return classify(d), d


def settle_probe(page: dict, secs: int = 45) -> tuple[str, dict]:
    """A just-launched tab needs time to hydrate; take the first non-rows0 verdict or the last one."""
    end = time.monotonic() + secs
    state, d = probe_tab(page)
    while state == "rows0" and time.monotonic() < end:
        time.sleep(3)
        state, d = probe_tab(page)
        if state == "rows0":
            t = Tab(page)
            try:
                dm = t.ev(DM_JS)
            finally:
                t.close()
            if isinstance(dm, dict) and dm.get("found"):
                break  # chat list painted, just collapsed
    return state, d


def vault_restore(dry: bool) -> str:
    cmd = ["ssh", "-o", f"ProxyCommand=python3 {SOCKS} %h %p", "-o", "ConnectTimeout=20",
           "-o", "BatchMode=yes", MINI,
           "cd ~/hailports-stack && ZOOM_WEB_CDP=http://box.tailnet-host.example.invalid:9227 "
           "/opt/homebrew/bin/python3 tools/zoom_session_vault.py restore --quiet; echo rc=$?"]
    if dry:
        return "dry:vault_restore"
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
    except Exception as exc:
        return f"vault_error:{type(exc).__name__}"
    rc = [ln for ln in out.splitlines() if ln.startswith("rc=")]
    return "vault_" + (rc[-1] if rc else "rc=?")


def expand_dm(page: dict) -> str:
    t = Tab(page)
    try:
        dm = t.ev(DM_JS)
        if not (isinstance(dm, dict) and dm.get("found") and not dm.get("expanded")):
            return "expand_skip"
        for typ in ("mousePressed", "mouseReleased"):
            t.call("Input.dispatchMouseEvent", {"type": typ, "x": dm["x"], "y": dm["y"], "button": "left", "clickCount": 1})
        for _ in range(10):
            time.sleep(0.5)
            n = t.ev(ROWS_JS)
            if n:
                return f"expanded rows={n}"
        return "expand_no_rows"
    finally:
        t.close()


def run(*, dry: bool = False, simulate: str | None = None) -> int:
    rows = wait_cdp()
    if rows is None:
        log(f"cdp :{PORT} not up after wait; nothing done", always=True)
        return 2
    tc, zm = zoom_tabs(rows)
    keep = tc[0] if tc else (zm[0] if zm else None)
    if keep is None:
        state, d = classify(None), {}
    else:
        state, d = settle_probe(keep, 5 if simulate else 45)
    if simulate:
        state = simulate
    acts = plan(state, len(tc))
    notes = []
    for act in acts:
        if act == "close_extra_tabs":
            for extra in tc[1:]:
                if dry:
                    notes.append(f"dry:close {extra['id'][:8]}")
                    continue
                try:
                    urllib.request.urlopen(BASE + "/json/close/" + extra["id"], timeout=5).read()
                    notes.append(f"closed {extra['id'][:8]}")
                except Exception as exc:
                    notes.append(f"close_fail {type(exc).__name__}")
        elif act == "vault_restore":
            notes.append(vault_restore(dry))
        elif act == "navigate_team_chat":
            if dry:
                notes.append("dry:navigate")
            elif keep is not None:
                t = Tab(keep)
                try:
                    t.call("Page.navigate", {"url": TEAM_CHAT})
                finally:
                    t.close()
                notes.append("navigated")
            else:
                urllib.request.urlopen(urllib.request.Request(BASE + "/json/new?" + TEAM_CHAT, method="PUT"), timeout=10).read()
                notes.append("opened_team_chat")
        elif act == "expand_dm":
            notes.append("dry:expand" if dry else expand_dm(keep))
    final = state
    if acts and not dry and keep is not None:
        time.sleep(5)
        lst = _list() or []
        tc2, zm2 = zoom_tabs(lst)
        tgt = tc2[0] if tc2 else (zm2[0] if zm2 else None)
        if tgt is not None:
            final, d2 = settle_probe(tgt, 45)
            if final == "rows0":
                notes.append(expand_dm(tgt))
                final, _ = probe_tab(tgt)
    o_sim = OUTLOOK_SIMULATE
    o_state, o_acts, o_notes = outlook_pass(dry=dry, simulate=o_sim)
    noop = not acts and not o_acts
    notes += [f"outlook={o_state}{' (simulated)' if o_sim else ''}"] + o_notes if (o_acts or o_sim or not QUIET) else []
    msg = (f"{'DRY ' if dry else ''}state={state}{' (simulated)' if simulate else ''} team_chat_tabs={len(tc)} "
           f"rows={d.get('rows') if isinstance(d, dict) else None} actions={acts or 'none'} {' '.join(notes)} final={final}")
    if noop:
        log(msg)            # quiet unless verbose/check
    else:
        log(msg, always=True)
    print(msg)
    return 0 if final == "ok" else 1


# ---- Outlook (added 2026-11-03, the operator approved 4:50 PM CT) --------------------------------------
# Acts only when :9227 already has an Outlook / Microsoft-login tab (nothing opens one: the Mini JS
# writers dead-sink :9227 while SAFARI_OPS_PRIMARY is set, and the live OWA bridge is Mini :18822,
# which this box hook can never reach). Never sends mail, never touches read/unread state.
OUTLOOK_HOSTS = ("outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft", "outlook.live.com")
MS_LOGIN_BITS = ("login.microsoftonline.com", "login.microsoft.com", "login.live.com", "/adfs/",
                 "idp.corp.example.com", "idp2.corp.example.com", "sso.corp.example.com")
MAIL_URL = "https://outlook.office.com/mail/"
OWA_PROBE_JS = r"""JSON.stringify({href:location.href,
rows:document.querySelectorAll('[data-convid]').length+document.querySelectorAll('[role=listbox] [role=option]').length,
err:/HTTP ERROR \d{3}|This page isn.t working|can.t be reached|ERR_[A-Z_]+/.test((document.body&&document.body.innerText)||'').toString()})"""


def owa_classify(probe: dict | None) -> str:
    """Same rules as Mini tools/outlook_session_watchdog.classify."""
    if probe is None:
        return "not_served"
    href = str(probe.get("href") or "").lower()
    if href.startswith("chrome-error://") or probe.get("err") in (True, "true"):
        return "sso_error"
    if any(b in href for b in MS_LOGIN_BITS):
        return "signed_out"
    if not any(h in href for h in OUTLOOK_HOSTS):
        return "not_served"
    return "ok" if int(probe.get("rows") or 0) > 0 else "rows0"


def owa_plan(state: str, n_mail_tabs: int) -> list[str]:
    acts: list[str] = []
    if n_mail_tabs > 1:
        acts.append("close_extra_mail_tabs")
    if state == "signed_out":
        acts += ["owa_vault_restore", "reload_mail"]
    elif state in ("rows0", "sso_error"):
        acts.append("reload_mail")
    return acts


def _mail_tabs(rows: list[dict]) -> list[dict]:
    pages = [r for r in rows if r.get("type") == "page"]
    mail = [r for r in pages if any(h in str(r.get("url", "")) for h in OUTLOOK_HOSTS)]
    mail += [r for r in pages if r not in mail and any(b in str(r.get("url", "")).lower() for b in MS_LOGIN_BITS[:3])]
    return mail


def owa_vault_restore(dry: bool) -> str:
    cmd = ["ssh", "-o", f"ProxyCommand=python3 {SOCKS} %h %p", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", MINI,
           "cd ~/hailports-stack && /opt/homebrew/bin/python3 tools/owa_session_vault.py restore "
           f"http://box.tailnet-host.example.invalid:{PORT}{' --dry' if dry else ''}; echo rc=$?"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout
    except Exception as exc:
        return f"owa_vault_error:{type(exc).__name__}"
    rc = [ln for ln in out.splitlines() if ln.startswith("rc=")]
    return ("dry:" if dry else "") + "owa_vault_" + (rc[-1] if rc else "rc=?")


def outlook_pass(*, dry: bool = False, simulate: str | None = None) -> tuple[str, list[str], list[str]]:
    rows = _list() or []
    mail = _mail_tabs(rows)
    if not mail and not simulate:
        return "not_served", [], []
    state, d = "not_served", {}
    if mail:
        t = Tab(mail[0])
        try:
            raw = t.ev(OWA_PROBE_JS)
            d = json.loads(raw) if raw else {"href": mail[0].get("url")}
        finally:
            t.close()
        state = owa_classify(d)
        if state == "rows0" and not simulate:          # fresh launch: let the list hydrate
            for _ in range(10):
                time.sleep(3)
                t = Tab(mail[0])
                try:
                    raw = t.ev(OWA_PROBE_JS)
                finally:
                    t.close()
                d = json.loads(raw) if raw else d
                state = owa_classify(d)
                if state != "rows0":
                    break
    if simulate:
        state = simulate
    acts = owa_plan(state, len(mail))
    notes: list[str] = []
    for act in acts:
        if act == "close_extra_mail_tabs":
            for extra in mail[1:]:
                if dry:
                    notes.append(f"dry:close {extra['id'][:8]}")
                    continue
                try:
                    urllib.request.urlopen(BASE + "/json/close/" + extra["id"], timeout=5).read()
                    notes.append(f"closed {extra['id'][:8]}")
                except Exception as exc:
                    notes.append(f"close_fail {type(exc).__name__}")
        elif act == "owa_vault_restore":
            notes.append(owa_vault_restore(dry))
        elif act == "reload_mail":
            if dry or not mail:
                notes.append("dry:reload_mail")
            else:
                t = Tab(mail[0])
                try:
                    t.call("Page.navigate", {"url": MAIL_URL})
                finally:
                    t.close()
                notes.append("reload_mail")
    return state, acts, notes


def self_test() -> int:
    assert classify({"href": "https://app.zoom.us/wc/", "signin_btns": True, "workplace": False}) == "signed_out"
    assert classify({"href": "https://zoom.us/signin#/login"}) == "signed_out"
    assert classify({"href": "chrome-error://chromewebdata/"}) == "sso_error"
    assert classify({"href": TEAM_CHAT, "workplace": True, "rows": 0}) == "rows0"
    assert classify({"href": TEAM_CHAT, "workplace": True, "rows": 26}) == "ok"
    assert classify(None) == "page_missing"
    assert plan("ok", 1) == []                                   # healthy -> no-op
    assert plan("ok", 2) == ["close_extra_tabs"]                 # restore + --new-window duplicate
    assert plan("signed_out", 2) == ["close_extra_tabs", "vault_restore"]
    assert plan("signed_out", 1) == ["vault_restore"]
    assert plan("rows0", 1) == ["expand_dm"]
    assert plan("page_missing", 0) == ["navigate_team_chat"]
    assert owa_classify(None) == "not_served"
    assert owa_classify({"href": "https://login.microsoftonline.com/common/oauth2/authorize"}) == "signed_out"
    assert owa_classify({"href": "https://idp.corp.example.com/adfs/ls/"}) == "signed_out"
    assert owa_classify({"href": "chrome-error://chromewebdata/"}) == "sso_error"
    assert owa_classify({"href": "https://outlook.office.com/mail/", "rows": 0}) == "rows0"
    assert owa_classify({"href": "https://outlook.office.com/mail/", "rows": 9}) == "ok"
    assert owa_plan("ok", 1) == [] and owa_plan("not_served", 0) == []
    assert owa_plan("ok", 2) == ["close_extra_mail_tabs"]
    assert owa_plan("signed_out", 1) == ["owa_vault_restore", "reload_mail"]
    assert owa_plan("rows0", 1) == ["reload_mail"]
    print("self-test ok")
    return 0


def main(argv: list[str]) -> int:
    global QUIET
    if "self-test" in argv:
        return self_test()
    dry = "--dry" in argv
    sim = argv[argv.index("--simulate") + 1] if "--simulate" in argv else None
    global OUTLOOK_SIMULATE
    OUTLOOK_SIMULATE = argv[argv.index("--simulate-outlook") + 1] if "--simulate-outlook" in argv else None
    QUIET = "run" in argv and not dry       # launcher path: log only when it acts
    with open(LOCK, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("busy: another post-launch run holds the lock")
            return 0
        return run(dry=dry, simulate=sim)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
