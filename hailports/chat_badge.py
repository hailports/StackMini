"""When a chat badge may be put back, and when a draft must leave it alone.

The same message is restored once. That happens only after this runner opened
a chat that was unread at that moment, and only when the newest line is from
the other person. A badge the operator clears stays clear. Saving a draft
does not mark the chat unread.
"""

from __future__ import annotations

import re

_CAP = 80
_NAMED_PASSKEY = re.compile(r"(?i)\bno matching passkey saved in password\b")
_SECRET_ASSIGN = re.compile(
    r"(?i)\b(api[_-]?key|secret|password|passcode|token|seed phrase|private key)\s*[:=]"
)
_ONE_TIME = re.compile(
    r"(?i)\b(one[- ]time code|verification code|passcode|otp|2fa)\b"
)
_DIGITS = re.compile(r"\b\d{4,8}\b")
_READ_CODE = re.compile(r"[^A-Za-z0-9_.:-]")


def blank() -> dict:
    return {}


def _entry(book: dict, chat_id: str) -> dict:
    ent = book.get(chat_id)
    if not isinstance(ent, dict):
        ent = {"restored_ids": [], "cleared_ids": [], "restored_n": 0, "operator_cleared": False}
        book[chat_id] = ent
    ent.setdefault("restored_ids", [])
    ent.setdefault("cleared_ids", [])
    ent.setdefault("restored_n", 0)
    ent.setdefault("operator_cleared", False)
    return ent


def _trim(ids: list) -> list:
    out = []
    for item in ids:
        text = str(item or "")[:200]
        if text and text not in out:
            out.append(text)
    return out[-_CAP:]


def read_code(value: str) -> str:
    """A short skip code. The message body is not part of it."""
    token = str(value or "").split()
    raw = token[0] if token else ""
    return _READ_CODE.sub("", raw)[:80] or "history_empty"


def secret_hold(text: str) -> str:
    """A reason code when the text holds a secret value. Empty when it does not.

    A sentence that only names a passkey error is not a secret value.
    An assignment, a private key, or a one-time code is.
    """
    raw = text or ""
    if "BEGIN PRIVATE KEY" in raw or "BEGIN OPENSSH PRIVATE KEY" in raw:
        return "private_key"
    cleaned = _NAMED_PASSKEY.sub("passkey missing", raw)
    if _SECRET_ASSIGN.search(cleaned):
        return "secret_assignment"
    if _ONE_TIME.search(cleaned) and _DIGITS.search(cleaned):
        return "one_time_code"
    return ""


def decide_rebadge(
    book: dict,
    chat_id: str,
    *,
    opened_by_runner: bool,
    unread_before: bool,
    latest_from_self: bool,
    message_id: str = "",
) -> dict:
    """Say whether this chat may be marked unread again.

    The book is not changed here. A skip leaves the badge as it is.
    """
    ent = book.get(chat_id) if isinstance(book.get(chat_id), dict) else {}
    restored = [str(item) for item in (ent.get("restored_ids") or [])]
    cleared = [str(item) for item in (ent.get("cleared_ids") or [])]
    mid = str(message_id or "")[:200]
    base = {"marks_unread": False, "chat_id": chat_id}

    def skip(reason: str) -> dict:
        return {**base, "action": "skip", "reason": reason}

    if latest_from_self:
        return skip("latest_from_self")
    if unread_before is not True:
        return skip("not_unread")
    if not opened_by_runner:
        return skip("not_this_open")
    if mid and mid in cleared:
        return skip("operator_read")
    if mid and (mid in restored or mid == str(ent.get("message_id") or "")):
        return skip("already_restored")
    if not mid and ent.get("operator_cleared"):
        return skip("operator_read")
    if not mid and int(ent.get("restored_n") or 0) >= 1:
        return skip("already_restored")
    return {**base, "action": "restore", "reason": "", "marks_unread": True}


def note_restored(book: dict, chat_id: str, message_id: str = "") -> dict:
    """Remember that this message was restored once."""
    ent = _entry(book, chat_id)
    mid = str(message_id or "")[:200]
    if mid:
        ent["restored_ids"] = _trim(list(ent["restored_ids"]) + [mid])
        ent["message_id"] = mid
    ent["restored_n"] = int(ent.get("restored_n") or 0) + 1
    return ent


def note_operator_cleared(book: dict, chat_id: str, message_id: str = "", reason: str = "dot_cleared") -> dict:
    """The operator cleared this dot. That message stays read."""
    ent = _entry(book, chat_id)
    ent["operator_cleared"] = True
    ent["cleared_reason"] = str(reason or "dot_cleared")[:80]
    mid = str(message_id or "")[:200]
    if mid:
        ent["cleared_ids"] = _trim(list(ent["cleared_ids"]) + [mid])
        ent["restored_ids"] = _trim(list(ent["restored_ids"]) + [mid])
        ent["message_id"] = mid
    return ent


def repay_owed(rows: list[dict] | None, *, opened_ids: set[str] | None, live_unread_ids: set[str] | None, book: dict) -> list[dict]:
    """The old owed list does not mark anything unread.

    A dot that is gone, and that this runner did not open, is recorded as cleared.
    """
    opened = set(opened_ids or [])
    looked = live_unread_ids is not None
    live = set(live_unread_ids or [])
    out = []
    for row in rows or []:
        chat_id = str((row or {}).get("chat_id") or "")
        mid = str((row or {}).get("message_id") or "")
        if not chat_id:
            out.append({"chat_id": "", "action": "skip", "reason": "missing_chat", "marks_unread": False})
            continue
        if (row or {}).get("latest_from_self"):
            out.append({"chat_id": chat_id, "action": "skip", "reason": "latest_from_self", "marks_unread": False})
            continue
        if looked and chat_id not in live and chat_id not in opened:
            note_operator_cleared(book, chat_id, mid, "dot_cleared_without_open")
            out.append({"chat_id": chat_id, "action": "skip", "reason": "operator_read", "marks_unread": False})
            continue
        out.append({"chat_id": chat_id, "action": "skip", "reason": "not_this_open", "marks_unread": False})
    return out


def after_save(skipped: str = "") -> dict:
    """Saving a draft leaves the badge alone.

    A skip that left the dot off on purpose is still a successful stage.
    """
    reason = str(skipped or "")
    final = {"operator_read", "already_restored", "not_unread", "not_this_open", "latest_from_self"}
    if reason in final:
        return {"ok": True, "left_read": reason, "marks_unread": False, "error": ""}
    if not reason:
        return {"ok": True, "left_read": "", "marks_unread": False, "error": ""}
    return {"ok": False, "left_read": "", "marks_unread": False, "error": "remark_failed"}
