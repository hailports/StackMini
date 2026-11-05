"""JSON HTTP calls. Error text never keeps an API key."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class ProviderError(RuntimeError):
    def __init__(self, what: str, how: str) -> None:
        super().__init__(what)
        self.what = what
        self.how = how


def scrub(text: str, secret: str | None) -> str:
    if not text:
        return ""
    if secret and len(secret) >= 4 and secret in text:
        text = text.replace(secret, "[redacted]")
    return text


def request_json(
    url: str,
    payload: dict | None,
    headers: dict[str, str],
    timeout: float,
    secret: str | None = None,
    method: str | None = None,
) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers)
    if data is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = scrub(exc.read(400).decode("utf-8", "replace"), secret)
        raise ProviderError(
            f"The endpoint returned HTTP {exc.code}. {body[:180]}".strip(),
            "Check the base URL, the model name, and the API key.",
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(
            "The endpoint did not answer. " + scrub(str(exc), secret),
            "Check that the address is reachable, then retry.",
        ) from exc
    try:
        doc = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise ProviderError("The endpoint did not return JSON.", "Check the base URL.") from exc
    if not isinstance(doc, dict):
        raise ProviderError("The endpoint returned an unexpected payload.", "Check the base URL and the model name.")
    return doc
