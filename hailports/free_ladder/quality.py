"""Lightweight quality gate for free-ladder responses."""
from __future__ import annotations

import re
from typing import Any

_EMPTYISH = re.compile(r"^\s*$")
_REFUSAL = re.compile(
    r"(?i)\b(?:i (?:can'?t|cannot|unable to) (?:help|assist)|as an ai|"
    r"i don'?t have access|error(?:\s+code)?\s*\d{3})\b"
)
_AI = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:as an ai|as a language model|i'?m (?:an )?ai|"
    r"i hope this helps|certainly!)(?![A-Za-z0-9_-])"
)


def extract_text(resp: dict[str, Any]) -> str:
    choices = resp.get("choices") or []
    if not choices:
        content = resp.get("content")
        if isinstance(content, list):
            return "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
        return ""
    msg = (choices[0].get("message") or {})
    c = msg.get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c if isinstance(p, dict)).strip()
    return ""


def quality_ok(text: str, *, task: str = "chat", min_chars: int = 1) -> tuple[bool, str]:
    if text is None or _EMPTYISH.match(text or ""):
        return False, "empty"
    t = text.strip()
    if len(t) < min_chars:
        return False, "too_short"
    if _REFUSAL.search(t) and len(t) < 80:
        return False, "refusal"
    if _AI.search(t) and task in {"code", "reasoning"}:
        return False, "ai_fingerprint"
    return True, "ok"
