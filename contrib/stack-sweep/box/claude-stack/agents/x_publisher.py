#!/usr/bin/env python3
"""X feed publisher for SOCIALS_ON_BOX (Linux Grok box).

Adapted from agents/linkedin_publisher.py (raw CDP tab) and the handle gate in
agents/darkgary_poster.py (_read_darkgary_handle). There is no hailports_x_deck.py
on this box. Playwright is not used. Nothing is posted unless ALL hold:

  * kill switch data/hustle/X_PUBLISH_OFF absent
  * lane listed in X_PUBLISH_LIVE
  * CDP reachable and x.com/home is logged in (login wall / challenge => abort)
  * signed-in handle matches data/runtime/x_lanes.json lanes.<lane>.expect
    (case-insensitive). Wrong or unreadable handle => abort before any composer click.
  * a non-empty ready text deck whose sibling .lane file names this lane
  * hailports / faceless copy does not name the operator, employer, or clients

--check and --dry-run never open the composer and never click Post.

Exit codes match linkedin_publisher.py.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

STACK = Path(os.environ.get("CLAUDE_STACK", Path.home() / "claude-stack"))
sys.path.insert(0, str(STACK))

from agents.linkedin_publisher import (  # noqa: E402
    EXIT_CDP,
    EXIT_DUP,
    EXIT_GATED,
    EXIT_IDENT,
    EXIT_LOGIN,
    EXIT_OK,
    EXIT_UI,
    EXIT_UNVERIFIED,
    Abort,
    Tab,
    utc,
)

HUSTLE = STACK / "data" / "hustle"
READY = HUSTLE / "x_decks" / "ready"
LEDGER = HUSTLE / "x_published.jsonl"
KILL = HUSTLE / "X_PUBLISH_OFF"
LANES_FILE = STACK / "data" / "runtime" / "x_lanes.json"
LOCK = STACK / "data" / "runtime" / "x_publisher.lock"
HOME = "https://x.com/home"

# Faceless / hailports must never name the day-job identity. realname X (Ima)
# is a different lane and is not run through this list.
_FACELESS_BLOCK = (
    "operator",
    "operator",
    "operator",
    "operator",
    "operator",
    "operator",
)


def log(msg: str) -> None:
    print(f"[x_publisher {utc()}] {msg}", file=sys.stderr, flush=True)


def load_lanes() -> dict:
    if not LANES_FILE.is_file():
        raise Abort(EXIT_GATED, "missing_x_lanes", path=str(LANES_FILE))
    try:
        return json.loads(LANES_FILE.read_text())
    except Exception as exc:  # noqa: BLE001
        raise Abort(EXIT_GATED, f"bad_lanes_file:{exc}")


def lane_cfg(lane: str) -> dict:
    data = load_lanes()
    cfg = dict((data.get("lanes") or {}).get(lane) or {})
    if not cfg:
        raise Abort(EXIT_GATED, "unknown_lane", lane=lane)
    env_key = {
        "x_ima": "X_CDP_URL",
        "x_hailports": "X_HAILPORTS_CDP_URL",
    }.get(lane, "X_CDP_URL")
    cfg.setdefault("cdp_url", os.environ.get(env_key) or cfg.get("cdp_url"))
    if not cfg.get("cdp_url"):
        raise Abort(EXIT_GATED, "cdp_unconfigured", lane=lane)
    exp = os.environ.get(f"X_EXPECT_{lane.upper()}")
    if exp:
        cfg["expect"] = exp
    return cfg


def live_lanes() -> set[str]:
    return {x.strip() for x in (os.environ.get("X_PUBLISH_LIVE") or "").split(",") if x.strip()}


def ledger_rows() -> list[dict]:
    if not LEDGER.is_file():
        return []
    rows = []
    for line in LEDGER.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return rows


def ledger_append(row: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(row) + "\n")


IDENT_JS = r"""
(() => {
  const hrefOf = (el) => (el && el.getAttribute('href')) || '';
  const profile = document.querySelector('[data-testid="AppTabBar_Profile_Link"]');
  const sw = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
  const u = location.href;
  const login = /\/i\/flow\/login|\/login/.test(u)
    || !!document.querySelector('input[autocomplete="username"], a[href="/i/flow/login"], [data-testid="loginButton"]');
  const challenge = /\/account\/access|\/i\/flow\/consent/.test(u);
  return {
    url: u,
    profile_href: hrefOf(profile),
    switcher: sw ? (sw.innerText || '').slice(0, 200) : '',
    login: login,
    challenge: challenge,
  };
})()
"""


def _norm_handle(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    # profile href is /Ima_Furad ; switcher text is "@Ima_Furad\nName"
    for part in text.replace("\\", "/").split():
        part = part.strip().lstrip("@")
        if part.startswith("/"):
            part = part.split("?")[0].strip("/").split("/")[0]
        if part and " " not in part and "\n" not in part:
            return part.split("/")[0]
    return text.lstrip("@").split("/")[-1].split()[0]


def read_identity(tab: Tab) -> dict:
    tab.goto(HOME, settle=30)
    # SPA: wait until the profile link or a login wall renders.
    tab.wait_js(
        """(() => {
          return !!(document.querySelector('[data-testid="AppTabBar_Profile_Link"]')
            || document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]')
            || document.querySelector('a[href="/i/flow/login"], [data-testid="loginButton"]')
            || /\\/i\\/flow\\/login/.test(location.href));
        })()""",
        timeout=25,
    )
    ident = tab.js(IDENT_JS) or {}
    if ident.get("login") or ident.get("challenge"):
        raise Abort(EXIT_LOGIN, "x_login_wall", url=ident.get("url"))
    handle = _norm_handle(ident.get("profile_href") or "") or _norm_handle(ident.get("switcher") or "")
    ident["handle"] = handle
    if not handle:
        raise Abort(EXIT_IDENT, "identity_unreadable", ident={k: ident.get(k) for k in ("url", "profile_href", "switcher")})
    return ident


def check_identity(ident: dict, lane: str, cfg: dict, require: bool) -> dict:
    expect = (cfg.get("expect") or "").strip().lower().lstrip("@")
    got = (ident.get("handle") or "").lower()
    if require and not expect:
        raise Abort(EXIT_IDENT, "lane_identity_unconfigured", lane=lane, ident=ident,
                    hint=f"set {LANES_FILE} lanes.{lane}.expect")
    if expect and got != expect:
        raise Abort(EXIT_IDENT, "identity_mismatch", lane=lane, handle=got, expect=expect)
    # Never let a faceless lane accept the personal LinkedIn identity, and never
    # accept a handle that is only "close".
    if "operator-demo" in got or got in {"operator", "operator-demo"}:
        raise Abort(EXIT_IDENT, "refusing_personal_linkedin_identity", lane=lane, handle=got)
    return ident


def faceless_leak(lane: str, text: str) -> str | None:
    if "hailports" not in lane and "faceless" not in lane:
        return None
    low = (text or "").lower()
    for bad in _FACELESS_BLOCK:
        if bad in low:
            return bad
    return None


def cmd_check(args) -> dict:
    cfg = lane_cfg(args.lane)
    with Tab(cfg["cdp_url"]) as tab:
        ident = read_identity(tab)
        check_identity(ident, args.lane, cfg, require=True)
    return {
        "ok": True,
        "lane": args.lane,
        "cdp": cfg["cdp_url"],
        "handle": ident.get("handle"),
        "expect": (cfg.get("expect") or "").lower().lstrip("@"),
        "profile_href": ident.get("profile_href"),
        "login_wall": False,
        "composer": "unopened",
        "posted": False,
    }


def resolve_deck(path: str | None) -> tuple[Path, str]:
    if not path:
        raise Abort(EXIT_GATED, "missing_deck")
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = READY / path
    p = p.resolve()
    lane_file = p.with_suffix(".lane") if p.suffix else Path(str(p) + ".lane")
    if p.suffix == ".txt":
        lane_file = p.with_suffix(".lane")
    return p, lane_file.read_text().strip() if lane_file.is_file() else ""


UI_STATE_JS = r"""
(() => {
  const btn = document.querySelector('[data-testid="tweetButtonInline"], [data-testid="tweetButton"]');
  const box = document.querySelector('[data-testid="tweetTextarea_0"]');
  return {
    url: location.href,
    new_tweet_href: (document.querySelector('[data-testid="SideNav_NewTweet_Button"]') || {}).getAttribute?.('href') || null,
    textarea: !!box,
    textarea_chars: box ? (box.innerText || '').trim().length : null,
    post_btn: btn ? {tid: btn.getAttribute('data-testid'), disabled: !!btn.disabled, aria_disabled: btn.getAttribute('aria-disabled')} : null,
  };
})()
"""


def ui_state(tab: Tab):
    # Diagnostic only: attached to aborts so the supervisor marker shows which step died.
    try:
        return tab.js(UI_STATE_JS)
    except Exception:  # noqa: BLE001
        return None


_BOX_SEL = """(() => { const a = document.querySelectorAll('[role="dialog"] [data-testid="tweetTextarea_0"]');
  const all = a.length ? a : document.querySelectorAll('[data-testid="tweetTextarea_0"]');
  return all.length ? all[all.length-1] : null; })()"""


def focus_editor(tab: Tab) -> None:
    """Bring the tab to front, move mouse with human jitter, and real-click the editor."""
    import random as _rnd
    try:
        tab.cmd("Page.bringToFront")
    except Exception:  # noqa: BLE001
        pass
    r = tab.js("(() => { const b = " + _BOX_SEL + "; if (!b) return null; b.focus(); const r = b.getBoundingClientRect(); return {x: r.x + r.width/2, y: r.y + Math.min(r.height/2, 20)}; })()")
    if r:
        jx = r["x"] + _rnd.uniform(-12, 12)
        jy = r["y"] + _rnd.uniform(-4, 4)
        tab.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=jx - _rnd.uniform(25, 65), y=jy - _rnd.uniform(10, 30))
        time.sleep(_rnd.uniform(0.06, 0.16))
        tab.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=jx, y=jy)
        time.sleep(_rnd.uniform(0.04, 0.10))
        for t in ("mousePressed", "mouseReleased"):
            tab.cmd("Input.dispatchMouseEvent", type=t, x=jx, y=jy, button="left", clickCount=1)
    time.sleep(_rnd.uniform(0.42, 0.98))


def editor_text(tab: Tab) -> str:
    return tab.js("(() => { const b = " + _BOX_SEL + "; return b ? (b.innerText || '').trim() : ''; })()") or ""


_ZW_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]")


def type_text(tab: Tab, text: str) -> None:
    """Human-paced input: strips zero-width chars, uses 18-55ms per-char keystrokes with word/clause micro-pauses for <=350 chars,
    and chunked line-by-line typing with 65-220ms inter-line jitter + 350-700ms paragraph pauses for long-form (>350 chars)."""
    import random as _rnd
    text = _ZW_RE.sub("", text or "")
    if len(text) > 350:
        lines = text.split("\n")
        for idx, ln in enumerate(lines):
            if ln:
                # Type in 45-90 char human phrase chunks with 60-180ms jitter
                pos = 0
                while pos < len(ln):
                    step = _rnd.randint(45, 90)
                    chunk = ln[pos:pos + step]
                    tab.cmd("Input.insertText", text=chunk)
                    pos += step
                    time.sleep(_rnd.uniform(0.055, 0.165))
            if idx < len(lines) - 1:
                tab.cmd("Input.dispatchKeyEvent", type="rawKeyDown", key="Enter",
                        code="Enter", windowsVirtualKeyCode=13, text="\r")
                tab.cmd("Input.dispatchKeyEvent", type="keyUp", key="Enter",
                        code="Enter", windowsVirtualKeyCode=13)
                time.sleep(_rnd.uniform(0.08, 0.22) if ln else _rnd.uniform(0.28, 0.58))
        # Real keystroke + backspace so DraftJS/Lexical commits the buffer, then human review pause
        tab.cmd("Input.dispatchKeyEvent", type="keyDown", key=" ", text=" ", unmodifiedText=" ")
        tab.cmd("Input.dispatchKeyEvent", type="keyUp", key=" ", text=" ", unmodifiedText=" ")
        time.sleep(_rnd.uniform(0.05, 0.12))
        tab.cmd("Input.dispatchKeyEvent", type="rawKeyDown", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
        tab.cmd("Input.dispatchKeyEvent", type="keyUp", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
        time.sleep(_rnd.uniform(0.45, 0.90))
        return
    for ch in text:
        if ch == "\n":
            tab.cmd("Input.dispatchKeyEvent", type="rawKeyDown", key="Enter",
                    code="Enter", windowsVirtualKeyCode=13, text="\r")
            tab.cmd("Input.dispatchKeyEvent", type="keyUp", key="Enter",
                    code="Enter", windowsVirtualKeyCode=13)
            time.sleep(_rnd.uniform(0.09, 0.24))
            continue
        tab.cmd("Input.dispatchKeyEvent", type="keyDown", key=ch, text=ch,
                unmodifiedText=ch)
        tab.cmd("Input.dispatchKeyEvent", type="keyUp", key=ch, text=ch,
                unmodifiedText=ch)
        if ch in ".!?,;:":
            time.sleep(_rnd.uniform(0.065, 0.160))
        elif ch == " ":
            time.sleep(_rnd.uniform(0.025, 0.065))
        else:
            time.sleep(_rnd.uniform(0.016, 0.048))


def _norm_cmp(t: str) -> str:
    return "\n".join(line.strip() for line in (t or "").replace("\r", "").replace("\u00a0", " ").strip().split("\n") if line.strip())


def clear_editor(tab: Tab) -> None:
    focus_editor(tab)
    for t in ("rawKeyDown", "keyUp"):
        tab.cmd("Input.dispatchKeyEvent", type=t, key="a", code="KeyA", windowsVirtualKeyCode=65, modifiers=2)
    for t in ("rawKeyDown", "keyUp"):
        tab.cmd("Input.dispatchKeyEvent", type=t, key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
    time.sleep(0.4)


def type_verified(tab: Tab, text: str, tries: int = 2) -> bool:
    """2026-10-17: focus, CONFIRM focus is in the composer, type, read back; True only on an exact match.
    Typing before focus settled dropped the first keystrokes into another box (truncated posts)."""
    for _ in range(tries):
        time.sleep(1.2)  # let the composer finish opening/animating before any keystroke
        focus_editor(tab)
        focused = tab.js("(() => { const b = " + _BOX_SEL + "; return !!b && (b === document.activeElement || b.contains(document.activeElement)); })()")
        if not focused:
            time.sleep(1.0)
            focus_editor(tab)
            focused = tab.js("(() => { const b = " + _BOX_SEL + "; return !!b && (b === document.activeElement || b.contains(document.activeElement)); })()")
            if not focused:
                continue
        if editor_text(tab):
            clear_editor(tab)
        type_text(tab, text)
        time.sleep(0.8)
        if _norm_cmp(editor_text(tab)) == _norm_cmp(text):
            return True
        clear_editor(tab)
    return False


# ── 2026-10-18: ONE shared send path. Every X send (post, thread part, reply, inbox reply-back, quote)
# goes through send_verified(): exact read-back before the click, trusted mouse click, then live_check()
# reads the live text off the account's with_replies tab; any mismatch is deleted at once and tipped.
import re as _re
from datetime import datetime, timezone
_TRUNC_LOG = Path("/home/operator/socials/deleted_truncated_20261018.jsonl")


def type_verified_keep(tab: Tab, text: str) -> bool:
    """Read the composer back without retyping (used after adding a poll)."""
    return _norm_cmp(editor_text(tab)) == _norm_cmp(text) or type_verified(tab, text)


def _live_norm(t: str) -> str:
    t = _re.sub(r"https?://\S+|\S+\.(?:com|ai|io|dev|org|net)/\S*…?", "", t or "")
    return _re.sub(r"\s+", " ", t.replace("\u00a0", " ").replace("…", "")).strip().lower()


def _loose(t: str) -> str:
    """2026-11-03: link/whitespace-insensitive form. X shows links as t.co / truncated display split
    across lines ('https://', 'github.com/hailports/Stac', 'kMini', '…'); drop every url-ish token,
    stray scheme, ellipsis and all non-alphanumerics so only the prose is compared."""
    t = (t or "").replace("\u00a0", " ")
    t = _re.sub(r"(\S+\.[a-z]{2,6}/\S*)[ \t]*\n[ \t]*\S+[ \t]*\n[ \t]*…", r"\1", t, flags=_re.I)  # split display link tail
    t = t.replace("…", " ")
    t = _re.sub(r"https?://\S*|\bt\.co/\S*|\S+\.(?:com|ai|io|dev|org|net|co|app|gg|me|so|xyz)(?:/\S*)?", " ", t, flags=_re.I)
    return _re.sub(r"[^a-z0-9]+", "", t.lower())


def _loose_match(live: str, want: str, thr: float = 0.9) -> float:
    import difflib
    a, b = _loose(live), _loose(want)
    if not a or not b:
        return 0.0
    if a == b or (b in a and len(a) - len(b) <= 40) or (a in b and len(b) - len(a) <= 40):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


_POST_BTN_JS = """(() => {
  const b = document.querySelector('[role="dialog"] [data-testid="tweetButton"], [role="dialog"] [data-testid="tweetButtonInline"]')
         || document.querySelector('[data-testid="tweetButtonInline"], [data-testid="tweetButton"]');
  if (!b) return {found: false};
  b.scrollIntoView({block: 'center', inline: 'center'});
  const r = b.getBoundingClientRect();
  const en = !(b.disabled || b.getAttribute('aria-disabled') === 'true');
  const vis = r.width > 0 && r.height > 0 && r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth;
  return {found: true, enabled: en, visible: vis, x: r.x + r.width / 2, y: r.y + r.height / 2};
})()"""


def _composer_gone_or_empty(tab: Tab) -> bool:
    st = tab.js("""(() => { const b = document.querySelector('[data-testid="tweetTextarea_0"]');
      return !b || (b.innerText || '').trim().length < 2; })()""")
    return bool(st)


def _kbd_submit(tab: Tab) -> None:
    """Fallback: Ctrl+Enter then Meta+Enter in the focused composer."""
    for mod in (2, 4):  # CDP modifiers: 2=Ctrl, 4=Meta(Cmd)
        for t in ("rawKeyDown", "keyUp"):
            tab.cmd("Input.dispatchKeyEvent", type=t, key="Enter", code="Enter",
                    windowsVirtualKeyCode=13, modifiers=mod)
        time.sleep(2.5)
        if _composer_gone_or_empty(tab):
            return


def real_click_post(tab: Tab, wait_s: float = 10.0) -> bool:
    """2026-11-04 replybtn fix: >280-char notice pushes Post to the screen edge. Scroll into view,
    wait until visible+enabled, trusted click, confirm; fallback Ctrl/Cmd+Enter."""
    import random as _rnd
    r = None
    deadline = time.time() + wait_s
    while time.time() < deadline:
        r = tab.js(_POST_BTN_JS) or {}
        if r.get("found") and r.get("enabled") and r.get("visible"):
            break
        time.sleep(0.5)
    if not (r and r.get("found") and r.get("enabled")):
        return False
    if r.get("visible"):
        jx = r["x"] + _rnd.uniform(-6, 6)
        jy = r["y"] + _rnd.uniform(-3, 3)
        tab.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=jx - _rnd.uniform(20, 50), y=jy - _rnd.uniform(10, 25))
        time.sleep(_rnd.uniform(0.08, 0.20))
        tab.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=jx, y=jy)
        time.sleep(_rnd.uniform(0.05, 0.14))
        for t in ("mousePressed", "mouseReleased"):
            tab.cmd("Input.dispatchMouseEvent", type=t, x=jx, y=jy, button="left", clickCount=1)
        tab.post_clicked = True
        time.sleep(2.5)
        if _composer_gone_or_empty(tab):
            return True
        log("post_click_missed -> kbd fallback")
    _kbd_submit(tab)
    tab.post_clicked = True
    return True


def send_verified(tab: Tab, text: str) -> bool:
    """Composer must already be open. Exact read-back, human review pause (0.9-2.2s), then trusted click."""
    import random as _rnd
    if not type_verified(tab, text):
        return False
    time.sleep(_rnd.uniform(0.9, 2.2))  # human reading/review pause before clicking Post
    if _norm_cmp(editor_text(tab)) != _norm_cmp(_ZW_RE.sub("", text or "")):  # re-check right before the click
        return False
    return real_click_post(tab)


_OWN_JS = r"""(() => [...document.querySelectorAll('article[data-testid="tweet"]')].map(a => {
  const tm = a.querySelector('time'); const l = tm && tm.closest('a'); const h = l ? l.getAttribute('href') : '';
  const m = h.match(/^\/([^/]+)\/status\/(\d+)/); const t = a.querySelector('[data-testid="tweetText"]');
  return m ? {user: m[1], id: m[2], time: tm.getAttribute('datetime'), text: t ? t.innerText : ''} : null}).filter(Boolean))()"""


def _delete_own(tab: Tab, handle: str, sid: str) -> bool:
    tab.goto(f"https://x.com/{handle}/status/{sid}", settle=7)
    def rc(js):
        r = tab.js(js)
        if not r:
            return False
        for t in ("mousePressed", "mouseReleased"):
            tab.cmd("Input.dispatchMouseEvent", type=t, x=r["x"], y=r["y"], button="left", clickCount=1)
        time.sleep(1.5)
        return True
    ok = rc(f"""(()=>{{const a=[...document.querySelectorAll('article[data-testid="tweet"]')].find(x=>{{const tm=x.querySelector('time');const l=tm&&tm.closest('a');return l&&l.getAttribute('href').toLowerCase()==='/{handle.lower()}/status/{sid}';}});
      if(!a) return null; const b=a.querySelector('[data-testid="caret"]'); if(!b) return null; b.scrollIntoView({{block:'center'}}); const r=b.getBoundingClientRect(); return {{x:r.x+r.width/2,y:r.y+r.height/2}};}})()""")
    ok = ok and rc("""(()=>{const m=[...document.querySelectorAll('[role="menuitem"]')].find(x=>/^Delete$/.test(x.innerText.trim()));if(!m)return null;const r=m.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()""")
    ok = ok and rc("""(()=>{const b=document.querySelector('[data-testid="confirmationSheetConfirm"]');if(!b)return null;const r=b.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()""")
    return bool(ok)


def live_check(tab: Tab, handle: str, text: str, lane: str, kind: str, window_s: int = 240) -> dict:
    """Post-send: the newest own item must equal the draft. Mismatch -> delete now + tip. Fail closed."""
    handle = handle.lstrip("@")
    want = _live_norm(text)
    time.sleep(2)
    rows = []
    seen = {}
    for i in range(4):  # feeds lag a few seconds behind the send; alternate tabs
        tab.goto(f"https://x.com/{handle}" + ("/with_replies" if i % 2 == 0 else ""), settle=9)
        for r in (tab.js(_OWN_JS) or []):
            if r["user"].lower() == handle.lower():
                seen[r["id"]] = r
        rows = list(seen.values())
        for r in rows:
            ln = _live_norm(r["text"])
            if ln == want or (len(want) > 200 and len(ln) >= 120 and want.startswith(ln[:110])):
                return {"live_ok": True, "id": r["id"]}
            if _loose_match(r["text"], text) >= 0.9:  # link/whitespace-only difference = live
                return {"live_ok": True, "id": r["id"], "loose": True}
        time.sleep(6)
    cut = datetime.now(timezone.utc).timestamp() - window_s
    fresh = [r for r in rows if r.get("time") and datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp() >= cut]
    bad = [r for r in fresh if _live_norm(r["text"]) and (_live_norm(r["text"]) in want or want[:30] in _live_norm(r["text"]) or want[-30:] in _live_norm(r["text"]))]
    bad = [r for r in bad if _loose_match(r["text"], text) < 0.8]  # never delete for a link/whitespace mismatch
    if os.environ.get("X_NO_AUTODELETE"):  # manual launches: report, never delete
        bad = []
    out = {"live_ok": False, "bad": [r["id"] for r in bad]}
    for r in bad:
        d = _delete_own(tab, handle, r["id"])
        with _TRUNC_LOG.open("a") as f:
            f.write(json.dumps({"lane": lane, "id": r["id"], "posted": r["time"], "text": r["text"], "draft": text,
                                "kind": kind, "deleted": d, "via": "live_check", "at": utc()}) + "\n")
    try:
        from core.reply_guard import tip
        tip(lane, "live_mismatch" if out["bad"] else "live_not_seen", {"id": ",".join(out["bad"]) or "none", "kind": kind, "draft": text, "live": [r["text"] for r in bad]})
    except Exception:
        pass
    return out


def _fill_poll(tab: Tab, options: list) -> None:
    """2026-10-20 rich formats: open the poll editor, type each choice with real key events, read every value back."""
    r = tab.js("""(() => { const b = document.querySelector('[role="dialog"] [data-testid="createPollButton"]');
      if (!b) return null; const r = b.getBoundingClientRect(); return {x: r.x + r.width/2, y: r.y + r.height/2}; })()""")
    if not r:
        raise Abort(EXIT_UI, "poll_button_missing")
    for t in ("mousePressed", "mouseReleased"):
        tab.cmd("Input.dispatchMouseEvent", type=t, x=r["x"], y=r["y"], button="left", clickCount=1)
    if not tab.wait_js("""!!document.querySelector('[role="dialog"] input[name="Choice1"]')""", timeout=10):
        raise Abort(EXIT_UI, "poll_editor_missing")
    for i, opt in enumerate(options[:4], 1):
        if i > 2:
            tab.js("""(() => { const b = document.querySelector('[role="dialog"] [data-testid="addPollChoice"]'); if (b) b.click(); return !!b; })()""")
            time.sleep(0.8)
        pos = tab.js(f"""(() => {{ const e = document.querySelector('[role="dialog"] input[name="Choice{i}"]'); if (!e) return null;
          e.scrollIntoView({{block: 'center'}}); e.focus(); const r = e.getBoundingClientRect(); return {{x: r.x + 20, y: r.y + r.height/2}}; }})()""")
        if not pos:
            raise Abort(EXIT_UI, f"poll_choice_{i}_missing")
        for t in ("mousePressed", "mouseReleased"):
            tab.cmd("Input.dispatchMouseEvent", type=t, x=pos["x"], y=pos["y"], button="left", clickCount=1)
        time.sleep(0.3)
        type_text(tab, str(opt))
        time.sleep(0.4)
        got = tab.js(f"""(() => {{ const e = document.querySelector('[role="dialog"] input[name="Choice{i}"]'); return e ? e.value : null; }})()""")
        if (got or "").strip() != str(opt).strip():
            raise Abort(EXIT_UI, "poll_choice_mismatch", choice=i, got=got, want=opt)


def media_live_check(tab: Tab, handle: str, sid: str, want: str) -> dict:
    """After a verified send: the live status must carry the media/poll (want = image|video|poll)."""
    tab.goto(f"https://x.com/{handle.lstrip('@')}/status/{sid}", settle=9)
    js = f"""(() => {{ const a = [...document.querySelectorAll('article[data-testid="tweet"]')].find(x => [...x.querySelectorAll('a[href*="/status/{sid}"]')].length) || document.querySelector('article[data-testid="tweet"]');
      if (!a) return null; return {{photos: a.querySelectorAll('[data-testid="tweetPhoto"] img').length,
        video: a.querySelectorAll('video, [data-testid="videoPlayer"], [data-testid="videoComponent"]').length,
        poll: a.querySelectorAll('[data-testid="cardPoll"], [role="radio"], [data-testid*="oll"]').length + (/votes|Final results|left$/m.test(a.innerText) ? 1 : 0)}}; }})()"""
    for _ in range(3):
        r = tab.js(js) or {}
        ok = bool(r) and ((want == "image" and r.get("photos", 0) > 0) or (want == "video" and r.get("video", 0) > 0)
                          or (want == "poll" and r.get("poll", 0) > 0))
        if ok:
            return {"media_ok": True, "want": want, **r}
        time.sleep(5)
        tab.goto(f"https://x.com/{handle.lstrip('@')}/status/{sid}", settle=8)
    return {"media_ok": False, "want": want, **(r or {})}


def do_publish(tab: Tab, text: str, images: list | None = None, poll: list | None = None, community_id: str | None = None) -> dict:
    # 2026-10-25 operator: if publishing to an X Community, navigate to the Community page first so the composer
    # defaults to that Community (distributing to BOTH Community Home Timelines AND our followers!).
    comm_selected = None
    if community_id and not _re.search(r"https?://x\.com/\S+/status/\d+", text or ""):
        tab.goto(f"https://x.com/i/communities/{community_id}", settle=6)
        tab.wait_js("""!!document.querySelector('[data-testid="SideNav_NewTweet_Button"]')""", timeout=12)
    # Composer opens only after the caller has already matched the handle.
    opened = tab.js(r"""
(() => {
  const b = document.querySelector('[data-testid="SideNav_NewTweet_Button"]');
  if (!b) return false;
  b.click();
  return true;
})()
""")
    if not opened:
        raise Abort(EXIT_UI, "new_tweet_button_missing", state=ui_state(tab))
    box = tab.wait_js(
        """!!document.querySelector('[role="dialog"] [data-testid="tweetTextarea_0"]')
           || (location.pathname === '/compose/post' && !!document.querySelector('[data-testid="tweetTextarea_0"]'))""",
        timeout=15,
    )
    if not box:
        raise Abort(EXIT_UI, "composer_missing", state=ui_state(tab))
    if community_id:
        comm_selected = tab.js("""(() => {
            const b = document.querySelector('[role="dialog"] button[aria-label="Choose audience"]');
            const chk = document.querySelector('[role="dialog"] input[type="checkbox"], [role="dialog"] [role="checkbox"]');
            if (chk && chk.getAttribute('aria-checked') === 'false') chk.click();
            return b ? (b.innerText || '').trim() : null;
        })()""")
    if not type_verified(tab, text):
        raise Abort(EXIT_UI, "composer_text_mismatch", state=ui_state(tab))
    if _re.search(r"https?://x\.com/\S+/status/\d+", text or ""):
        # Wait briefly for X to hydrate the native embedded Quote Post card inside the composer
        time.sleep(2.5)
    else:
        time.sleep(1)
    if poll and not images:
        _fill_poll(tab, poll)
        if not type_verified_keep(tab, text):
            raise Abort(EXIT_UI, "composer_text_mismatch_after_poll", state=ui_state(tab))
    if images:
        root = tab.cmd("DOM.getDocument", depth=-1, pierce=True)["root"]["nodeId"]
        ids = tab.cmd("DOM.querySelectorAll", nodeId=root,
                      selector='[role="dialog"] input[type="file"][data-testid="fileInput"], [role="dialog"] input[type="file"]').get("nodeIds") or []
        if not ids:
            raise Abort(EXIT_UI, "x_file_input_missing")
        tab.cmd("DOM.setFileInputFiles", nodeId=ids[0], files=[str(p) for p in images[:4]])
        ok = tab.wait_js(f"""(() => {{
          const d = document.querySelector('[role="dialog"]') || document;
          const n = d.querySelectorAll('[data-testid="attachments"] img, [data-testid="attachments"] [role="link"], [data-testid="attachments"] video').length;
          const b = d.querySelector('[data-testid="tweetButton"]');
          return n >= {min(len(images), 4)} && b && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
        }})()""", timeout=240 if any(str(p).endswith(".mp4") for p in images) else 60)
        if not ok:
            raise Abort(EXIT_UI, "x_images_not_attached", state=ui_state(tab))
        time.sleep(2)
    # Re-read handle from the still-open home shell before clicking Post.
    ident = tab.js(IDENT_JS) or {}
    return {"composed": True, "community": comm_selected, "community_id": community_id,
            "handle_still": _norm_handle(ident.get("profile_href") or "") or _norm_handle(ident.get("switcher") or "")}


def click_post(tab: Tab) -> None:
    if real_click_post(tab):
        return
    clicked = tab.js(r"""
(() => {
  const b = document.querySelector('[data-testid="tweetButtonInline"], [data-testid="tweetButton"]');
  if (!b || b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
  b.click();
  return true;
})()
""")
    if not clicked:
        raise Abort(EXIT_UI, "post_button_missing", state=ui_state(tab))
    tab.post_clicked = True


def _clear_composer(tab: Tab) -> None:
    """Ctrl+A + Backspace via real key events, then Escape to close the composer."""
    for args in (
        {"type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2, "windowsVirtualKeyCode": 65},
        {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 2, "windowsVirtualKeyCode": 65},
        {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
        {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
    ):
        tab.cmd("Input.dispatchKeyEvent", **args)
    tab.cmd("Input.dispatchKeyEvent", type="keyDown", key="Escape",
            code="Escape", windowsVirtualKeyCode=27)
    tab.cmd("Input.dispatchKeyEvent", type="keyUp", key="Escape",
            code="Escape", windowsVirtualKeyCode=27)


def cmd_composer_test(args) -> dict:
    """Open compose, type 'composer test', verify, clear + close. Never posts."""
    cfg = lane_cfg(args.lane)
    with Tab(cfg["cdp_url"]) as tab:
        ident = read_identity(tab)
        check_identity(ident, args.lane, cfg, require=True)
        opened = tab.js(r"""
(() => {
  const b = document.querySelector('[data-testid="SideNav_NewTweet_Button"]');
  if (!b) return false;
  b.click();
  return true;
})()
""")
        if not opened:
            raise Abort(EXIT_UI, "new_tweet_button_missing", state=ui_state(tab))
        box = tab.wait_js("""!!document.querySelector('[role="dialog"] [data-testid="tweetTextarea_0"]')""", timeout=15)
        if not box:
            raise Abort(EXIT_UI, "composer_missing", state=ui_state(tab))
        focus_editor(tab)
        type_text(tab, "composer test")
        time.sleep(1)
        typed = editor_text(tab)
        out = {
            "ok": "composer test" in typed,
            "composer_test": True,
            "lane": args.lane,
            "cdp": cfg["cdp_url"],
            "handle": ident.get("handle"),
            "typed": typed[:80],
            "posted": False,
        }
        try:
            _clear_composer(tab)
        except Exception:  # noqa: BLE001
            pass
        print(json.dumps(out, indent=2))
        return out


def load_thread_parts(text: str) -> list[str]:
    """Ordered posts. JSON {parts: [...]} or a list, else split on a --- line."""
    raw = (text or "").strip()
    data = None
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except Exception:
            data = None
    if isinstance(data, dict):
        parts = data.get("parts") or []
    elif isinstance(data, list):
        parts = data
    else:
        parts = re.split(r"\n---\n", raw)
    # keep line breaks (like /tmp/pub.py); only trim trailing spaces per line
    out = ["\n".join(ln.rstrip() for ln in str(p).strip().splitlines()).strip() for p in parts]
    return [p for p in out if p]


def _progress_path(deck: Path) -> Path:
    return Path(str(deck) + ".progress.json")


def _read_progress(deck: Path) -> dict:
    path = _progress_path(deck)
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_progress(deck: Path, data: dict) -> None:
    _progress_path(deck).write_text(json.dumps(data))


def _latest_match(tab: Tab, handle: str, snippet: str) -> dict:
    h = handle.lstrip("@")
    tab.goto(f"https://x.com/{h}/with_replies", settle=10)
    clean_snip = _live_norm(snippet)[:40]
    snippet_js = json.dumps(clean_snip or snippet[:40].lower())
    found = tab.js(f"""(() => {{
      const want = {snippet_js};
      const arts = [...document.querySelectorAll('article[data-testid="tweet"]')];
      for (const a of arts) {{
        const txt = ((a.querySelector('[data-testid="tweetText"]') || {{}}).innerText || '').replace(/https?:\/\/\S+/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
        const link = a.querySelector('a[href*="/status/"]');
        if (!link) continue;
        const m = (link.href || '').match(/status\\/(\\d+)/);
        if (!m) continue;
        if (!want || txt.includes(want)) return {{id: m[1], url: link.href.split('?')[0], text: txt.slice(0, 200)}};
      }}
      return null;
    }})()""") or {}
    return found if isinstance(found, dict) else {}


def _reply_on(tab: Tab, status_url: str, status_id: str, text: str) -> dict:
    tab.goto(status_url, settle=10)
    opened = tab.js(f"""(() => {{
      const arts = [...document.querySelectorAll('article[data-testid="tweet"]')];
      const a = arts.find(x => [...x.querySelectorAll('a[href*="/status/{status_id}"]')].length) || arts[0];
      if (!a) return false;
      const b = a.querySelector('[data-testid="reply"]');
      if (!b) return false;
      b.click();
      return true;
    }})()""")
    if not opened:
        raise Abort(EXIT_UI, "thread_reply_button_missing", status=status_id)
    box = tab.wait_js(
        """!!document.querySelector('[role="dialog"] [data-testid^="tweetTextarea"], [data-testid^="tweetTextarea"]')""",
        timeout=12,
    )
    if not box:
        raise Abort(EXIT_UI, "thread_composer_missing", status=status_id)
    tab.wait_js("""!!document.querySelector('[role="dialog"] [data-testid="tweetTextarea_0"]')""", timeout=8)
    if not send_verified(tab, text):
        raise Abort(EXIT_UI, "thread_composer_text_mismatch", status=status_id)
    time.sleep(3)
    return {"replied": True, "parent": status_id}


def cmd_publish(args) -> dict:
    lane = args.lane
    deck, tagged = resolve_deck(args.publish)
    if tagged and tagged != lane:
        raise Abort(EXIT_GATED, "deck_lane_mismatch", lane=lane, tagged=tagged)
    if not deck.is_file():
        raise Abort(EXIT_GATED, "missing_deck", deck=str(deck))
    raw_text = deck.read_text().strip()
    if not raw_text:
        raise Abort(EXIT_GATED, "empty_deck", deck=str(deck))
    parts = load_thread_parts(raw_text) if args.thread else [raw_text]
    thread = bool(args.thread and len(parts) >= 2)
    if args.thread and not thread:
        log("thread fail_closed_single")
        parts = parts[:1] or [raw_text]
    progress = _read_progress(deck) if thread else {}
    text = parts[0]
    for part in parts:
        leak = faceless_leak(lane, part)
        if leak:
            raise Abort(EXIT_GATED, "faceless_identity_leak", token=leak)
    leak = None
    if leak:
        raise Abort(EXIT_GATED, "faceless_identity_leak", token=leak)
    ref = args.ref or deck.stem
    if KILL.exists():
        raise Abort(EXIT_GATED, "kill_switch", path=str(KILL))
    if lane not in live_lanes() and not args.dry_run:
        raise Abort(EXIT_GATED, "lane_not_live", lane=lane, live=sorted(live_lanes()))
    posted_prior = list(progress.get("posted") or [])
    incomplete = thread and posted_prior and len(posted_prior) < len(parts)
    prior = [r for r in ledger_rows() if r.get("ref") == ref and r.get("lane") == lane and r.get("verified") is True]
    if prior and not args.force and not incomplete:
        raise Abort(EXIT_DUP, "ref_already_attempted", ref=ref)
    cfg = lane_cfg(lane)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        with Tab(cfg["cdp_url"]) as tab:
            ident = read_identity(tab)
            check_identity(ident, lane, cfg, require=True)
            if args.dry_run:
                return {
                    "ok": True, "dry_run": True, "posted": False, "lane": lane, "ref": ref,
                    "handle": ident.get("handle"), "expect": cfg.get("expect"),
                    "chars": len(text), "composer": "unopened",
                    "thread": thread, "parts": len(parts),
                }
            base = {"ts": utc(), "ref": ref, "lane": lane, "deck": deck.name, "handle": ident.get("handle")}
            root_done = thread and any(p.get("i") == 0 for p in posted_prior)
            if not root_done:
                try:
                    _poll = json.loads(args.poll) if getattr(args, "poll", "") else None
                    _cid = getattr(args, "community", "") or ""
                    if _cid.lower() == "none":
                        _cid = ""
                    elif not _cid and not thread and not _re.search(r"https?://x\.com/\S+/status/\d+", text or ""):
                        try:
                            from core.x_communities import pick_community
                            _picked = pick_community(lane, text)
                            if _picked:
                                _cid = _picked["id"]
                        except Exception:
                            _cid = ""
                    composed = do_publish(tab, text, [Path(x) for x in (args.images or "").split(",") if x] or None, _poll, community_id=_cid or None)
                except Abort:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise Abort(EXIT_UI, "compose_failed", error=str(exc)[:200])
                if (composed.get("handle_still") or "").lower() != (cfg.get("expect") or "").strip().lower().lstrip("@"):
                    raise Abort(EXIT_IDENT, "identity_changed_before_post", handle=composed.get("handle_still"), expect=cfg.get("expect"))
                try:
                    click_post(tab)
                except Exception as exc:  # noqa: BLE001
                    if not getattr(tab, "post_clicked", False):
                        raise
                    ledger_append({**base, "event": "post_clicked", "verified": False, "error": str(exc)[:300]})
                    raise Abort(EXIT_UNVERIFIED, "post_unverified_after_error", ref=ref)
                time.sleep(3)
                still = tab.js("""(() => {
                  const box = document.querySelector('[data-testid="tweetTextarea_0"]');
                  if (!box) return {cleared: true};
                  return {cleared: ((box.innerText || '').trim().length < 2)};
                })()""") or {}
                verified = bool(still.get("cleared"))
                if verified:
                    lc = live_check(tab, ident.get("handle") or cfg.get("expect") or "", text, lane, "post")
                    verified = bool(lc.get("live_ok"))
                    base = {**base, "live_check": lc}
                _want = ("poll" if getattr(args, "poll", "") else ("video" if any(x.endswith(".mp4") for x in (args.images or "").split(",") if x)
                         else ("image" if args.images else "")))
                if verified and _want and (base.get("live_check") or {}).get("id"):
                    try:
                        base = {**base, "media_check": media_live_check(tab, ident.get("handle") or cfg.get("expect") or "", base["live_check"]["id"], _want)}
                    except Exception as _e:  # noqa: BLE001
                        base = {**base, "media_check": {"media_ok": False, "error": str(_e)[:120]}}
                if composed.get("community_id"):
                    base["community_id"] = composed.get("community_id")
                    base["community_name"] = composed.get("community")
                ledger_append({**base, "event": "posted" if verified else "post_clicked", "verified": verified, "part": 0})
                if not verified:
                    raise Abort(EXIT_UNVERIFIED, "post_unverified", ref=ref)
                _root_id = (base.get("live_check") or {}).get("id")
            if not thread:
                return {"ok": True, "verified": True, "posted": True, "ref": ref, "lane": lane, "handle": ident.get("handle"),
                        "id": (base.get("live_check") or {}).get("id"), "media_check": base.get("media_check")}
            posted = list(progress.get("posted") or [])
            _h_clean = (ident.get("handle") or cfg.get("expect") or "").lstrip("@")
            if not any(p.get("i") == 0 for p in posted):
                found = ({"id": _root_id, "url": f"https://x.com/{_h_clean}/status/{_root_id}"} if _root_id
                         else _latest_match(tab, _h_clean, text))
                if not found.get("id"):
                    _write_progress(deck, {"posted": posted, "error": "first_status_unreadable"})
                    raise Abort(EXIT_UNVERIFIED, "thread_first_unreadable", ref=ref)
                posted.append({"i": 0, "id": found["id"], "url": found["url"]})
                _write_progress(deck, {"posted": posted})
            for idx, part in enumerate(parts[1:], start=1):
                if any(p.get("i") == idx for p in posted):
                    continue
                parent = next(p for p in posted if p.get("i") == idx - 1)
                _reply_on(tab, parent["url"], parent["id"], part)
                lc = live_check(tab, _h_clean, part, lane, "thread_part")
                if not lc.get("live_ok"):
                    _write_progress(deck, {"posted": posted, "error": f"part_{idx}_live_mismatch"})
                    raise Abort(EXIT_UNVERIFIED, "thread_part_live_mismatch", ref=ref, part=idx, lc=lc)
                found = ({"id": lc["id"], "url": f"https://x.com/{_h_clean}/status/{lc['id']}"} if lc.get("id")
                         else _latest_match(tab, _h_clean, part))
                if not found.get("id"):
                    _write_progress(deck, {"posted": posted, "error": f"part_{idx}_unreadable"})
                    raise Abort(EXIT_UNVERIFIED, "thread_part_unreadable", ref=ref, part=idx)
                posted.append({"i": idx, "id": found["id"], "url": found["url"]})
                _write_progress(deck, {"posted": posted})
                ledger_append({**base, "live_check": lc, "id": found["id"], "url": found["url"],
                               "parent_id": parent["id"], "root_id": posted[0]["id"],
                               "event": "posted", "verified": True, "part": idx})
            prog = _progress_path(deck)
            if prog.is_file():
                prog.unlink()
            return {
                "ok": True, "verified": True, "posted": True, "ref": ref, "lane": lane,
                "handle": ident.get("handle"), "thread": True, "parts": len(parts),
            }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="x_publisher.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--publish", metavar="TXT", help="text deck under x_decks/ready or an absolute .txt")
    ap.add_argument("--lane", required=True, help="x_ima or x_hailports")
    ap.add_argument("--ref", help="ledger ref (default: deck stem)")
    ap.add_argument("--check", action="store_true", help="read-only identity check, no composer")
    ap.add_argument("--dry-run", action="store_true", help="gates only, composer stays closed")
    ap.add_argument("--thread", action="store_true",
                    help="post the deck as a reply chain (first tweet, then replies). One part fail-closes to a single post")
    ap.add_argument("--composer-test", action="store_true",
                    help="open compose, type + verify 'composer test', clear + close — never posts")
    ap.add_argument("--images", default="", help="comma-separated PNG paths (max 4) attached to the first post")
    ap.add_argument("--poll", default="", help="JSON list of 2-4 poll options (first post only; no media with a poll)")
    ap.add_argument("--community", default="", help="X Community ID to publish into (auto-picked for standalone posts when omitted)")
    ap.add_argument("--force", action="store_true")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.check and not args.publish and not args.composer_test:
        build_parser().print_help()
        return EXIT_GATED
    try:
        out = cmd_check(args) if args.check else (
            cmd_composer_test(args) if args.composer_test else cmd_publish(args))
        print(json.dumps(out, indent=2))
        return EXIT_OK
    except Abort as a:
        print(json.dumps({"ok": False, "exit": a.code, "reason": a.reason, **a.extra}, indent=2, default=str))
        log(f"ABORT {a.code} {a.reason}")
        return a.code
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "exit": EXIT_UI, "reason": f"{type(exc).__name__}: {exc}"}))
        return EXIT_UI


if __name__ == "__main__":
    raise SystemExit(main())
