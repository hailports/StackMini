"""OAuth device and loopback flows. The client id belongs to the user. Nothing is shipped."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlparse

from hailports.integrations.http import call_json
from hailports.integrations.store import IntegrationError

GOOGLE_DEVICE = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GRAPH_DEVICE = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
GRAPH_TOKEN = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

GOOGLE_SCOPES = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar.readonly"
GRAPH_SCOPES = "Mail.Read Calendars.Read Notes.Read offline_access"


@dataclass
class DevicePrompt:
    user_code: str
    verification_url: str
    interval: int
    device_code: str

    def __repr__(self) -> str:
        return "DevicePrompt()"


def _prompt(doc: dict) -> DevicePrompt:
    url = str(doc.get("verification_url") or doc.get("verification_uri") or "")
    code = str(doc.get("device_code") or "")
    user = str(doc.get("user_code") or "")
    if not url.startswith("https://") or not code or not user:
        raise IntegrationError("device login did not return a code")
    try:
        interval = int(doc.get("interval") or 5)
    except (TypeError, ValueError):
        interval = 5
    interval = min(10, max(1, interval))
    return DevicePrompt(user, url, interval, code)


def google_device_start(http, client_id: str, scopes: str = GOOGLE_SCOPES) -> DevicePrompt:
    if not client_id:
        raise IntegrationError("google needs your client id")
    doc = call_json(http, "POST", GOOGLE_DEVICE, form={"client_id": client_id, "scope": scopes})
    return _prompt(doc)


def graph_device_start(http, client_id: str, scopes: str = GRAPH_SCOPES) -> DevicePrompt:
    if not client_id:
        raise IntegrationError("microsoft graph needs your client id")
    doc = call_json(http, "POST", GRAPH_DEVICE, form={"client_id": client_id, "scope": scopes})
    return _prompt(doc)


def _token_doc(doc: dict) -> dict:
    if not isinstance(doc, dict) or not doc.get("access_token"):
        error = str(doc.get("error") or "") if isinstance(doc, dict) else ""
        if error == "authorization_pending":
            return {"pending": True}
        raise IntegrationError("the provider did not return a token")
    return {
        "access_token": str(doc.get("access_token") or ""),
        "refresh_token": str(doc.get("refresh_token") or ""),
        "expiry": int(doc.get("expires_in") or 3600),
    }


def poll_device(http, token_url: str, form: dict, *, interval: int = 5, attempts: int = 60, sleep=None) -> dict:
    pause = time.sleep if sleep is None else sleep
    for _ in range(max(1, attempts)):
        doc = call_json(http, "POST", token_url, form=form)
        if isinstance(doc, dict) and doc.get("error") == "authorization_pending":
            pause(interval)
            continue
        return _token_doc(doc)
    raise IntegrationError("device login timed out")


def google_device_finish(http, client_id: str, prompt: DevicePrompt, client_secret: str = "", attempts: int = 60, sleep=None) -> dict:
    form = {
        "client_id": client_id,
        "device_code": prompt.device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    if client_secret:
        form["client_secret"] = client_secret
    token = poll_device(http, GOOGLE_TOKEN, form, interval=prompt.interval, attempts=attempts, sleep=sleep)
    token["client_id"] = client_id
    if client_secret:
        token["client_secret"] = client_secret
    return token


def graph_device_finish(http, client_id: str, prompt: DevicePrompt, attempts: int = 60, sleep=None) -> dict:
    form = {
        "client_id": client_id,
        "device_code": prompt.device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    token = poll_device(http, GRAPH_TOKEN, form, interval=prompt.interval, attempts=attempts, sleep=sleep)
    token["client_id"] = client_id
    return token


def google_auth_url(client_id: str, redirect: str, state: str, scopes: str = GOOGLE_SCOPES) -> str:
    if not redirect.startswith("http://127.0.0.1:") and not redirect.startswith("http://localhost:"):
        raise IntegrationError("the redirect must stay on this computer")
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": scopes,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    })
    return GOOGLE_AUTH + "?" + query


def exchange_code(http, client_id: str, code: str, redirect: str, client_secret: str = "", token_url: str = GOOGLE_TOKEN) -> dict:
    form = {
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }
    if client_secret:
        form["client_secret"] = client_secret
    doc = call_json(http, "POST", token_url, form=form)
    token = _token_doc(doc)
    token["client_id"] = client_id
    if client_secret:
        token["client_secret"] = client_secret
    return token


def refresh_token(http, token_url: str, client_id: str, refresh: str, client_secret: str = "") -> dict:
    if not refresh:
        raise IntegrationError("there is no refresh token")
    form = {"client_id": client_id, "refresh_token": refresh, "grant_type": "refresh_token"}
    if client_secret:
        form["client_secret"] = client_secret
    doc = call_json(http, "POST", token_url, form=form)
    token = _token_doc(doc)
    if not token.get("refresh_token"):
        token["refresh_token"] = refresh
    token["client_id"] = client_id
    if client_secret:
        token["client_secret"] = client_secret
    return token


class _Callback(BaseHTTPRequestHandler):
    queue: Queue
    state: str

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        sent = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        ok = parsed.path.startswith("/callback") and sent == self.state and bool(code)
        if ok:
            self.queue.put(code)
            body = b"Signed in. You can close this window."
            status = 200
        else:
            body = b"That sign-in did not match. Close this window and try again."
            status = 400
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def listen_for_code(state: str, timeout: float = 180, port: int = 0) -> tuple[ThreadingHTTPServer, Queue]:
    """Bind 127.0.0.1 and collect one authorization code. The caller reads the queue."""
    queue: Queue = Queue()

    class Handler(_Callback):
        pass

    Handler.queue = queue
    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.timeout_s = timeout
    return server, queue


def take_code(queue: Queue, timeout: float) -> str:
    try:
        return str(queue.get(timeout=timeout))
    except Empty as exc:
        raise IntegrationError("the loopback sign-in timed out") from exc


def bearer(token: str) -> dict:
    return {"Authorization": "Bearer " + token, "Accept": "application/json"}


def dumps_safe(doc: dict) -> str:
    """Status files and logs call this only for non-secret fields."""
    return json.dumps(doc)
