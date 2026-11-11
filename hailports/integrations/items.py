"""Normalized documents and the secret mask applied before indexing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from hailports.brain.dataline import hit_block, path_block, redact_secrets

SURFACES = ("email", "calendar", "notes", "chats")
WINDOW_SURFACES = ("email", "calendar", "chats")
STOP = {
    "that", "this", "with", "from", "your", "have", "mail", "note", "notes",
    "chat", "chats", "item", "message", "about", "there", "their", "masked",
}


@dataclass
class Item:
    surface: str
    provider: str
    external_id: str
    title: str
    body: str
    when: float = 0
    kind: str = ""

    def kind_name(self) -> str:
        if self.kind:
            return self.kind
        return {"email": "email", "calendar": "calendar", "notes": "note", "chats": "chat"}.get(self.surface, "note")


def window_start(now: float, days: int, surface: str) -> float | None:
    """Mail, calendar, and chats use the day window. Notes are the whole set."""
    if surface not in WINDOW_SURFACES:
        return None
    span = 30 if days <= 0 else int(days)
    return float(now) - (span * 86400)


def apply_mask(title: str, body: str, source: str, terms: list[str]) -> tuple[str, str] | None:
    """Return redacted title and body, or None when the source is restricted or nothing is left."""
    if path_block(source):
        return None
    safe_title = title or ""
    safe_body = body or ""
    for term in terms or []:
        text = str(term or "")
        if len(text) < 3:
            continue
        safe_title = safe_title.replace(text, "[masked]")
        safe_body = safe_body.replace(text, "[masked]")
    safe_title = redact_secrets(safe_title)
    safe_body = redact_secrets(safe_body)
    if hit_block({"title": safe_title, "text": safe_body, "source": source}):
        return None
    if not (safe_title.strip() or safe_body.strip()):
        return None
    return safe_title.strip(), safe_body.strip()


def needle_for(title: str, body: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", f"{title or ''} {body or ''}")
    kept = [word for word in words if word.lower() not in STOP]
    if not kept:
        return ""
    kept.sort(key=len, reverse=True)
    return kept[0]
