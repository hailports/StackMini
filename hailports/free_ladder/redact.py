"""Mask passwords / 2FA / secrets before any external provider call."""
from __future__ import annotations

import re
from typing import Any

_PW = re.compile(
    r"(?i)\b(?:password|passwd|passcode|pwd)\b\s*(?::|=|\bis\b)\s*(?!\[masked\])\S+"
)
_TOKEN = re.compile(
    r"(?i)\b(?:api[_ -]?key|secret|token|bearer|authorization)\b\s*(?::|=)\s*(?!\[masked\])\S+"
)
_OTP = re.compile(
    r"(?i)\b(?:2fa|otp|totp|one[\s-]*time(?:\s+pass(?:word|code)?(?:\s+code)?)?|"
    r"recovery\s+codes?|seed\s+phrase|private\s+key)\b\s*(?::|=)?\s*\S+"
)
_AWS = re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*")


def redact_text(text: str) -> str:
    if not text:
        return text
    out = _PW.sub(lambda m: m.group(0).split()[0] + " [masked]", text)
    out = _TOKEN.sub(lambda m: m.group(0).split()[0] + " [masked]", out)
    out = _OTP.sub(lambda m: m.group(0).split()[0] + " [masked]", out)
    out = _AWS.sub("[masked-aws-key]", out)
    out = _BEARER.sub("Bearer [masked]", out)
    return out


def redact_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not messages:
        return []
    out = []
    for m in messages:
        item = dict(m)
        c = item.get("content")
        if isinstance(c, str):
            item["content"] = redact_text(c)
        elif isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    q = dict(p)
                    q["text"] = redact_text(p["text"])
                    parts.append(q)
                else:
                    parts.append(p)
            item["content"] = parts
        out.append(item)
    return out


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    if "messages" in body:
        body["messages"] = redact_messages(body.get("messages"))
    if isinstance(body.get("input"), str):
        body["input"] = redact_text(body["input"])
    if isinstance(body.get("prompt"), str):
        body["prompt"] = redact_text(body["prompt"])
    if isinstance(body.get("system"), str):
        body["system"] = redact_text(body["system"])
    return body
