"""Block a draft that says a record change already happened without proof.

A prepare-only request may say the work is still pending. It may not say
the change was made. Nothing here sends mail or writes a record.
"""

from __future__ import annotations

import re
import threading

_NEG = re.compile(
    r"(?i)\b(?:not|never|didn.?t|did not|haven.?t|have not|hasn.?t|has not|won.?t|will not|cannot|can.?t)\b"
)
_CLAIM = re.compile(
    r"(?i)(?:"
    r"\bi(?:'ve| have)? (?:also )?(?:just )?(?:added|enabled|activated|turned on|created|updated|changed|completed|finished)\b"
    r"|"
    r"\bi(?:'ve| have)? (?:also )?(?:just )?(?:ran|run)\b"
    r"|"
    r"\bi did (?:also )?(?:just )?(?:add|enable|run|activate|update|change|complete|finish|create)\b"
    r"|"
    r"\bdone in (?:the )?(?:record system|tracker)\b"
    r"|"
    r"\b(?:the )?compliance check (?:was |has been |is )?(?:run|ran|done|complete|completed)\b"
    r"|"
    r"\baccess (?:was |has been |is now )?(?:added|enabled|turned on)\b"
    r")"
)
_FALSE_CONFIRM = re.compile(
    r"(?i)(?:does not confirm|doesn.t confirm|did not confirm|failed to confirm|"
    r"should confirm|must confirm|needs to confirm|not confirm)"
)
_FALSE_WORK = re.compile(
    r"(?i)\b(?:compliance|access|added|enabled|tracker|record|ran|run|done|created|updated|changed)\b"
)
_PENDING = (
    "I have not changed the records. "
    "This is still pending and waiting on a go. "
    "The account number is still needed before anything is turned on."
)
_lap = threading.local()


def set_lap(work=None, proof=None) -> None:
    _lap.work = work if isinstance(work, dict) else None
    _lap.proof = proof if isinstance(proof, dict) else None


def clear_lap() -> None:
    _lap.work = None
    _lap.proof = None


def lap_work():
    return getattr(_lap, "work", None)


def lap_proof():
    return getattr(_lap, "proof", None)


def _sentences(text: str) -> list:
    parts = re.split(r"[\n.!?]+", text or "")
    return [part.strip() for part in parts if part.strip()]


def completion_claims(text: str) -> list:
    """Sentences that say a change already happened. A denial is not a claim."""
    found = []
    for sent in _sentences(text):
        if not _CLAIM.search(sent):
            continue
        if _NEG.search(sent):
            continue
        if sent not in found:
            found.append(sent)
    return found


def _ids(proof: dict) -> bool:
    for key in ("record_ids", "ids", "record_id"):
        val = proof.get(key)
        if isinstance(val, (list, tuple)) and any(str(item).strip() for item in val):
            return True
        if isinstance(val, str) and val.strip():
            return True
    for key in ("work_item_id", "item_id"):
        val = proof.get(key)
        if isinstance(val, int) and val > 0:
            return True
        if isinstance(val, str) and val.strip().isdigit() and int(val) > 0:
            return True
    receipt = proof.get("receipt")
    if isinstance(receipt, dict) and (receipt.get("verify_result") or receipt.get("record_ids")):
        return True
    return False


def _history_proves(rows) -> bool:
    """A field change proves a write. A create-only row does not."""
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or row.get("Action") or "").lower()
        if action in {"create", "created"}:
            continue
        field = str(row.get("field") or row.get("Field") or "")
        if not field:
            continue
        old = row.get("old", row.get("OldValue"))
        new = row.get("new", row.get("NewValue"))
        if old != new:
            return True
    return False


def _flag_proves(proof: dict) -> bool:
    for key in ("access_readback", "access_flag"):
        if key not in proof:
            continue
        val = proof.get(key)
        if val is True:
            return True
        if isinstance(val, str) and val.strip().lower() in {"yes", "true", "on"}:
            return True
    return False


def write_proven(proof) -> bool:
    """True only when this lap has a verified write. A request is not proof."""
    if not isinstance(proof, dict):
        return False
    status = str(proof.get("status") or "")
    if status in {"executed_verified", "done_verified"} and (proof.get("receipt") or _ids(proof)):
        return True
    if proof.get("executed") is True and _ids(proof):
        return True
    if _history_proves(proof.get("history_changes")):
        return True
    if _flag_proves(proof):
        return True
    result = proof.get("write_result")
    if isinstance(result, dict) and result is not proof and write_proven(result):
        return True
    return False


def prepare_only(work) -> bool:
    if not isinstance(work, dict):
        return False
    if str(work.get("route") or "") == "prepare_only":
        return True
    if str(work.get("status") or "") == "packaged_not_executed":
        return True
    if work.get("record_write") is False and work.get("needs_go") and work.get("executed") is not True:
        return True
    return False


def needs_pending(work) -> bool:
    if write_proven(work):
        return False
    return prepare_only(work)


def demands_false_confirmation(reason: str) -> bool:
    """The grader wanted the draft to claim work that was not shown as done."""
    text = reason or ""
    return bool(_FALSE_CONFIRM.search(text) and _FALSE_WORK.search(text))


def _norm_words(text: str) -> set:
    cleaned = re.sub(r"(?i)\b(a|an|the|also|just|have|has|been|was|were)\b", " ", text or "")
    return set(re.findall(r"[a-z]{4,}", cleaned.lower())) - {
        "have", "also", "just", "been", "with", "that", "this", "from", "they", "them", "your",
    }


def _backed_by_context(sentence: str, context: str) -> bool:
    """True when the context already says this same change was completed."""
    others = completion_claims(context or "")
    if not others:
        return False
    want = _norm_words(sentence)
    if not want:
        return False
    for other in others:
        got = _norm_words(other)
        if len(want & got) >= 2 or (len(want) == 1 and want <= got):
            return True
    return False


def allowed(text: str, context: str = "", proof=None, work=None) -> bool:
    claims = completion_claims(text)
    if not claims:
        return True
    if write_proven(proof) or (work is not proof and write_proven(work)):
        return True
    if needs_pending(work):
        return False
    return all(_backed_by_context(sent, context or "") for sent in claims)


def pending_voice(work=None) -> str:
    if isinstance(work, dict):
        drafted = str(work.get("draft") or "").strip()
        if drafted and not completion_claims(drafted):
            return drafted
    return _PENDING


def settle(text: str, *, context: str = "", proof=None, work=None) -> dict:
    """keep, replace with the pending voice, or block."""
    raw = (text or "").strip()
    if allowed(raw, context=context, proof=proof, work=work):
        return {"action": "keep", "text": raw, "reason": ""}
    if needs_pending(work):
        pending = pending_voice(work)
        if pending and allowed(pending, context=context, proof=proof, work=work):
            return {"action": "replace", "text": pending, "reason": "prepare_only"}
    return {"action": "block", "text": "", "reason": "unproven_work"}


def outgoing(text: str, work=None, proof=None, context: str = "") -> tuple:
    """Body that may be staged, and a hold reason when it may not."""
    if work is None:
        work = lap_work()
    if proof is None:
        proof = lap_proof()
    decided = settle(text or "", context=context, proof=proof, work=work)
    if decided["action"] == "block":
        return "", "hold:unproven_work"
    return decided["text"], ""


def stage_block(text: str, proof=None, work=None, context: str = "") -> str:
    """Empty when this exact body may be written. A hold reason otherwise."""
    cleaned, hold = outgoing(text, work=work, proof=proof, context=context)
    if hold:
        return hold
    if (cleaned or "").strip() != (text or "").strip():
        return "hold:unproven_work"
    return ""
