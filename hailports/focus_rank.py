"""Current-work ranking for local chat. Stdlib only. No network.

A self-focus question asks what the user is working on and does not name a
ticket or a project. Current focus is a fresh Active row whose owner equals
the user when an owner is present. Ticket ids compare as whole tokens.
Short names are not expanded here.
"""

from __future__ import annotations

import re
from datetime import date

FOCUS_REV = "20261105-recovery"

_SELF_FOCUS = re.compile(
    r"(?i)(?:"
    r"what am i (?:currently )?focused on"
    r"|what(?:'s| is) my (?:current )?focus"
    r"|what should i (?:be )?focus(?:ed|ing) on"
    r")"
)
# Whole token only. SF-12, SF10001, and 5–8 digit ids count. A 4-digit
# year does not. Matching is exact set membership later, never a substring.
_TICKET_ID = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:SF-\d+|SF\d{3,}|\d{5,8})(?![A-Za-z0-9])"
)
_PAREN_Q = re.compile(r"\([^)]*\?[^)]*\)")
_ACRONYM_PAREN_Q = re.compile(r"\b[A-Z]{2,8}\s*\([^)]*\)\s*\?")
_ACRONYM_Q = re.compile(r"\b[A-Z]{2,8}\s*\?")
_ACRONYM_PAREN = re.compile(r"\b([A-Z]{2,8})\s*\(\s*([^)]*)\)")
_SPELLOUT = re.compile(
    r"\b([A-Z]{2,8})\b\s+(?:means|stands for|is short for)\s+([^.]{1,120})"
)
_TERMINAL = re.compile(
    r"\b(?:closed|resolved|done|removed|cancelled|canceled|backlog|halted)\b",
    re.I,
)
_LOWER = "Lower rank (not the user's current assignment, or stale):"
_OWNER_KEYS = ("owner", "assigned", "assigned_to", "assignee")


