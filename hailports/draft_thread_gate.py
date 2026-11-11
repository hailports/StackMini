"""Reply drafts target one exact source id.

A person's name, a subject fragment, or a "match:" hint is not a target.
No source id means the draft is not staged.
"""

from __future__ import annotations

import re

SKIP_NO_SOURCE_ID = "skipped_no_source_id"
SKIP_NOT_ADDRESSED = "skipped_not_addressed_to_Pat"
REJECT_GREETING = "rejected_greeting_not_participant"

_PERSON = re.compile(r"[A-Z][a-z]{1,20}")
_ASKING = re.compile(
    r"\b([A-Z][a-z]{1,30})\s+(?:is\s+|was\s+)?"
    r"(?:asking|asked|asks)\s+([A-Z][a-z]{1,30})\b"
)
_FOR_OTHER = re.compile(
    r"\bfor\s+([A-Za-z][A-Za-z'\-]{1,30})\s+to\s+"
    r"(?:send|reply|respond|answer|pass)\b",
    re.I,
)
_TO_LINE = re.compile(r"(?im)^\s*to\s*:\s*([^\n]+)")
_MSG_LABEL = re.compile(
    r"(?i)\b(?:source[\s_-]?message[\s_-]?id|message[\s_-]?id)\s*[:=]\s*"
    r"<?([^>\s,;\"']+)>?"
)
_CONV_LABEL = re.compile(
    r"(?i)\b(?:source[\s_-]?conversation[\s_-]?id|conversation[\s_-]?id)\s*[:=]\s*"
    r"<?([^>\s,;\"']+)>?"
)
_GREET = re.compile(r"(?im)^\s*(?:hey|hi|hello)\s+([A-Za-z][A-Za-z'\-]{1,30})\b")
_GREET_SKIP = {"there", "all", "team", "folks", "everyone", "guys", "both", "yall"}
_LAST_FIRST = re.compile(
    r"\b([A-Za-z][A-Za-z'\-]{1,30})\s*,\s*([A-Za-z][A-Za-z'\-]{1,30})\b"
)


def _strip_id(value) -> str:
    return str(value or "").strip().strip("<>").strip().strip("'\"")


def _is_person_token(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][a-z]{1,20}", value or ""))


def _accept_id(value) -> str:
    """An exact id. A first name or a short token is not one."""
    token = _strip_id(value)
    if not token or any(ch.isspace() for ch in token):
        return ""
    if _is_person_token(token) or len(token) < 8:
        return ""
    return token


def _zoom_host(host: str) -> bool:
    marker = "xmpp." + "zoom.us"
    host = (host or "").lower().rstrip(".")
    return host == marker or host.endswith("." + marker)


def _zoom_jid(text: str) -> str:
    for raw in re.findall(r"[^\s<>]+", text or ""):
        token = _accept_id(raw.strip(".,;"))
        if token.count("@") != 1:
            continue
        if _zoom_host(token.split("@", 1)[1]):
            return token
    return ""


def explicit_source_ids(item: dict | None) -> dict:
    """Ids the item itself carries. Nothing is inferred from a name."""
    item = item or {}
    notes = str(item.get("notes") or "")
    message_id = ""
    for key in ("source_message_id", "message_id"):
        message_id = _accept_id(item.get(key))
        if message_id:
            break
    if not message_id:
        labeled = _MSG_LABEL.search(notes)
        if labeled:
            message_id = _accept_id(labeled.group(1))
    conversation_id = ""
    for key in ("source_conversation_id", "conversation_id"):
        conversation_id = _accept_id(item.get(key))
        if conversation_id:
            break
    if not conversation_id:
        labeled = _CONV_LABEL.search(notes)
        if labeled:
            conversation_id = _accept_id(labeled.group(1))
    if not conversation_id:
        conversation_id = _zoom_jid(notes)
    return {"message_id": message_id, "conversation_id": conversation_id}


def _is_operator(text: str, operator: str) -> bool:
    if not text or not operator:
        return False
    return re.search(rf"\b{re.escape(operator)}\b", text, re.I) is not None


