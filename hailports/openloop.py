"""One open card at a time, then the next, and the lap starts again.

An unchanged card is skipped. A full unchanged lap returns so other idle
work can run. The next call starts the following lap. Related notes are
read only. A prepared change waits for a person to say go.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

CLOSED = ("resolved", "closed", "removed", "done")
MAIL_CAP = 10
SUGGEST_EVERY = 3

HARD = re.compile(
    r"(?i)(legal|finance|lawyer|attorney|accountant|\bcpa\b|bank|"
    r"password|passcode|seed phrase|verification code|\b2fa\b|\bmfa\b|\botp\b)"
)
HARD_PATH = re.compile(r"(?i)(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)")
_STATE_LINE = re.compile(r"(?m)^State:\s*(.+)$")
_ASSIGNED = re.compile(r"(?m)^Assigned to:\s*(.+)$")
_CHANGED = re.compile(r"(?m)^Changed:\s*(.+)$")
_PARENT = re.compile(r"(?m)^Parent:\s*(?:card-)?(\d{3,8})\s*$", re.I)
_REC = re.compile(r"\brec-(\d{3,8})\b", re.I)
_SKIP_MENTION = re.compile(r"(?i)^(created by|last changed by):")


def hard_blocked(text: str, url: str = "") -> bool:
    blob = "{0}\n{1}".format(url or "", text or "")
    if HARD_PATH.search(blob):
        return True
    return bool(HARD.search(blob))


def base_state(body: str) -> str:
    match = _STATE_LINE.search(body or "")
    raw = match.group(1).strip() if match else ""
    return raw.split("(", 1)[0].strip()


def is_closed(state: str) -> bool:
    raw = (state or "").split("(", 1)[0].strip().lower()
    return raw in CLOSED or base_state(state).lower() in CLOSED


def assigned_name(body: str) -> str:
    match = _ASSIGNED.search(body or "")
    return match.group(1).strip() if match else ""


def is_operator(text: str, tokens: tuple) -> bool:
    blob = (text or "").casefold()
    return any(tok and tok.casefold() in blob for tok in tokens)


def changed_stamp(body: str) -> str:
    match = _CHANGED.search(body or "")
    return (match.group(1).strip() if match else "")[:40]


def _mention_line(body: str, tokens: tuple) -> bool:
    for line in (body or "").splitlines():
        if _SKIP_MENTION.match(line):
            continue
        if is_operator(line, tokens):
            return True
    return False


def select_open(rows: list, tokens: tuple) -> list:
    """Keep cards assigned to the operator, mentioned, or active work they last changed.

    A created-by line by itself does not count as a mention.
    """
    out = []
    seen = set()
    for row in rows or []:
        body = str(row.get("body") or "")
        state = str(row.get("state") or base_state(body))
        if is_closed(state) or hard_blocked(body, str(row.get("url") or "")):
            continue
        who = assigned_name(body) or str(row.get("assigned") or "")
        active = state.lower().startswith("active") or state.lower().startswith("testing")
        last = bool(re.search(r"(?im)^Last changed by:.*(" + "|".join(re.escape(t) for t in tokens if t) + ")", body)) if tokens else False
        keep = is_operator(who, tokens) or _mention_line(body, tokens) or (active and last)
        if not keep:
            continue
        wid = str(row.get("id") or "")
        if not wid.isdigit() or wid in seen:
            continue
        seen.add(wid)
        out.append({
            "id": wid,
            "title": str(row.get("title") or "")[:180],
            "state": state,
            "body": body[:4000],
            "url": str(row.get("url") or ""),
            "changed": str(row.get("changed") or changed_stamp(body)),
        })
    out.sort(key=lambda item: item["id"])
    return out


def fingerprint(parts: dict) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]


def item_fingerprint(item: dict, hits: list, comment_fp: str = "") -> str:
    parts = {
        "id": item.get("id"),
        "state": item.get("state") or "",
        "changed": item.get("changed") or "",
        "title": item.get("title") or "",
        "comment": comment_fp or "",
        "hits": sorted((h.get("source"), h.get("ref")) for h in hits or []),
    }
    return fingerprint(parts)


def step_cursor(state: dict, order: list, fp_of) -> dict:
    """Walk until one changed card, or finish the lap."""
    state = dict(state or {})
    order = [str(x) for x in (order or [])]
    if not order:
        state["cursor"] = 0
        return {"action": "empty", "state": state, "skips": 0}
    start = int(state.get("cursor") or 0) % len(order)
    fps = dict(state.get("fp") or {})
    skips = 0
    for offset in range(len(order)):
        idx = (start + offset) % len(order)
        wid = order[idx]
        current = str(fp_of(wid) or "")
        if fps.get(wid) == current and current:
            skips += 1
            continue
        state["cursor"] = idx
        state["pending"] = wid
        state["fp"] = fps
        return {"action": "work", "id": wid, "fp": current, "state": state, "skips": skips}
    state["cursor"] = 0
    state["pending"] = ""
    state["fp"] = fps
    state["laps"] = int(state.get("laps") or 0) + 1
    return {"action": "lap", "state": state, "skips": skips}


def remember_fp(state: dict, wid: str, fp: str) -> dict:
    state = dict(state or {})
    fps = dict(state.get("fp") or {})
    fps[str(wid)] = str(fp)
    state["fp"] = fps
    order_len = int(state.get("n") or 0)
    if order_len:
        state["cursor"] = (int(state.get("cursor") or 0) + 1) % order_len
    state["pending"] = ""
    state["served"] = int(state.get("served") or 0) + 1
    return state


def material(prev: dict | None, new: dict) -> bool:
    if not prev:
        return False
    old_refs = {str(x.get("ref")) for x in (prev.get("found") or [])}
    new_refs = {str(x.get("ref")) for x in (new.get("found") or [])}
    if new_refs - old_refs:
        return True
    if str(prev.get("revision") or "") != str(new.get("revision") or ""):
        return True
    if str(prev.get("state") or "") != str(new.get("state") or ""):
        return True
    return False


def mail_choice(prev: dict | None, artifact: dict, sent_today: int, cap: int = MAIL_CAP) -> str:
    """First packet is a baseline and is not mailed.

    A later lap mails once per revision when something material showed up.
    The same revision only updates the packet.
    """
    if not prev:
        return "baseline"
    revision = str(artifact.get("revision") or "")
    if revision and revision == str(prev.get("emailed_rev") or ""):
        return "update"
    if not material(prev, artifact):
        return "update"
    if sent_today >= cap:
        return "cap"
    return "email"


def after_unchanged_lap(suggestion_pending: bool) -> str:
    """When every open card is unchanged, suggest once if one is waiting, else other work."""
    return "suggest" if suggestion_pending else "other"


def suggest_due(served: int, pending: bool, every: int = SUGGEST_EVERY) -> bool:
    if not pending or served <= 0:
        return False
    return served % every == 0


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def blocking_line(body: str, hits: list) -> str:
    for line in (body or "").splitlines():
        if re.search(r"(?i)\b(block|waiting|cannot|can't|need|unblocked)\b", line):
            cleaned = _clip(line, 180)
            if cleaned and not hard_blocked(cleaned):
                return cleaned
    state = base_state(body)
    if hits:
        return "Open ({0}). A related note is in {1}.".format(state or "open", hits[0].get("source") or "the index")
    return "Open ({0}). No newer note in the index on this lap.".format(state or "open")


def next_action(body: str, hits: list) -> str:
    if hits:
        hit = hits[0]
        return "Use {0} {1} on the open question, then update this item.".format(
            hit.get("source") or "the note", hit.get("ref") or ""
        )
    parent = _PARENT.search(body or "")
    if parent:
        return "Check parent {0} for the decision this item is waiting on.".format(parent.group(1))
    return "The description is unchanged. The next action is the open question already written on the item."


def prepared_change(hits: list, action: str) -> dict:
    if any(h.get("source") == "pull" for h in hits):
        kind = "pr_plan"
    elif any(h.get("source") == "record" for h in hits):
        kind = "record_prepare"
    elif any(h.get("source") in ("mail", "chat") for h in hits):
        kind = "reply"
    else:
        kind = "note"
    return {"kind": kind, "needs_go": True, "text": _clip(action, 240)}


def build_artifact(wid: str, body: str, hits: list, revision: str, fp: str, note: str = "") -> dict:
    action = next_action(body, hits)
    return {
        "id": str(wid),
        "revision": revision,
        "state": base_state(body),
        "blocking": blocking_line(body, hits),
        "found": [
            {"source": h.get("source"), "ref": h.get("ref"), "note": _clip(h.get("note") or "", 160)}
            for h in (hits or [])[:6]
        ],
        "next_action": action,
        "prepared": prepared_change(hits or [], action),
        "fp": fp,
        "note": _clip(note, 400),
        "at": int(time.time()),
    }


def related_hits(item: dict, pages: list, allowed_repos: list | None = None) -> list:
    """Read-only related notes. Hard-line rows are dropped."""
    wid = str(item.get("id") or "")
    body = str(item.get("body") or "")
    allowed = [r for r in (allowed_repos or []) if r]
    hits = []
    seen = set()

    def add(source: str, ref: str, note: str, url: str = "") -> None:
        key = source + "|" + str(ref)
        if key in seen or not ref or hard_blocked(note, url):
            return
        seen.add(key)
        hits.append({"source": source, "ref": str(ref)[:80], "note": _clip(note, 180)})

    needle = "card-" + wid
    for page in pages or []:
        source = str(page.get("source") or "")
        ref = str(page.get("ref") or "")
        url = str(page.get("url") or "")
        title = str(page.get("title") or "")
        text = str(page.get("body") or "")
        blob = "\n".join((url, title, text, " ".join(page.get("ties") or [])))
        if hard_blocked(text, url):
            continue
        mentioned = wid in blob or needle in blob
        if source == "pull":
            if allowed and not any(repo in url for repo in allowed):
                continue
            if not mentioned:
                continue
            add("pull", ref or url, title, url)
        elif source == "tracker" and ref not in (needle, "card-" + wid, "comments-" + wid):
            if mentioned:
                add("tracker", ref, title, url)
        elif source == "record":
            if any(("rec-" + num) in body for num in _REC.findall(body)[:3]) and (
                any(("rec-" + num) in blob for num in _REC.findall(body)[:3])
            ):
                add("record", ref, title, url)
        elif source in ("mail", "chat", "index", "runbook") and mentioned:
            add(source if source != "index" else "index", ref, title, url)
        elif source == "comment" and ref == "comments-" + wid:
            add("comment", ref, title or text, url)
    parent = _PARENT.search(body)
    if parent:
        add("tracker", "card-" + parent.group(1), "parent " + parent.group(1))
    return hits[:8]


def save_packet(directory: Path, prev: dict | None, artifact: dict, sent_today: int, cap: int = MAIL_CAP) -> tuple:
    choice = mail_choice(prev, artifact, sent_today, cap)
    stored = dict(artifact)
    stored["mail"] = choice
    if choice == "email":
        stored["emailed_rev"] = str(artifact.get("revision") or "")
    elif prev and prev.get("emailed_rev"):
        stored["emailed_rev"] = prev.get("emailed_rev")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    wid = str(artifact.get("id") or "")
    if not wid.isdigit():
        raise ValueError("id")
    path = directory / (wid + ".json")
    path.write_text(json.dumps(stored, indent=2, default=str) + "\n")
    return choice, stored
