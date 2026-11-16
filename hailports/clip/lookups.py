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


def _greeting(reply: str) -> str:
    match = re.match(r"(?i)\s*hey\s+([A-Za-z]+)\s+--", reply or "")
    if not match:
        return ""
    return "hey " + match.group(1) + " --"


def _is_raw_id(token: str) -> bool:
    if not token or not token.isalnum() or len(token) not in (15, 18):
        return False
    if valid_sf_id(token):
        return True
    return token[:3] in _PREFIX


def _human(anchors: list[Anchor]) -> list[Anchor]:
    return [anchor for anchor in anchors if anchor.kind not in {"url", "sfid"}]


def _join(bits: list[str]) -> str:
    if not bits:
        return ""
    if len(bits) == 1:
        return bits[0]
    if len(bits) == 2:
        return bits[0] + " and " + bits[1]
    return ", ".join(bits[:-1]) + ", and " + bits[-1]


def _focus(anchors: list[Anchor]) -> list[str]:
    """Human names and numbers. A link or a raw id becomes 'the asset', never the path."""
    spoken = [anchor.exact for anchor in _human(anchors)]
    if spoken:
        return spoken
    labels: list[str] = []
    for anchor in anchors:
        if anchor.obj and anchor.kind in {"url", "sfid"}:
            label = "the " + anchor.obj.lower()
            if label not in labels:
                labels.append(label)
    return labels


