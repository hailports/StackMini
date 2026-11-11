"""Which prep cards may become a note, and the per-run send cap.

The owner display name is supplied by the caller. It is the
System.AssignedTo display name. A card is in scope when that field
matches, or when blocking_who itself names the owner. The next-step
sentence does not count. A run that would send more than 25 notes
sends none.
"""
from __future__ import annotations

import re

NOTE_CAP = 25
_NOBODY = {"nobody", "none", "no one", "noone", "n/a", "na"}
_PARTICLES = {"de", "da", "del", "della", "van", "von", "der", "den", "la", "le"}


def assignee_name(card: dict) -> str:
    """Display name from System.AssignedTo, stored on the card as assignee."""
    raw = card.get("assignee")
    if raw in (None, ""):
        raw = card.get("System.AssignedTo") or ""
    if isinstance(raw, dict):
        raw = raw.get("displayName") or raw.get("uniqueName") or ""
    return " ".join(str(raw).split())


def _same_name(left: str, right: str) -> bool:
    return " ".join(str(left).split()).casefold() == " ".join(str(right).split()).casefold()


def name_tokens(owner: str) -> list[str]:
    parts = [p for p in str(owner or "").replace(",", " ").split() if p]
    return [p for p in parts if p.casefold() not in _PARTICLES]


def assigned_to_owner(card: dict, owner: str) -> bool:
    owner = " ".join(str(owner or "").split())
    text = assignee_name(card)
    if not owner or not text:
        return False
    return _same_name(text, owner)


def blocking_names_owner(card: dict, owner: str) -> bool:
    """True only when blocking_who itself names the owner."""
    who = " ".join(str(card.get("blocking_who") or "").split())
    if not who or who.casefold() in _NOBODY:
        return False
    for token in name_tokens(owner):
        if re.search(r"(?i)(?<![A-Za-z])" + re.escape(token) + r"(?![A-Za-z])", who):
            return True
    return False


def in_scope(card: dict, owner: str) -> bool:
    return assigned_to_owner(card, owner) or blocking_names_owner(card, owner)


def cap_abort(count: int, cap: int = NOTE_CAP) -> bool:
    """Over the cap, send nothing. Do not send a shorter batch."""
    return int(count) > int(cap)


def select_notes(cards, owner: str, already_sent: set | None = None, cap: int = NOTE_CAP) -> dict:
    """Pick notes for one run. An over-cap result has an empty send list."""
    send = []
    skipped = {}
    done = already_sent or set()
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        if not in_scope(card, owner):
            skipped["not_owner"] = skipped.get("not_owner", 0) + 1
            continue
        tid = str(card.get("id") or "")
        if tid and tid in done:
            skipped["unchanged"] = skipped.get("unchanged", 0) + 1
            continue
        send.append(card)
    if cap_abort(len(send), cap):
        skipped["cap"] = len(send)
        return {"send": [], "aborted": "cap", "would_send": len(send), "skipped": skipped, "cap": cap}
    return {"send": send, "aborted": "", "would_send": len(send), "skipped": skipped, "cap": cap}
