"""Production access grants require an approved Service Desk Plus ticket.

Fail closed. This module never invents approval. Shared privileged passwords
are never stored, logged, or forwarded.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

_TICKET_LABELED = re.compile(
    r"(?i)(?:service\s*desk\s*plus|servicedesk\s*plus|\bsd\s*\+|\bsdp)\s*[#:\-]?\s*(\d{3,10})"
)
_TICKET_WORD = re.compile(r"(?i)\bticket\s*[#:\-]?\s*(\d{3,10})\b")
_SD_CONTEXT = re.compile(r"(?i)service\s*desk|\bsd\s*\+|\bsdp\b|servicedesk")
_PRIV_PW = re.compile(
    r"(?i)(?:"
    r"\b(?:privileged|shared)\b.{0,80}\bpassword\b"
    r"|\bpassword\b.{0,80}\b(?:privileged|shared|cyberark|passwordstate)\b"
    r"|\b(?:cyberark|passwordstate)\b"
    r"|\b(?:set|share|store|send|paste|put|save|give|copy)\b.{0,40}\b(?:the\s+)?"
    r"(?:admin|root|shared|privileged)\b.{0,40}\bpassword\b"
    r"|\bpassword\s+(?:for|of)\s+(?:the\s+)?(?:shared|privileged|admin)\b"
    r")"
)
_STATUS_OK = {"approved", "rejected", "open", "unknown"}

REFUSAL_MISSING = (
    "Access was not granted. Production access needs an approved Service Desk Plus ticket. "
    "Put the ticket id in the request (for example SD+ 12345) after Service Desk Plus shows it approved. "
    "Nothing was changed."
)
REFUSAL_PASSWORD = (
    "Shared privileged passwords stay in CyberArk or Passwordstate. "
    "Do not put the password in a Service Desk Plus ticket. Nothing was stored or sent."
)

_VERIFIER = None


def is_prod_org(org: str) -> bool:
    """True for a production alias. Sandbox, partial, and scratch names are not production."""
    raw = (org or "").strip().lower()
    if not raw:
        return False
    compact = re.sub(r"[^a-z0-9]+", "", raw)
    if any(part in raw for part in ("partial", "sand", "scratch", "uat", "qa")):
        return False
    if "nonprod" in compact or "dev" in raw:
        return False
    if compact in {"prod", "production", "operator"}:
        return True
    return compact.endswith("prod")


def parse_ticket_id(text: str) -> str:
    blob = text or ""
    labeled = _TICKET_LABELED.search(blob)
    if labeled:
        return labeled.group(1)
    if _SD_CONTEXT.search(blob):
        word = _TICKET_WORD.search(blob)
        if word:
            return word.group(1)
    return ""


def privileged_password_request(text: str) -> bool:
    return bool(_PRIV_PW.search(text or ""))


def set_verifier(fn) -> None:
    """Test hook. Production code must not call this."""
    global _VERIFIER
    _VERIFIER = fn


def clear_verifier() -> None:
    global _VERIFIER
    _VERIFIER = None


def _status_from_payload(data) -> str:
    if not isinstance(data, dict):
        return "unknown"
    if data.get("approved") is True:
        return "approved"
    if data.get("approved") is False:
        return "rejected"
    approval = str(data.get("approval_status") or data.get("approval") or "").strip().lower()
    if approval in {"approved"}:
        return "approved"
    if approval in {"rejected", "denied"}:
        return "rejected"
    if approval in {"pending", "requested", "open"}:
        return "open"
    status = str(data.get("status") or data.get("request_status") or "").strip().lower()
    if status == "approved":
        return "approved"
    if status in {"rejected", "denied", "cancelled", "canceled"}:
        return "rejected"
    if status in {"open", "requested", "pending", "in progress", "in_progress"}:
        return "open"
    return "unknown"


def verify_ticket(ticket_id: str) -> str:
    """Return approved, rejected, open, or unknown. Unknown blocks a production grant."""
    if not ticket_id or not str(ticket_id).isdigit():
        return "unknown"
    if _VERIFIER is not None:
        try:
            status = str(_VERIFIER(ticket_id) or "unknown").strip().lower()
        except Exception:
            return "unknown"
        return status if status in _STATUS_OK else "unknown"
    base = (os.environ.get("SDPLUS_API_BASE") or "").strip().rstrip("/")
    if not base.startswith("https://"):
        return "unknown"
    if not re.fullmatch(r"https://[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._~/-]*)?", base):
        return "unknown"
    url = base + "/requests/" + ticket_id
    req = urllib.request.Request(url, method="GET")
    token = (os.environ.get("SDPLUS_API_TOKEN") or "").strip()
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read(8000)
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return "unknown"
    return _status_from_payload(data)


def audit_path() -> Path:
    override = (os.environ.get("SDPLUS_AUDIT_PATH") or "").strip()
    if override:
        return Path(override)
    return Path.home() / "claude-stack" / "data" / "sdplus_access_audit.jsonl"


def record(op: str, prod: bool, ticket_id: str, decision: str) -> None:
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "op": str(op or "")[:80],
        "prod": bool(prod),
        "ticket_id": str(ticket_id or "")[:16],
        "decision": str(decision or "")[:40],
    }
    path = audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except Exception:
        return


def _unapproved_message(ticket_id: str, status: str) -> str:
    if status == "unknown":
        return (
            "Access was not granted. Service Desk Plus ticket %s could not be confirmed approved. "
            "Get the ticket approved in Service Desk Plus first. Nothing was changed."
        ) % ticket_id
    return (
        "Access was not granted. Service Desk Plus ticket %s is not confirmed approved. "
        "Get the ticket approved in Service Desk Plus first. Nothing was changed."
    ) % ticket_id


def decide(text: str, *, prod: bool, op: str) -> dict:
    """Block is true when the operation must not run."""
    if privileged_password_request(text):
        record(op, prod, "", "password_refused")
        return {
            "block": True,
            "allow": False,
            "ticket_id": "",
            "decision": "password_refused",
            "message": REFUSAL_PASSWORD,
        }
    ticket = parse_ticket_id(text)
    if not prod:
        decision_name = "sandbox_noted" if ticket else "sandbox_ok"
        if ticket:
            record(op, False, ticket, decision_name)
        return {
            "block": False,
            "allow": True,
            "ticket_id": ticket,
            "decision": decision_name,
            "message": "",
        }
    if not ticket:
        record(op, True, "", "missing_ticket")
        return {
            "block": True,
            "allow": False,
            "ticket_id": "",
            "decision": "missing_ticket",
            "message": REFUSAL_MISSING,
        }
    status = verify_ticket(ticket)
    record(op, True, ticket, status)
    if status == "approved":
        return {
            "block": False,
            "allow": True,
            "ticket_id": ticket,
            "decision": "approved",
            "message": "",
        }
    return {
        "block": True,
        "allow": False,
        "ticket_id": ticket,
        "decision": status,
        "message": _unapproved_message(ticket, status),
    }