def _as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [part.strip() for part in re.split(r"[,;]", str(value)) if part.strip()]


def _item_text(item: dict) -> str:
    return "\n".join(str(item.get(key) or "") for key in ("title", "why", "notes"))


def addressed_to_operator(item: dict | None, operator: str = "Pat") -> tuple[bool, str]:
    """False when the item says someone other than the operator was asked.

    Unknown stays open. A direct To line, an "X asking Y" line, or a
    "for Y to send" line is enough to skip.
    """
    item = item or {}
    to = _as_list(item.get("to") or item.get("addressed_to") or item.get("ask_of"))
    cc = _as_list(item.get("cc") or item.get("cc_list"))
    text = _item_text(item)
    asked = _ASKING.search(text)
    if to:
        if any(_is_operator(entry, operator) for entry in to):
            return True, ""
        return False, "to_not_operator"
    if asked and not _is_operator(asked.group(2), operator):
        return False, "asking_other"
    draft_for = _FOR_OTHER.search(text)
    if draft_for and not _is_operator(draft_for.group(1), operator):
        return False, "draft_for_other"
    to_line = _TO_LINE.search(text)
    if to_line and not _is_operator(to_line.group(1), operator):
        return False, "to_header_other"
    if re.search(rf"\b{re.escape(operator)}\b[^.\n]{{0,40}}\bonly\s+cc", text, re.I):
        return False, "cc_only"
    if cc and any(_is_operator(entry, operator) for entry in cc):
        if not (asked and _is_operator(asked.group(2), operator)):
            return False, "cc_only"
    return True, ""


def greeting_name(draft: str) -> str:
    match = _GREET.search(draft or "")
    if not match:
        return ""
    name = match.group(1)
    if name.lower() in _GREET_SKIP:
        return ""
    return name


def participant_names(*blobs) -> set[str]:
    names: set[str] = set()
    for blob in blobs:
        if blob is None or blob == "":
            continue
        if isinstance(blob, (list, tuple, set)):
            text = " ".join(str(part) for part in blob)
        else:
            text = str(blob)
        for match in _LAST_FIRST.finditer(text):
            names.add(match.group(2).lower())
        for match in _PERSON.finditer(text):
            names.add(match.group(0).lower())
    return names


def thread_participants(item: dict | None, source: dict | None = None) -> set[str]:
    """People on the target thread. The item summary is not that list."""
    item = item or {}
    blobs = []
    for key in ("participants", "to", "cc", "from", "source_from"):
        if item.get(key):
            blobs.append(item.get(key))
    if isinstance(source, dict):
        for key in ("from", "to", "cc", "participants", "sender", "source_sender"):
            if source.get(key):
                blobs.append(source.get(key))
    return participant_names(*blobs)


def reply_draft_gate(
    item: dict | None,
    draft: str = "",
    source: dict | None = None,
    operator: str = "Pat",
) -> dict:
    """Whether a reply draft may be staged. Never sends."""
    item = item or {}
    channel = str(item.get("draft_channel") or item.get("channel") or "").lower()
    ids = explicit_source_ids(item)
    result = {
        "ok": True,
        "disposition": "",
        "reason": "",
        "channel": channel,
        "message_id": ids["message_id"],
        "conversation_id": ids["conversation_id"],
        "target": "",
        "sent": False,
    }
    if channel not in ("outlook", "zoom"):
        return result
    ok_addr, why = addressed_to_operator(item, operator)
    if not ok_addr:
        result.update(ok=False, disposition=SKIP_NOT_ADDRESSED, reason=why)
        return result
    if channel == "outlook":
        target = ids["message_id"]
    else:
        target = ids["conversation_id"] or ids["message_id"]
    if not target:
        result.update(ok=False, disposition=SKIP_NO_SOURCE_ID, reason="no_exact_id")
        return result
    result["target"] = target
    name = greeting_name(draft)
    if name and name.lower() not in thread_participants(item, source):
        result.update(ok=False, disposition=REJECT_GREETING, reason="greeting", target=target)
        return result
    return result