def _scrub(text: str) -> str:
    """Drop raw ids, lightning paths, and 'I have not' filler. Leave a real sentence alone."""
    out = _LIGHTNING.sub(" ", text or "")
    out = _SFID.sub(lambda match: " " if _is_raw_id(match.group(1)) else match.group(0), out)
    out = " ".join(out.split())
    parts = re.split(r"(?<=[.?!])\s+", out)
    kept = [part for part in parts if part and not re.search(r"(?i)\bi have not\b", part)]
    out = " ".join(kept).strip()
    out = re.sub(r"(?i)\bi'll open\s*,\s*", "", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def _broken(text: str) -> bool:
    body = text or ""
    if re.search(r"(?i)\bi'll open\b(?:\s*[,.]|\s*$)", body):
        return True
    if re.search(r"(?i)\b(?:name|owner|account|user) is\s*(?:[.,]|$)", body):
        return True
    if re.search(r"(?i)\bid\s*[.,]", body):
        return True
    for part in re.split(r"(?<=[.?!])\s+", body):
        words = re.findall(r"[A-Za-z']+", part)
        if not words or len(words) > 6:
            continue
        if re.search(r"(?i)\b(i'll|i|is|has|have|sits|check|found|assigning|want|failed|stuck|will|can|thanks)\b", part):
            continue
        return True
    return False


def _names(text: str, anchors: list[Anchor]) -> bool:
    body = text or ""
    for anchor in _human(anchors):
        if anchor.exact not in body:
            return False
    return True


def _natural(sources: str, anchors: list[Anchor], greet: str) -> str:
    """One plain sentence in the replier's words. Names the item. Does not claim a change."""
    prefix = (greet + " ") if greet else ""
    low_s = (sources or "").lower()
    human = _human(anchors)
    serials = [anchor.exact for anchor in human if anchor.kind == "serial"]
    errors = [anchor.exact for anchor in human if anchor.kind == "error"]
    tickets = [anchor.exact for anchor in human if anchor.kind == "ticket"]
    if "permission" in low_s:
        who = "the dealer's " if "dealer" in low_s else ""
        focus = _join((tickets or serials or errors or _focus(anchors))[:4]) or "the record"
        return f"{prefix}thanks, I'll check {who}permission set assignments on {focus} and get back to you."
    if serials and errors:
        where = (", " + tickets[0]) if tickets else ""
        return f"{prefix}I'll check the {errors[0]} error on {serials[0]}{where}."
    if tickets and not serials and not errors and "dealer login" in low_s:
        return f"{prefix}thanks, I'll check the dealer login on {_join(tickets)} and get back to you."
    named = _join(serials[:1] or errors[:1] or tickets[:1] or _focus(anchors)[:1])
    if named:
        return f"{prefix}thanks, I'll check {named} and get back to you."
    return f"{prefix}thanks, I'll check that and get back to you."


def _parse_facts(facts: str) -> dict:
    text = facts or ""
    records = []
    record_re = re.compile(
        r"(?i)\b(account|asset|case|contact|user|lead|opportunity)\s+"
        r"([A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?)\s+name is\s+"
        r"(.+?)(?:\s+owner is\s+([A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?))?"
        r"(?:\s+account is\s+([A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?))?\."
    )
    for match in record_re.finditer(text):
        records.append({
            "obj": match.group(1).lower(),
            "id": match.group(2),
            "name": (match.group(3) or "").strip(),
            "owner": (match.group(4) or "").strip(),
            "account": (match.group(5) or "").strip(),
        })
    accounts = {
        match.group(1): match.group(2).strip()
        for match in re.finditer(
            r"(?i)\baccount\s+([A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?)\s+name is\s+([^.]+)\.",
            text,
        )
    }
    users = {
        match.group(2): match.group(1).strip()
        for match in re.finditer(
            r"(?i)\bactive user\s+(.+?)\s+id\s+([A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?)\.",
            text,
        )
    }
    missing = [
        match.group(1)
        for match in re.finditer(
            r"(?i)\buser\s+([A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?)\s+has no permission set assignment and no permission set group\.",
            text,
        )
    ]
    assigned = [
        (match.group(1).strip(), match.group(2).strip(), match.group(3).strip())
        for match in re.finditer(
            r"(?i)\buser\s+(.+?)\s+permission set\s+(.+?)\s+permission set group\s+([^.]+)\.",
            text,
        )
    ]
    return {
        "records": records,
        "accounts": accounts,
        "users": users,
        "missing": missing,
        "assigned": assigned,
        "missing_record": re.search(r"(?i)\bwas not returned\.", text) is not None,
    }


def _weave(sources: str, anchors: list[Anchor], facts: str, greet: str) -> str:
    """Cause and fix in one or two sentences. Only names that were actually read."""
    parsed = _parse_facts(facts)
    if not (parsed["records"] or parsed["missing"] or parsed["assigned"] or parsed["accounts"]):
        return ""
    prefix = (greet + " ") if greet else ""
    low = (sources or "").lower()
    permission = re.search(r"\b(permission|permissions|access|visible|visibility)\b", low) is not None
    primary = next((row for row in parsed["records"] if row["obj"] != "account"), None)
    if primary is None and parsed["records"]:
        primary = parsed["records"][0]
    obj = primary["obj"] if primary else ""
    item = ""
    for exact in _focus(anchors):
        if obj and exact.lower().startswith(obj):
            item = exact
            break
    if not item:
        spoken = _focus(anchors)
        item = spoken[0] if spoken else ("the " + obj if obj else "the record")
    account_name = ""
    if primary and primary.get("account"):
        account_name = parsed["accounts"].get(primary["account"], "")
    if account_name.lower() == "unnamed":
        account_name = ""
    if permission and parsed["missing"] and not parsed["missing_record"] and primary and primary.get("name"):
        who = parsed["users"].get(parsed["missing"][0], "")
        if who and who.lower() != "unnamed":
            if account_name:
                head = f"{item} sits on the {account_name} account"
            else:
                head = item
            return (
                f"{prefix}{head}, and their {who} login has no permission set group. "
                f"which group should I add?"
            )
    if permission and parsed["assigned"] and not parsed["missing"] and primary and primary.get("name"):
        who, perm, group = parsed["assigned"][0]
        if _is_raw_id(who):
            who = parsed["users"].get(who, "")
        if who and who.lower() != "unnamed":
            have = []
            if perm and perm.lower() != "none":
                have.append("the " + perm + " permission set")
            if group and group.lower() != "none":
                have.append("the " + group + " group")
            what = " and ".join(have) if have else "a permission set"
            where = f" on the {account_name} account" if account_name else ""
            return f"{prefix}found it. {item} is{where}, and {who} has {what}."
    if primary and primary.get("name") and not parsed["missing_record"] and not _is_raw_id(primary["name"]):
        where = f" on the {account_name} account" if account_name else ""
        return f"{prefix}found it. {item} is {primary['name']}{where}."
    return ""


def concrete_reply(reply: str, sources: str, facts: str = "") -> str:
    """Name the item in a normal sentence. Leave a reply that already does that."""
    anchors = find_anchors(sources)
    body = (reply or "").strip()
    if not body:
        return body
    if not anchors and not (facts or "").strip():
        return body
    generic = _GENERIC.search(body) is not None
    done = _DONE.search(body) is not None
    cleaned = _scrub(body)
    have_facts = bool((facts or "").strip())
    sharp = (
        not generic
        and not done
        and bool(cleaned)
        and _names(cleaned, anchors)
        and not _broken(cleaned)
        and not _is_raw_paste(cleaned)
        and "i have not" not in cleaned.lower()
    )
    if sharp:
        return cleaned
    greet = _greeting(body)
    if have_facts:
        woven = _weave(sources, anchors, facts, greet)
        if woven and not _is_raw_paste(woven):
            return woven
    if anchors:
        return _natural(sources, anchors, greet)
    return cleaned or body


def _is_raw_paste(text: str) -> bool:
    if "/lightning/" in (text or "").lower():
        return True
    return any(_is_raw_id(match.group(1)) for match in _SFID.finditer(text or ""))


def apply_facts(messages: list[dict], facts: str) -> list[dict]:
    """Put the read-only block at the front of the user turn. Empty facts add nothing."""
    block = (facts or "").strip()
    if not block:
        return messages
    note = (
        "Weave these facts into one or two natural sentences that give the cause and the fix. "
        "Use the human name or number, never a raw CRM id and never a /lightning/ path. "
        "Do not claim a change was already made, and do not say that nothing was changed. "
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
