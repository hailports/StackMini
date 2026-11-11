"""HTTP for official APIs. Errors name the host and the status, never the token."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from hailports.integrations.store import IntegrationError
from hailports.local_bridges.fabric import _host_ok


def assert_remote(url: str) -> None:
    parsed = urlparse(url)
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise IntegrationError("url must not contain a secret")
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and _host_ok(url):
        return
    raise IntegrationError("url must be https or loopback")


def default_http(method: str, url: str, headers: dict | None = None, body: bytes | None = None, timeout: float = 30):
    assert_remote(url)
    host = urlparse(url).hostname or ""
    req = Request(url, data=body, method=method.upper(), headers=dict(headers or {}))
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), raw
    except HTTPError as exc:
        raise IntegrationError(f"request failed status {int(exc.code)} host {host}") from None
    except URLError:
        raise IntegrationError(f"request failed host {host}") from None
    except IntegrationError:
        raise
    except Exception:
        raise IntegrationError(f"request failed host {host}") from None


def call_http(http, method: str, url: str, headers: dict | None = None, body: bytes | None = None):
    assert_remote(url)
    host = urlparse(url).hostname or ""
    fn = http or default_http
    try:
        result = fn(method, url, dict(headers or {}), body)
    except IntegrationError:
        raise
    except Exception:
        raise IntegrationError(f"request failed host {host}") from None
    if isinstance(result, tuple):
        status, raw = result
        if int(status) >= 400:
            raise IntegrationError(f"request failed status {int(status)} host {host}")
        return raw
    return result


def call_json(http, method: str, url: str, headers: dict | None = None, payload=None, form: dict | None = None):
    head = dict(headers or {})
    body = None
    if form is not None:
        from urllib.parse import urlencode
        body = urlencode(form).encode("utf-8")
        head.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif payload is not None:
        body = json.dumps(payload).encode("utf-8")
        head.setdefault("Content-Type", "application/json")
    raw = call_http(http, method, url, head, body)
    if not str(raw).strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        raise IntegrationError("the provider did not return json") from None
    if not isinstance(loaded, dict) and not isinstance(loaded, list):
        raise IntegrationError("the provider did not return json")
    return loaded
