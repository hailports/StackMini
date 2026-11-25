#!/usr/bin/env python3
"""One bounded session-maintenance tick for a dedicated macOS browser."""

import argparse
from contextlib import closing
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

HOME = {"zoom": "https://app.zoom.us/wc/team-chat", "outlook": "https://outlook.office.com/mail/"}
COOLDOWN = 600
ZOOM_SITE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.zoom\.us")


def route(provider, href, zoom_site):
    if href == "about:blank":
        return "blank"
    url = urlsplit(href)
    if url.scheme != "https" or not url.hostname or url.netloc != url.hostname:
        return "other"
    host, path = url.hostname, url.path.rstrip("/")
    if host == "login.microsoftonline.com":
        return "interactive"
    if provider == "zoom" and host in {"zoom.us", zoom_site, "app.zoom.us"}:
        if path in {"/signin", "/wc/signin", "/web/sso/login"} or path.startswith("/saml/"):
            return "interactive"
        if url.query or url.fragment:
            return "other"
        if host == "app.zoom.us" and path in {"/wc", "/wc/home", "/wc/team-chat"}:
            return "home"
        if host in {"zoom.us", zoom_site} and path in {"/profile", "/myhome", "/meeting"}:
            return "portal"
    if provider == "outlook" and host in {"outlook.office.com", "outlook.cloud.microsoft"}:
        if path == "/mail" and not url.query and not url.fragment:
            return "home"
        if path.startswith("/mail/"):
            return "work"
    return "other"


