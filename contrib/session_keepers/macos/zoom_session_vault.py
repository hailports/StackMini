#!/usr/bin/env python3
"""Keep the Zoom web login alive across browser restarts.

Zoom's chat credential (`cred`, `_zm_ssid`, `_zm_page_auth`, `zm_aid`) is a pure
SESSION cookie -- `is_persistent=0`. Chrome drops those on launch unless session
restore is on, and `tools/zoom_cdp_recover.py` restarts this profile constantly
(238 restarts to date, 8 on 2026-07-30 alone) to unwedge CDP. Every one of those
restarts was a coin flip on the login, and on 2026-07-29 ~16:00 the flip came up
tails: the bridge went blind for ~18h until a human re-did SSO.

So: snapshot the authenticated cookie jar after every verified-good read, and
re-inject it when the sidebar goes missing. A lost session then costs one silent
$0 self-heal instead of the operator's SSO. Only when the re-injection also fails is the
login genuinely gone and worth paging a human about.

The vault holds live credentials: 0600 file inside a 0700 dir, local disk only,
never a synced/cloud path.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _default_zoom_cdp() -> str:
    explicit = (os.environ.get("ZOOM_WEB_CDP") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        resolve = __import__("importlib").import_module("stack.bridge_endpoints").resolve
        return resolve("zoom", force_probe=False)
    except Exception:
        return "http://box.tailnet-host.example.invalid:9227"


VAULT_DIR = Path.home() / ".zoom_web_session_vault"
VAULT_PATH = VAULT_DIR / "cookies.json"
CDP_URL = _default_zoom_cdp()
TEAM_CHAT_URL = "https://example.zoom.us/wc/team-chat"
SIDEBAR = 'li[class*="_sessionItem_"]'
# The credential set that actually authorizes Team Chat. If a snapshot is missing
# these it is not worth restoring -- better to fail loudly than to "restore" junk.
REQUIRED = {"cred", "_zm_ssid"}


# corp's on-prem AD FS -- it mints the SAML assertion that Zoom turns into `cred`.
FEDERATION_HOST = "idp2.corp.example.com"


def _zoom_cookies(context) -> list[dict]:
    # Vault the federation session alongside Zoom's own. MEASURED 2026-07-30: the AD FS
    # cookies (MSISAuth, MSISAuthenticated, SamlSession) are SESSION-scoped too -- AD FS KMSI
    # is off -- so they die with the browser exactly like `cred` does. Saving only zoom.us
    # meant a restart that could not reuse `cred` had to re-federate, found no AD FS session
    # either, and cost the operator a full manual SSO. cdp_login_keeper already vaults these same
    # cookies into the same 0700 dir, so this adds no new exposure.
    return [
        c for c in context.cookies()
        if "zoom.us" in c.get("domain", "") or FEDERATION_HOST in c.get("domain", "")
    ]


def _has_sidebar(context, settle_s: int = 20, page=None) -> bool:
    # Session restore reopens several Zoom tabs, so match the bridge's own
    # preference order instead of taking whichever example.zoom.us page comes first.
    # Callers that just navigated a page MUST pass it: re-selecting by URL scan
    # picked the stale signed-out /wc landing tab and reported a live session
    # dead, which pages the operator for an SSO he does not need.
    page = page or next((p for p in context.pages if "team-chat" in p.url), None) or next(
        (p for p in context.pages if "example.zoom.us" in p.url), None
    )
    if page is None:
        return False
    # Count with the BRIDGE's own test, not a private copy. Zoom ships the conversation
    # list collapsed and only `_wait_sidebar` expands the sections first -- a private
    # copy reported "restore failed" on a session the server had just accepted.
    _wait_sidebar = __import__("importlib").import_module("tools.zoom_web_send")._wait_sidebar

    frame = next(
        (f for f in page.frames if "team-chat" in f.url and "integrate" in f.url),
        page.main_frame,
    )
    return bool(_wait_sidebar(frame, secs=max(settle_s, 20)))


def save(quiet: bool = False) -> int:
    """Snapshot the cookie jar. Only ever called on a proven-authenticated read."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        connect_zoom_cdp = __import__("importlib").import_module("tools.zoom_cdp_session").connect_zoom_cdp
        browser = connect_zoom_cdp(play, endpoint=CDP_URL)
        context = browser.contexts[0]
        cookies = _zoom_cookies(context)

    names = {c["name"] for c in cookies}
    if not REQUIRED.issubset(names):
        if not quiet:
            print(f"[zoom-vault] refusing to save: missing {sorted(REQUIRED - names)}", file=sys.stderr)
        return 2

    VAULT_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(VAULT_DIR, 0o700)
    payload = {"saved_at": datetime.now(timezone.utc).isoformat(), "cookies": cookies}
    fd = os.open(VAULT_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    if not quiet:
        print(f"[zoom-vault] saved {len(cookies)} zoom cookies")
    return 0


def restore(quiet: bool = False) -> int:
    """Re-inject the saved jar into the running browser and PROVE it took."""

    if not VAULT_PATH.exists():
        if not quiet:
            print("[zoom-vault] no snapshot to restore", file=sys.stderr)
        return 2

    payload = json.loads(VAULT_PATH.read_text(encoding="utf-8"))
    cookies = payload.get("cookies") or []
    if not REQUIRED.issubset({c["name"] for c in cookies}):
        if not quiet:
            print("[zoom-vault] snapshot lacks the chat credential; not restoring", file=sys.stderr)
        return 2

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        connect_zoom_cdp = __import__("importlib").import_module("tools.zoom_cdp_session").connect_zoom_cdp
        browser = connect_zoom_cdp(play, endpoint=CDP_URL)
        context = browser.contexts[0]
        # Prove liveness from a FRESH navigation. Zoom keeps painting its last sidebar
        # after the session cookie dies, so probing the current DOM would report
        # "already authenticated" for a dead session and skip the restore entirely.
        # A restarted browser has no team-chat tab at all (it lands on /wc), so scanning
        # for one skipped the navigation and probed the signed-out landing page instead.
        page = next((p for p in context.pages if "team-chat" in p.url), None) or next(
            (p for p in context.pages if "example.zoom.us" in p.url), None
        )
        if page is not None:
            try:
                page.goto(TEAM_CHAT_URL, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
        if _has_sidebar(context, settle_s=12, page=page):
            if not quiet:
                print("[zoom-vault] already authenticated; nothing to restore")
            return 0
        try:
            # Drop the SP's current jar first. A half-finished SSO attempt leaves partial
            # zoom.us cookies behind, and injecting the vault on top of those produced a
            # jar the server rejected -- observed live 2026-07-30. Non-zoom cookies (the
            # Google IdP session above all) must survive untouched.
            keep = [c for c in context.cookies() if "zoom.us" not in c.get("domain", "")]
            context.clear_cookies()
            context.add_cookies(keep)
            context.add_cookies([c for c in cookies if "zoom.us" in c.get("domain", "")])
            # Replay the AD FS session ONLY when the profile has none of its own. `keep` already
            # preserved any LIVE federation cookies, and a live session must always beat a
            # vaulted one -- injecting a stale MSISAuth over a good one is precisely the
            # "server rejected the jar" failure the block above exists to prevent. When the
            # profile genuinely has no AD FS session, replaying one lets a restart inside the
            # AD FS SsoLifetime (8h default) re-federate silently instead of paging a human.
            if not any(
                FEDERATION_HOST in (c.get("domain") or "") and c.get("name") == "MSISAuth"
                for c in keep
            ):
                federation = [c for c in cookies if FEDERATION_HOST in (c.get("domain") or "")]
                if federation:
                    context.add_cookies(federation)
                    if not quiet:
                        print(f"[zoom-vault] replayed {len(federation)} AD FS federation cookie(s)")
        except Exception as exc:
            if not quiet:
                print(f"[zoom-vault] cookie injection failed: {exc}", file=sys.stderr)
            return 1
        page = next((p for p in context.pages if "example.zoom.us" in p.url), None)
        if page is None:
            page = context.new_page()
        try:
            page.goto(TEAM_CHAT_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        ok = _has_sidebar(context, settle_s=25, page=page)

    age_h = ""
    try:
        saved = datetime.fromisoformat(payload["saved_at"])
        age_h = f" (snapshot {int((datetime.now(timezone.utc) - saved).total_seconds() // 3600)}h old)"
    except Exception:
        pass
    if not quiet:
        print(f"[zoom-vault] restore {'succeeded' if ok else 'failed'}{age_h}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else ""
    quiet = "--quiet" in args
    if cmd == "save":
        return save(quiet)
    if cmd == "restore":
        return restore(quiet)
    if cmd == "status":
        if not VAULT_PATH.exists():
            print("no snapshot")
            return 1
        payload = json.loads(VAULT_PATH.read_text(encoding="utf-8"))
        print(f"snapshot saved {payload.get('saved_at')} ({len(payload.get('cookies') or [])} cookies)")
        return 0
    print("usage: zoom_session_vault.py {save|restore|status} [--quiet]", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
