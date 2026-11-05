"""Tell the owner when a login check fails. The channel is local or loopback only."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


def is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def http_status(url: str, timeout: float = 5.0) -> int:
    from hailports.webtools import _NoRedirect, public_http_url

    if not is_loopback(url) and not public_http_url(url):
        return 0
    req = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            exc.close()
            return 0
        return int(exc.code)
    except Exception:
        return 0


def probe_login(url: str, status_fn=http_status) -> str:
    if not url:
        return "skipped"
    code = int(status_fn(url))
    if code in (401, 403):
        return "login-broken"
    if 200 <= code < 300:
        return "ok"
    return "down"


def alert_text(router, service: str):
    """One model sentence. The probe body is not included."""
    return router.complete(
        [
            {"role": "system", "content": "Write one short sentence. Do not include secrets, tokens, or passwords."},
            {"role": "user", "content": f"The login check for {service} failed."},
        ],
        task="draft",
    )


def deliver(text: str, private_url: str, outbox: Path, post=None) -> dict:
    """Always keep a local copy. POST only to a loopback channel."""
    outbox.parent.mkdir(parents=True, exist_ok=True)
    line = " ".join((text or "").split())
    with outbox.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if not private_url:
        return {"outbox": str(outbox), "posted": False, "reason": "outbox-only"}
    if not is_loopback(private_url):
        return {"outbox": str(outbox), "posted": False, "reason": "channel-not-private"}
    sender = post or _post
    sender(private_url, line)
    return {"outbox": str(outbox), "posted": True, "reason": "posted"}


def _post(url: str, text: str) -> None:
    from hailports.webtools import _NoRedirect

    if not is_loopback(url):
        raise OSError("alert channel is not loopback")
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=5) as resp:
        resp.read()