def _flat(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _norm_owner(value: str) -> str:
    return _flat(value).casefold()


def _question(value: str) -> str:
    text = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    return _flat(text)


def is_self_focus(question: str, *, named: re.Pattern[str] | None = None) -> bool:
    """True for a current-focus question that does not name a ticket or project."""
    text = _question(question)
    if not _SELF_FOCUS.search(text):
        return False
    if _TICKET_ID.search(text):
        return False
    if named is not None and named.search(text):
        return False
    return True


def phrase_instructions() -> str:
    return (
        "The question asks what the user is focused on now. "
        "Use only FACTS. Cite the source time, each item's state, its changed date, "
        "and its link when FACTS include one. "
        "Do not add a ticket, person, or project that is not in FACTS. "
        "A ticket id must be copied exactly. Do not invent a shorter or longer id. "
        "Do not expand a short name. If FACTS do not spell it out, leave it as written. "
        "Do not present another person's item as the user's focus. "
        "Only fresh Active items assigned to the user are current focus. "
        "New, backlog, inactive, and halted items are not current focus."
    )


def grounding_rules() -> str:
    return (
        "Cite the date and the owner on each item when this pack has them. "
        "Do not call an item the user's current focus unless the owner equals the user. "
        "Do not expand a short name unless this pack spells the words out. "
        "If it does not, leave the short form as written."
    )


def owner_is_user(owner: str, user_markers: tuple[str, ...]) -> bool:
    """True only when the whole owner string equals a marker.

    Comparison is case-insensitive and collapses whitespace. A shorter token
    is not a match: "ann" is not "Joanne", and "example" is not "Example User".
    """
    left = _norm_owner(owner)
    if not left:
        return False
    for marker in user_markers:
        right = _norm_owner(marker)
        if right and left == right:
            return True
    return False


def _id_set(text: str) -> set[str]:
    return {match.group(0).casefold() for match in _TICKET_ID.finditer(text or "")}


def _parse_ticket(line: str) -> tuple[str, str, str] | None:
    if not line.startswith("- "):
        return None
    parts = [part.strip() for part in line.split("|")]
    owner = ""
    changed = ""
    owner_at = -1
    changed_at = -1
    for index, part in enumerate(parts):
        low = part.casefold()
        if low.startswith("owner:"):
            owner = part.split(":", 1)[1].strip()
            owner_at = index
        elif low.startswith("changed"):
            changed = part.split(None, 1)[-1].strip()
            changed_at = index
    if owner_at > 0:
        state = parts[owner_at - 1]
    elif changed_at > 1:
        state = parts[changed_at - 1]
    else:
        state = ""
    if not owner and not changed:
        return None
    return owner, state, changed[:10]


def _changed_date(raw: str) -> date | None:
    token = (raw or "").strip()[:10]
    try:
        return date.fromisoformat(token)
    except ValueError:
        return None


def _as_of_date(as_of: str) -> date | None:
    return _changed_date(as_of or "")


def _terminal(state: str) -> bool:
    return _TERMINAL.search(state or "") is not None


def _active(state: str) -> bool:
    """The state head is exactly Active, and no terminal word is present.

    "Inactive", "Not active", "non-active", and "Deactivated" are not Active.
    "Active (Closed)" is not current. "unresolved" does not contain the word
    "resolved", and "abandoned" does not contain the word "done".
    """
    raw = _flat(state)
    if not raw:
        return False
    head = re.split(r"[(/]", raw, maxsplit=1)[0].strip().casefold()
    head = _flat(head)
    if head != "active":
        return False
    if _terminal(raw):
        return False
    return True


def _stale(state: str, changed: str, today: date, stale_days: int) -> bool:
    if _terminal(state):
        return True
    when = _changed_date(changed)
    if when is None:
        return True
    return (today - when).days > stale_days


def _bucket(
    owner: str,
    state: str,
    changed: str,
    *,
    mine: bool,
    identities: tuple[str, ...],
    today: date,
    stale_days: int,
) -> tuple[int, int]:
    when = _changed_date(changed)
    ordinal = when.toordinal() if when else 0
    stale = _stale(state, changed, today, stale_days)
    is_user = bool(mine) or owner_is_user(owner, identities)
    if is_user and _active(state) and not stale:
        return (0, -ordinal)
    if is_user and not stale:
        return (1, -ordinal)
    if not stale:
        return (2, -ordinal)
    return (3, -ordinal)


def _row_owner(row: dict) -> str:
    for key in _OWNER_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _link(row: dict) -> str:
    for key in ("url", "link", "href"):
        value = row.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text.startswith("https://") or text.startswith("http://"):
            return text
    return ""


def rerank_ticket_lines(
    text: str,
    *,
    user_markers: tuple[str, ...] = (),
    today: date | None = None,
    stale_days: int = 21,
) -> str:
    """Put the user's fresh Active items first. Demote every other line."""
    lines = (text or "").splitlines()
    today = today or date.today()
    identities = tuple(user_markers)
    out: list[str] = []
    index = 0
    count = len(lines)
    while index < count:
        if _parse_ticket(lines[index]) is None:
            out.append(lines[index])
            index += 1
            continue
        block: list[str] = []
        while index < count and (
            not lines[index].strip() or _parse_ticket(lines[index]) is not None
        ):
            if _parse_ticket(lines[index]) is not None:
                block.append(lines[index])
            index += 1
        ranked = sorted(
            block,
            key=lambda line: _bucket(
                *(_parse_ticket(line) or ("", "", "")),
                mine=False,
                identities=identities,
                today=today,
                stale_days=stale_days,
            ),
        )
        labeled = False
        for line in ranked:
            parsed = _parse_ticket(line) or ("", "", "")
            bucket = _bucket(
                *parsed,
                mine=False,
                identities=identities,
                today=today,
                stale_days=stale_days,
            )[0]
            if not labeled and bucket >= 1:
                out.append(_LOWER)
                labeled = True
            out.append(line)
    return "\n".join(out)


def _current_row(row: dict, *, user: str, today: date, stale_days: int) -> bool:
    if not isinstance(row, dict):
        return False
    state = str(row.get("state") or "")
    changed = str(row.get("changed") or "")
    if not _active(state) or _stale(state, changed, today, stale_days):
        return False
    owner = _row_owner(row)
    if owner:
        if row.get("mine") is True and (not user or owner_is_user(owner, (user,))):
            return True
        if not user or not owner_is_user(owner, (user,)):
            return False
    return True


def focus_lines(
    items: list[dict],
    *,
    as_of: str,
    user: str = "",
    stale_days: int = 21,
) -> str:
    """Fresh Active rows only. Titles, dates, and links are copied."""
    today = _as_of_date(as_of)
    kept: list[tuple[int, int, str]] = []
    if today is not None:
        for index, row in enumerate(items):
            if not _current_row(row, user=user, today=today, stale_days=stale_days):
                continue
            title = _flat(str(row.get("title") or ""))
            changed = str(row.get("changed") or "").strip()
            item_id = str(row.get("id") or "").strip()
            state = _flat(str(row.get("state") or ""))
            if not item_id or not title:
                continue
            bit = f"- {item_id} | {title} | {state} | changed {changed}"
            link = _link(row)
            if link:
                bit += f" | {link}"
            when = _changed_date(changed)
            ordinal = when.toordinal() if when else 0
            kept.append((ordinal, index, bit))
    kept.sort(key=lambda item: (-item[0], item[1]))
    lines = [bit for _ordinal, _index, bit in kept]
    stamp = (as_of or "").strip() or "an unknown time"
    if not lines:
        return (
            f"Assigned docket as of {stamp}. "
            "No fresh Active items assigned to the user. "
            "New, backlog, inactive, and stale items are not current focus."
        )
    head = (
        f"Assigned docket as of {stamp}. "
        "Fresh Active items assigned to the user. "
        "New, backlog, inactive, and stale items are not current focus."
    )
    return head + "\n" + "\n".join(lines)


def pack_focus(
    cards: list[dict],
    *,
    limit: int = 5,
    user_markers: tuple[str, ...] = (),
    user: str = "",
    today: date | None = None,
    stale_days: int = 21,
) -> str:
    """Rank focus cards. Only the user's fresh Active items sit above the line."""
    today = today or date.today()
    identities = [marker for marker in user_markers if _norm_owner(marker)]
    if _norm_owner(user):
        identities.append(user)
    records: list[dict] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        owner = str(card.get("owner") or "").strip()
        if card.get("mine") is True and _norm_owner(owner):
            identities.append(owner)
        records.append(card)
    identity_tuple = tuple(identities)
    ranked = sorted(
        records,
        key=lambda card: _bucket(
            str(card.get("owner") or ""),
            str(card.get("state") or ""),
            str(card.get("changed") or ""),
            mine=card.get("mine") is True,
            identities=identity_tuple,
            today=today,
            stale_days=stale_days,
        ),
    )
    out: list[str] = []
    shown = 0
    labeled = False
    for card in ranked:
        bucket = _bucket(
            str(card.get("owner") or ""),
            str(card.get("state") or ""),
            str(card.get("changed") or ""),
            mine=card.get("mine") is True,
            identities=identity_tuple,
            today=today,
            stale_days=stale_days,
        )[0]
        if not labeled and bucket >= 1:
            out.append(_LOWER)
            labeled = True
        if shown >= limit:
            continue
        shown += 1
        owner = str(card.get("owner") or "").strip()
        state = _flat(str(card.get("state") or ""))
        changed = str(card.get("changed") or "").strip()[:10]
        title = _flat(str(card.get("title") or "untitled"))
        item_id = str(card.get("id") or "").strip()
        line = (
            f"- {item_id} | {title} | {state} | owner: {owner or 'unknown'} | changed {changed}"
        )
        link = _link(card)
        if link:
            line += f" | {link}"
        out.append(line)
    return "\n".join(out)


def _acronym_guess(text: str, facts: str) -> bool:
    if _PAREN_Q.search(text) or _ACRONYM_PAREN_Q.search(text) or _ACRONYM_Q.search(text):
        return True
    facts_low = (facts or "").casefold()
    for match in _ACRONYM_PAREN.finditer(text):
        expansion = match.group(2).strip().rstrip("?").strip()
        if expansion and expansion.casefold() not in facts_low:
            return True
    for match in _SPELLOUT.finditer(text):
        expansion = match.group(2).strip()
        if expansion and expansion.casefold() not in facts_low:
            return True
    return False


def keep_grounded(model_text: str, facts: str) -> str:
    """Replace a reply that adds a ticket id or expands a short name."""
    facts = facts or ""
    text = (model_text or "").strip()
    if not text:
        return facts
    if _acronym_guess(text, facts):
        return facts
    known = _id_set(facts)
    for token in _TICKET_ID.findall(text):
        if token.casefold() not in known:
            return facts
    return text