def check_owner(job, port):
    ctl = subprocess.check_output(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{job}"], text=True, timeout=3)
    pid = re.search(r"^\s*pid = ([0-9]+)$", ctl, re.M)
    if not pid:
        raise ValueError("missing browser")
    sockets = subprocess.check_output(
        ["/usr/sbin/lsof", "-aPn", "-p", pid[1], f"-iTCP:{port}", "-sTCP:LISTEN", "-Fn"],
        text=True, timeout=3)
    names = {s[1:] for s in sockets.splitlines() if s.startswith("n")}
    if names != {f"127.0.0.1:{port}"}:
        raise ValueError("listener ownership mismatch")


def pages(port):
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as connection:
        connection.request("GET", "/json/list")
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("target list unavailable")
        return [t for t in json.loads(response.read(1_000_000)) if t.get("type") == "page"]


class Page:
    def __init__(self, port, target_id):
        import websocket
        if not re.fullmatch(r"[a-zA-Z0-9-]+", target_id):
            raise ValueError("invalid page")
        self.ws = websocket.create_connection(f"ws://127.0.0.1:{port}/devtools/page/{target_id}",
                                               timeout=3, suppress_origin=True, http_no_proxy=["127.0.0.1"])
        self.serial = 0
        self.crashed = False

    def close(self):
        self.ws.close(timeout=1)

    def call(self, method, **params):
        self.serial += 1
        self.ws.send(json.dumps(dict(id=self.serial, method=method, params=params)))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.ws.settimeout(max(0.01, deadline - time.monotonic()))
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Inspector.targetCrashed":
                self.crashed = True
            if msg.get("id") == self.serial:
                if "error" in msg:
                    raise RuntimeError(method)
                return msg["result"]
        raise TimeoutError(method)

    def fault(self, href):
        try:
            self.call("Inspector.enable")
            frame = self.call("Page.getFrameTree")["frameTree"]["frame"]
        except Exception:
            if self.crashed:
                return True
            raise
        return self.crashed or frame.get("unreachableUrl") == href

    def evaluate(self, expression):
        reply = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if "exceptionDetails" in reply:
            raise ValueError("page evaluation failed")
        return reply["result"]["value"]


# Return a fixed status only. Compose controls, chat contents and input values never leave the page.
UI = """
const visible = selector => [...document.querySelectorAll(selector)].some(e => e.getClientRects().length);
const editing = () => document.activeElement?.matches('input, textarea, [contenteditable="true"]') ||
    visible('[role="dialog"] [contenteditable="true"], input[type="password"]');
"""
SEARCH = '[role="searchbox"], [aria-label^="Search" i], input[placeholder^="Search" i]'
APP_CONTROL = {"zoom": '[aria-label="Team Chat" i], [title="Team Chat" i], a[href="/wc/team-chat"]',
               "outlook": 'button[aria-label*="New mail" i], button[title*="New mail" i]'}
SIGNIN = 'a[href*="/signin"], button[aria-label="sign in" i]'
OUTLOOK_SIGNIN = r"""
const outlookSignIn = () => [...document.querySelectorAll('a[href]')].find(link => {
    if (!link.getClientRects().length || link.checkVisibility?.({checkOpacity: true, checkVisibilityCSS: true}) === false)
        return false;
    let url;
    try { url = new URL(link.href); } catch { return false; }
    return url.origin === 'https://login.microsoftonline.com' && !url.username && !url.password &&
        /^\/[^/]+\/oauth2\/(?:v2\.0\/)?authorize\/?$/i.test(url.pathname);
});
const signInControl = () => [...document.querySelectorAll('a[href], button, [role="button"]')].some(el =>
    el.getClientRects().length && /^(sign|log)\s*in\b/i.test((el.getAttribute('aria-label') || el.textContent).trim()));
"""


def ui(page, href, provider):
    sign_in = f"if (visible({json.dumps(SIGNIN)})) return 'signed_out';"
    if provider == "outlook":
        sign_in = OUTLOOK_SIGNIN + """
            if (outlookSignIn()) return 'signed_out';
            if (signInControl() || visible('a[href*="/signin"]')) return 'sign_in_pending';
        """
    return page.evaluate("(() => {" + UI + f"""
        if (location.href !== {json.dumps(href)}) return 'target_changed';
        if (visible({json.dumps(SEARCH)}) && visible({json.dumps(APP_CONTROL[provider])})) return 'ready';
        if (editing()) return 'input_in_use';
        {sign_in}
        return 'ui_unverified';
    }})()""")


def navigate(page, href, dest):
    # Resolve the ordinary OAuth URL in the tab so redirect parameters stay inside the browser.
    move = f"location.assign({json.dumps(dest)});" if dest is not None else OUTLOOK_SIGNIN + """
        const link = outlookSignIn();
        if (!link) return 'sign_in_pending';
        location.assign(link.href);
    """
    return page.evaluate("(() => {" + UI + f"""
        if (location.href !== {json.dumps(href)}) return 'target_changed';
        if (editing()) return 'input_in_use';
        {move}
        return 'recovering';
    }})()""")


def maintain(provider, zoom_site, state, now, list_pages, open_page, save):
    targets = list_pages()
    if len(targets) != 1:
        state.pop("blank_target", None)
        return "empty_browser" if not targets else "multiple_tabs"
    target = targets[0]
    tid, href = target["id"], target["url"]
    kind = route(provider, href, zoom_site)
    if kind != "blank":
        state.pop("blank_target", None)
    if kind == "interactive":
        return "sign_in_pending"
    if kind == "other":
        return "active_page"
    if kind == "work":
        with closing(open_page(tid)) as page:
            return "ready" if ui(page, href, provider) == "ready" else "active_page"
    if kind == "blank" and state.get("blank_target") != tid:
        state["blank_target"] = tid
        return "empty_tab"
    returning = kind == "portal" and state.get("sign_in_target") == tid and now < state.get("return_by", 0)
    if kind == "portal" and not returning:
        return "active_page"

    with closing(open_page(tid)) as page:
        dest = HOME[provider]
        action = "restore_home"
        if kind == "home":
            if page.fault(href):
                action = "reload"
            else:
                status = ui(page, href, provider)
                if status != "signed_out":
                    if status == "ready":
                        state.pop("sign_in_target", None)
                    return status
                action = "open_sign_in"
                if provider == "zoom":
                    dest = "https://" + (zoom_site or "zoom.us") + "/signin"
                else:
                    dest = None
        if not returning and now < state.get("retry_after", 0):
            return "cooldown"
        current = list_pages()
        if len(current) != 1 or (current[0]["id"], current[0]["url"]) != (tid, href):
            return "target_changed"
        state["retry_after"] = now + COOLDOWN
        save("recovering")
        if action == "reload":
            page.call("Page.reload")
            return "recovering"
        result = navigate(page, href, dest)
        if result == "recovering":
            if action == "open_sign_in" and provider == "zoom":
                state.update(sign_in_target=tid, return_by=now + 900)
            elif returning:
                state.pop("sign_in_target", None)
        return result


def run(args):
    folder = Path.home() / "Library" / "Application Support" / "StackMini" / "native-session"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = f"{args.provider}-{args.port}"
    status_file = folder / (key + ".json")
    with (folder / (key + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "already_running"
        state = json.loads(status_file.read_text()) if status_file.exists() else {}

        def save(status):
            state.update(status=status, checked_at=int(time.time()))
            with tempfile.NamedTemporaryFile(dir=folder, mode="w", delete=False) as output:
                json.dump(state, output)
            os.replace(output.name, status_file)

        try:
            check_owner(args.browser_job, args.port)
        except (OSError, ValueError, subprocess.SubprocessError):
            save("browser_unavailable")
            return "browser_unavailable"
        try:
            result = maintain(args.provider, args.zoom_site, state, int(time.time()),
                              lambda: pages(args.port), lambda tid: Page(args.port, tid), save)
        except Exception:
            result = "probe_failed"
        save(result)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=HOME)
    parser.add_argument("--browser-job", required=True, help="launchd label of the dedicated browser process")
    parser.add_argument("--port", required=True, type=int, help="its IPv4 loopback DevTools port")
    parser.add_argument("--zoom-site", help="optional organization vanity hostname under zoom.us")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or (args.zoom_site and
            (args.provider != "zoom" or not ZOOM_SITE.fullmatch(args.zoom_site))):
        parser.error("invalid port or Zoom vanity hostname")
    status = run(args)
    print(status)
    sys.exit(0 if status == "ready" else 1)
