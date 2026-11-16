"""Read-only record checks before a draft. Off unless the user turns them on.

A provider may run a SELECT or a record read. It must not insert, update,
delete, deploy, or assign. A miss, a timeout, or any error returns no text,
and the draft stays the same as with lookups off.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path


FACTS_LABEL = "Facts I looked up (read-only):"
CAP = 2000
TIMEOUT_S = 8.0

_ON = {"on", "true", "yes", "1"}
_PREFIX = {
    "001": "Account",
    "003": "Contact",
    "005": "User",
    "006": "Opportunity",
    "00D": "Organization",
    "00G": "Group",
    "00Q": "Lead",
    "00T": "Task",
    "00U": "Event",
    "00e": "Profile",
    "02i": "Asset",
    "0PG": "PermissionSetGroup",
    "0PS": "PermissionSet",
    "0Pa": "PermissionSetAssignment",
    "500": "Case",
    "701": "Campaign",
    "800": "Contract",
}
_LIGHTNING = re.compile(
    r"(?i)((?:https?://[^\s\"'<>]+)?/lightning/r/([A-Za-z0-9_]+)/([A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?)(?:/view)?)"
)
_SFID = re.compile(r"\b([A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?)\b")
_LABELED = re.compile(
    r"(?i)\b((?:ticket|case|asset|incident|issue|sr)\s*[:#]?\s*(?:[A-Z][A-Z0-9]+-\d+|\d{5,12}))\b"
)
_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{2,8})\b")
_SERIAL = re.compile(
    r"(?i)\b((?:serial(?:\s*number)?|s/n|sn)\s*[:#]?\s*[A-Z0-9][A-Z0-9-]{4,})\b"
)
_ERROR = re.compile(r"(?i)\b(?:error|exception|fault)\s*[:#-]?\s*([A-Z][A-Z0-9_]{3,80})\b")
_ERROR_QUOTED = re.compile(r"(?i)\b(?:error|exception|fault)\s*[:#-]?\s*\"([^\"]{4,120})\"")
_GENERIC = re.compile(
    r"(?i)\b(?:i'll|i will|let me|i'm going to|im going to)\s+(?:just\s+)?take a look\b"
    r"|\b(?:i'll|i will)\s+(?:just\s+)?look into it\b"
    r"|\b(?:i'll|i will)\s+check it out\b"
)
_DONE = re.compile(
    r"(?i)\b(?:i|we)\s+(?:already\s+)?(?:have\s+)?(?:updated|assigned|added|granted|fixed|deployed|removed|changed)\b"
)
_WRITE = re.compile(
    r"(?i)\b(insert|update|delete|upsert|merge|undelete|create|drop|alter|truncate)\b|;|--|/\*"
)
_ALLOWED = (
    ("org", "display"),
    ("data", "get", "record"),
    ("data", "query"),
)
_BANNED_ARG = {"deploy", "delete", "create", "update", "upsert", "apex", "project", "assign"}


class Anchor:
    def __init__(self, kind: str, exact: str, obj: str = "", record_id: str = ""):
        self.kind = kind
        self.exact = exact
        self.obj = obj
        self.record_id = record_id


class LookupProvider:
    """One read-only source. Jira and ADO can subclass this later."""

    name = ""

    def ready(self) -> bool:
        return False

    def facts(self, text: str, deadline: float) -> str:
        return ""


def _flag(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in _ON:
        return True
    return False


def lookups_enabled(cfg: dict | None, home: Path | None = None) -> bool:
    """True only for `lookups: on` (or the menu file the toggle writes)."""
    cfg = cfg or {}
    if "lookups" in cfg:
        return _flag(cfg.get("lookups"))
    paiste = cfg.get("paiste")
    if isinstance(paiste, dict) and "lookups" in paiste:
        return _flag(paiste.get("lookups"))
    path = (home or Path.home()) / ".config" / "paiste" / "lookups"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return _flag(raw.strip().splitlines()[0] if raw.strip() else "")


def sf_checksum(id15: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    suffix = []
    for chunk in range(3):
        flags = 0
        for bit in range(5):
            char = id15[chunk * 5 + bit]
            if "A" <= char <= "Z":
                flags += 1 << bit
        suffix.append(alphabet[flags])
    return id15 + "".join(suffix)


def valid_sf_id(value: str) -> bool:
    if not value or not value.isalnum():
        return False
    if len(value) == 18:
        return value[:3] in _PREFIX and sf_checksum(value[:15]) == value
    if len(value) == 15:
        return value[:3] in _PREFIX
    return False


def guard_select(query: str) -> str:
    """Allow one SELECT. Anything else is refused before a process starts."""
    text = " ".join((query or "").split())
    if not text or _WRITE.search(text) or not re.match(r"(?i)select\b", text):
        raise ValueError("not a select")
    return text


def find_anchors(text: str) -> list[Anchor]:
    """Record links, CRM ids, ticket ids, serials, and error text."""
    raw = text or ""
    found: list[Anchor] = []
    spans: list[tuple[int, int]] = []

    def add(kind: str, exact: str, start: int, end: int, obj: str = "", record_id: str = "") -> None:
        exact = (exact or "").strip()
        if not exact or any(exact == item.exact for item in found):
            return
        if any(not (end <= left or start >= right) for left, right in spans):
            return
        spans.append((start, end))
        found.append(Anchor(kind, exact, obj, record_id))

    for match in _LIGHTNING.finditer(raw):
        obj = match.group(2)
        record_id = match.group(3)
        if not valid_sf_id(record_id) or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", obj):
            continue
        add("url", match.group(1), match.start(1), match.end(1), obj, record_id)
    for match in _SFID.finditer(raw):
        record_id = match.group(1)
        if not valid_sf_id(record_id):
            continue
        if any(record_id in item.exact for item in found if item.kind == "url"):
            continue
        add("sfid", record_id, match.start(1), match.end(1), _PREFIX.get(record_id[:3], ""), record_id)
    for match in _LABELED.finditer(raw):
        add("ticket", match.group(1), match.start(1), match.end(1))
    for match in _KEY.finditer(raw):
        add("ticket", match.group(1), match.start(1), match.end(1))
    for match in _SERIAL.finditer(raw):
        add("serial", match.group(1), match.start(1), match.end(1))
    for match in _ERROR.finditer(raw):
        add("error", match.group(1), match.start(1), match.end(1))
    for match in _ERROR_QUOTED.finditer(raw):
        add("error", match.group(1), match.start(1), match.end(1))
    return found


def _covered(reply: str, anchors: list[Anchor]) -> bool:
    body = reply or ""
    for anchor in anchors:
        if anchor.kind == "sfid" and any(anchor.exact in item.exact for item in anchors if item.kind == "url"):
            continue
        if anchor.exact not in body:
            return False
    return True


def _greeting(reply: str) -> str:
    match = re.match(r"(?i)\s*hey\s+([A-Za-z]+)\s+--", reply or "")
    if not match:
        return ""
    return "hey " + match.group(1) + " --"


def _named(anchors: list[Anchor]) -> str:
    exacts: list[str] = []
    for anchor in anchors:
        if anchor.kind == "sfid" and any(anchor.exact in item.exact for item in anchors if item.kind == "url"):
            continue
        if anchor.exact not in exacts:
            exacts.append(anchor.exact)
    return ", ".join(exacts[:4])


def _step(sources: str, anchors: list[Anchor]) -> str:
    named = _named(anchors) or "the record"
    low = (sources or "").lower()
    if "permission" in low:
        who = "the dealer's " if "dealer" in low else ""
        return (
            f"I'll open {named}. I'll check {who}permission set assignments. "
            "I have not changed anything."
        )
    if any(anchor.kind == "error" for anchor in anchors):
        return f"I'll open {named} and read that error on the record. I have not changed anything."
    if any(anchor.kind == "serial" for anchor in anchors):
        return f"I'll open {named} and read that serial on the record. I have not changed anything."
    return f"I'll open {named} and answer what was asked about it. I have not changed anything."


def _fact_sentences(facts: str) -> str:
    lines = []
    for line in (facts or "").splitlines():
        if line.strip().lower().startswith(FACTS_LABEL.lower()):
            continue
        if line.strip():
            lines.append(line.strip())
    body = " ".join(lines)
    body = re.sub(r"\s{2,}", " ", body).strip()
    return body[:900]


def concrete_reply(reply: str, sources: str, facts: str = "") -> str:
    """Name the exact item and the next step. Never invent a finding or a finished change."""
    anchors = find_anchors(sources)
    body = (reply or "").strip()
    if not body:
        return body
    generic = _GENERIC.search(body) is not None
    done = _DONE.search(body) is not None
    have_facts = bool((facts or "").strip())
    if not anchors and not have_facts:
        return body
    if have_facts:
        if not (generic or done or (anchors and not _covered(body, anchors))):
            return body
    elif not anchors or (not generic and _covered(body, anchors) and not done):
        return body
    greet = _greeting(body)
    pieces = []
    said = _fact_sentences(facts) if have_facts else ""
    if said:
        pieces.append(said if said.endswith(".") else said + ".")
    if anchors:
        pieces.append(_step(sources, anchors))
    elif have_facts:
        pieces.append("I have not changed anything.")
    if have_facts and "no permission set assignment" in (facts or "").lower():
        pieces.append("Assigning a permission set is the fix. I have not assigned one.")
    text = " ".join(piece for piece in pieces if piece).strip()
    if greet:
        text = greet + " " + text
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def apply_facts(messages: list[dict], facts: str) -> list[dict]:
    """Put the read-only block at the front of the user turn. Empty facts add nothing."""
    block = (facts or "").strip()
    if not block:
        return messages
    note = (
        "State these facts plainly and propose a fix. "
        "Do not claim a change was already made. "
        "Do not invent a fact beyond this block.\n"
        + block
    )
    out = [dict(item) for item in messages]
    for item in out:
        if item.get("role") == "user":
            item["content"] = note + "\n\n" + str(item.get("content") or "")
            return out
    out.append({"role": "user", "content": note})
    return out


def _which_sf() -> str | None:
    return shutil.which("sf")


def _default_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class CrmProvider(LookupProvider):
    name = "crm"

    def __init__(self, run=None, which=None):
        self._run = run or _default_run
        self._which = which or _which_sf

    def ready(self) -> bool:
        return self._which() is not None

    def facts(self, text: str, deadline: float) -> str:
        try:
            return self._facts(text or "", deadline)
        except (OSError, subprocess.TimeoutExpired, TimeoutError, ValueError, json.JSONDecodeError):
            return ""

    def _left(self, deadline: float) -> float:
        left = deadline - time.monotonic()
        if left <= 0.05:
            raise TimeoutError()
        return left

    def _argv_ok(self, argv: list[str]) -> bool:
        if len(argv) < 3 or Path(argv[0]).name != "sf":
            return False
        rest = argv[1:]
        if any(part.lower() in _BANNED_ARG for part in rest):
            return False
        return any(tuple(rest[: len(prefix)]) == prefix for prefix in _ALLOWED)

    def _call(self, argv: list[str], deadline: float) -> dict | None:
        if not self._argv_ok(argv):
            raise ValueError("not a read")
        if argv[1:3] == ["data", "query"]:
            try:
                query = argv[argv.index("--query") + 1]
            except (ValueError, IndexError) as exc:
                raise ValueError("not a select") from exc
            guard_select(query)
        done = self._run(argv, self._left(deadline))
        if done.returncode != 0:
            return None
        doc = json.loads(done.stdout or "{}")
        if not isinstance(doc, dict):
            return None
        status = doc.get("status", 0)
        if status not in (0, "0", None):
            return None
        return doc

    def _rows(self, doc: dict | None) -> list[dict] | None:
        if doc is None:
            return None
        result = doc.get("result", doc)
        if isinstance(result, dict) and isinstance(result.get("records"), list):
            return [row for row in result["records"] if isinstance(row, dict)]
        if isinstance(result, dict) and (result.get("Id") or result.get("id") or result.get("alias")):
            return [result]
        if isinstance(result, list):
            return [row for row in result if isinstance(row, dict)]
        return []

    def _org_ok(self, deadline: float) -> bool:
        doc = self._call(["sf", "org", "display", "--json"], deadline)
        rows = self._rows(doc)
        if not rows:
            return False
        row = rows[0]
        return bool(row.get("id") or row.get("Id") or row.get("alias") or row.get("username"))

    def _targets(self, text: str) -> list[Anchor]:
        picked: list[Anchor] = []
        seen: set[str] = set()
        for anchor in find_anchors(text):
            if anchor.kind not in {"url", "sfid"} or not anchor.record_id or not anchor.obj:
                continue
            if anchor.record_id in seen:
                continue
            seen.add(anchor.record_id)
            picked.append(anchor)
            if len(picked) == 3:
                break
        return picked

    def _clean(self, value: object) -> str:
        if not isinstance(value, (str, int, float, bool)):
            return ""
        text = str(value).strip()
        if not text or "@" in text or len(text) > 80:
            return ""
        return text

    def _record_line(self, anchor: Anchor, row: dict) -> str:
        name = self._clean(row.get("Name"))
        owner = self._clean(row.get("OwnerId"))
        account = self._clean(row.get("AccountId"))
        bits = [anchor.obj.lower(), anchor.record_id]
        if name:
            bits.append("name is " + name)
        if owner:
            bits.append("owner is " + owner)
        if account:
            bits.append("account is " + account)
        return " ".join(bits) + "."

    def _facts(self, text: str, deadline: float) -> str:
        targets = self._targets(text)
        if not targets or not self._org_ok(deadline):
            return ""
        lines: list[str] = []
        owners: list[str] = []
        accounts: list[str] = []
        for anchor in targets:
            doc = self._call(
                [
                    "sf", "data", "get", "record",
                    "--sobject", anchor.obj,
                    "--record-id", anchor.record_id,
                    "--json",
                ],
                deadline,
            )
            rows = self._rows(doc)
            if rows is None:
                return ""
            if not rows:
                lines.append(anchor.obj.lower() + " " + anchor.record_id + " was not returned.")
                continue
            row = rows[0]
            lines.append(self._record_line(anchor, row))
            owner = self._clean(row.get("OwnerId"))
            account = self._clean(row.get("AccountId"))
            if owner and valid_sf_id(owner):
                owners.append(owner)
            if account and valid_sf_id(account):
                accounts.append(account)
        if not _asks_permission(text):
            return " ".join(lines)[:CAP]
        for account in accounts[:2]:
            doc = self._call(
                ["sf", "data", "query", "--query", guard_select(
                    "SELECT Id, Name FROM Account WHERE Id = '" + account + "'"
                ), "--json"],
                deadline,
            )
            rows = self._rows(doc)
            if rows is None:
                return ""
            if rows:
                name = self._clean(rows[0].get("Name")) or "unnamed"
                lines.append("account " + account + " name is " + name + ".")
        user_ids = []
        if owners or accounts:
            where = []
            if owners:
                where.append("Id = '" + owners[0] + "'")
            if accounts:
                where.append("Contact.AccountId = '" + accounts[0] + "'")
            query = (
                "SELECT Id, Name, IsActive FROM User WHERE IsActive = true AND ("
                + " OR ".join(where)
                + ") LIMIT 10"
            )
            doc = self._call(
                ["sf", "data", "query", "--query", guard_select(query), "--json"],
                deadline,
            )
            rows = self._rows(doc)
            if rows is None:
                return ""
            for row in rows:
                uid = self._clean(row.get("Id"))
                name = self._clean(row.get("Name")) or "unnamed"
                if uid:
                    user_ids.append(uid)
                    lines.append("active user " + name + " id " + uid + ".")
        if not user_ids and owners:
            user_ids = owners[:3]
        if user_ids:
            quoted = ", ".join("'" + uid + "'" for uid in user_ids[:10])
            query = (
                "SELECT AssigneeId, Assignee.Name, PermissionSet.Label, PermissionSetGroup.MasterLabel "
                "FROM PermissionSetAssignment WHERE AssigneeId IN (" + quoted + ") LIMIT 40"
            )
            doc = self._call(
                ["sf", "data", "query", "--query", guard_select(query), "--json"],
                deadline,
            )
            rows = self._rows(doc)
            if rows is None:
                return ""
            if not rows:
                for uid in user_ids[:10]:
                    lines.append(
                        "user " + uid + " has no permission set assignment and no permission set group."
                    )
            else:
                seen_users = set()
                for row in rows:
                    uid = self._clean(row.get("AssigneeId")) or "unknown"
                    seen_users.add(uid)
                    assignee = row.get("Assignee") if isinstance(row.get("Assignee"), dict) else {}
                    who = self._clean(assignee.get("Name")) or uid
                    perm = row.get("PermissionSet") if isinstance(row.get("PermissionSet"), dict) else {}
                    group = row.get("PermissionSetGroup") if isinstance(row.get("PermissionSetGroup"), dict) else {}
                    label = self._clean(perm.get("Label")) or "none"
                    group_label = self._clean(group.get("MasterLabel")) or "none"
                    lines.append(
                        "user " + who + " permission set " + label + " permission set group " + group_label + "."
                    )
                for uid in user_ids:
                    if uid not in seen_users:
                        lines.append(
                            "user " + uid + " has no permission set assignment and no permission set group."
                        )
        return " ".join(lines)[:CAP]


def _asks_permission(text: str) -> bool:
    return re.search(
        r"(?i)\b(permission|permissions|access|can't see|cannot see|cant see|not visible|visibility)\b",
        text or "",
    ) is not None


def providers() -> list[LookupProvider]:
    return [CrmProvider()]


def collect_facts(
    text: str,
    timeout_s: float = TIMEOUT_S,
    found: list[LookupProvider] | None = None,
) -> str:
    """One capped block, or empty when nothing read-only came back in time."""
    deadline = time.monotonic() + timeout_s
    ready = found if found is not None else [item for item in providers() if item.ready()]
    if not ready:
        return ""
    chunks: list[str] = []
    try:
        for provider in ready:
            if time.monotonic() >= deadline:
                return ""
            piece = provider.facts(text, deadline).strip()
            if piece:
                chunks.append(piece)
    except (OSError, subprocess.TimeoutExpired, TimeoutError, ValueError, json.JSONDecodeError):
        return ""
    if not chunks:
        return ""
    body = "\n".join(chunks).strip()
    if len(body) > CAP:
        cut = body[:CAP]
        body = cut.rsplit("\n", 1)[0].strip() or cut.strip()
    return FACTS_LABEL + "\n" + body


def facts_for_draft(cfg: dict | None, text: str, home: Path | None = None) -> str:
    if not lookups_enabled(cfg, home):
        return ""
    try:
        return collect_facts(text)
    except (OSError, subprocess.TimeoutExpired, TimeoutError, ValueError, json.JSONDecodeError):
        return ""
