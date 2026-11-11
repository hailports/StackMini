"""Which prep cards may become a note, and the per-run send cap.

The owner display name is supplied by the caller. It is the
System.AssignedTo display name. A card is in scope when all of these hold:

- that field matches, or blocking_who itself names the owner
- the state is still open on that work item type
- the card is in the current iteration, when the caller names one

The next-step sentence does not count. Testing, resolved, closed, done,
and removed are not open. A run that would send more than 25 notes
sends none.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

NOTE_CAP = 25
_NOBODY = {"nobody", "none", "no one", "noone", "n/a", "na"}
_PARTICLES = {"de", "da", "del", "della", "van", "von", "der", "den", "la", "le"}
_CLOSED = {"closed", "removed", "done"}
# Open states are the names before the first testing or terminal state
# on that work item type's own list.
_STOP_STATE = re.compile(
    r"(?i)^(?:"
    r"testing|in testing|ready for test(?:ing)?|"
    r"qa|in qa|in review|code review|review|"
    r"resolved|done|closed|removed|completed|complete|"
    r"cut|cancelled|canceled|abandoned|rejected|duplicate|"
    r"won'?t fix"
    r")$"
)
TYPE_STATES = {
    "User Story": ("New", "Active", "Testing", "Resolved", "Closed", "Removed"),
    "Bug": ("New", "Active", "Testing", "Resolved", "Closed"),
    "Feature": ("New", "Active", "Resolved", "Closed", "Removed"),
    "Task": ("New", "Active", "Closed", "Removed"),
}


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


def allowed_states_for(card: dict):
    raw = card.get("allowed_states")
    if isinstance(raw, (list, tuple)) and any(str(item).strip() for item in raw):
        return tuple(str(item) for item in raw if str(item).strip())
    kind = str(card.get("type") or card.get("work_item_type") or "").strip()
    return TYPE_STATES.get(kind)


def is_open_state(state: str, allowed=None) -> bool:
    """Open means before testing and before any terminal state on that type."""
    name = str(state or "").strip()
    if not name:
        return False
    if allowed:
        open_names = []
        for item in allowed:
            if _STOP_STATE.match(str(item).strip()):
                break
            open_names.append(str(item).strip().lower())
        return name.lower() in open_names
    if name.lower() in _CLOSED:
        return False
    return _STOP_STATE.match(name) is None


def card_iteration(card: dict, file_iteration: str = "") -> str:
    own = str(
        card.get("iteration")
        or card.get("iteration_path")
        or card.get("System.IterationPath")
        or ""
    ).strip()
    return own or str(file_iteration or "").strip()


def scope_reason(card: dict, owner: str, current_iteration: str = "", file_iteration: str = "") -> str:
    """Empty when the card may become a note."""
    state = str(card.get("state") or "").strip()
    if state.lower() in _CLOSED or not is_open_state(state, allowed_states_for(card)):
        return "not_open"
    if not (assigned_to_owner(card, owner) or blocking_names_owner(card, owner)):
        return "not_owner"
    current = str(current_iteration or "").strip()
    if current and card_iteration(card, file_iteration) != current:
        return "not_current_sprint"
    return ""


def in_scope(card: dict, owner: str, current_iteration: str = "", file_iteration: str = "") -> bool:
    return scope_reason(card, owner, current_iteration, file_iteration) == ""


def cap_abort(count: int, cap: int = NOTE_CAP) -> bool:
    """Over the cap, send nothing. Do not send a shorter batch."""
    return int(count) > int(cap)


def select_notes(
    cards,
    owner: str,
    already_sent: set | None = None,
    cap: int = NOTE_CAP,
    current_iteration: str = "",
    file_iteration: str = "",
) -> dict:
    """Pick notes for one run. An over-cap result has an empty send list."""
    send = []
    skipped = {}
    done = already_sent or set()
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        why = scope_reason(card, owner, current_iteration, file_iteration)
        if why:
            skipped[why] = skipped.get(why, 0) + 1
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


def gate_latest(stack, iteration: str = "", owner: str = "") -> dict:
    """Read latest.json and apply the scope. Sends nothing."""
    owner = " ".join(str(owner or os.environ.get("SELFNOTE_ASSIGNEE") or "").split())
    path = Path(stack) / "data" / "ado_prep" / "latest.json"
    if not path.is_file():
        return {"send": [], "aborted": "", "would_send": 0, "skipped": {"no_file": 1}, "cap": NOTE_CAP}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        cards = data.get("cards") or []
        file_iteration = str(data.get("iteration") or "")
    else:
        cards = data or []
        file_iteration = ""
    current = str(iteration or file_iteration or "").strip()
    if file_iteration and current and file_iteration != current:
        return {
            "send": [],
            "aborted": "",
            "would_send": 0,
            "skipped": {"not_current_sprint": len(cards)},
            "cap": NOTE_CAP,
        }
    return select_notes(
        cards,
        owner,
        current_iteration=current,
        file_iteration=file_iteration,
    )
