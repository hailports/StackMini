"""A small queue of draft jobs. The runner does not send anything by itself."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from hailports.drafter import (
    NoSource,
    draft,
    has_pass,
    newest_unquoted,
    preserve_kept,
    scheduling_confirm,
    scheduling_question,
    stamp_pass,
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """create table if not exists jobs (
            id integer primary key,
            ask text not null,
            sources text not null,
            status text not null,
            result text,
            created real not null,
            message_id text,
            subject text,
            sender text,
            skip_reason text,
            reopen_n integer,
            gate_reopen_n integer,
            discarded_at real,
            error text
        )"""
    )
    _ensure(con)
    return con


_EXTRA = (
    ("message_id", "text"),
    ("subject", "text"),
    ("sender", "text"),
    ("skip_reason", "text"),
    ("reopen_n", "integer"),
    ("gate_reopen_n", "integer"),
    ("discarded_at", "real"),
    ("error", "text"),
)


def _ensure(con: sqlite3.Connection) -> None:
    have = {row[1] for row in con.execute("pragma table_info(jobs)")}
    for name, kind in _EXTRA:
        if name not in have:
            con.execute(f"alter table jobs add column {name} {kind}")
    con.commit()


def normalize_mid(message_id: str) -> str:
    """One id per note. Capital letters and angle brackets do not make a second copy."""
    return str(message_id or "").strip().strip("<>").lower()


def skip_line(reason: str, subject: str = "", sender: str = "", error: str = "") -> str:
    """One skip, with the reason and the subject or sender. The body is not logged."""
    why = " ".join(str(reason or "skip").split())[:80]
    subj = " ".join(str(subject or "").split())[:70]
    who = " ".join(str(sender or "").split())[:60]
    err = " ".join(str(error or "").split())[:80]
    detail = f" detail={err}" if err and err != why else ""
    frm = f" from={who}" if who else ""
    return f"draft_skip reason={why}{detail} subj={subj}{frm}"


def hold_reopen_once(prev: dict | None) -> bool:
    """A parked note with no saved draft gets one more try.

    A real delete stays parked. A second miss stays parked.
    """
    prev = prev or {}
    if prev.get("draft_id") or prev.get("discarded_at") or prev.get("draft_proof"):
        return False
    if prev.get("draft_ok") and str(prev.get("body_preview") or "").strip():
        return False
    if int(prev.get("hold_reopen_n") or 0) >= 1:
        return False
    err = str(prev.get("error") or "")
    if prev.get("discard_parked") or err.startswith("need_citation") or "needs_input" in err:
        return True
    return False


def stamp_reopen_once(prev: dict | None) -> bool:
    """A missing pass stamp gets one try of its own. A saved draft stays put."""
    prev = prev or {}
    if prev.get("draft_id") or prev.get("discarded_at") or prev.get("draft_proof"):
        return False
    if prev.get("draft_ok") and str(prev.get("body_preview") or "").strip():
        return False
    if int(prev.get("gate_reopen_n") or 0) >= 1:
        return False
    return "no_pass" in str(prev.get("error") or "")


def _park(prev: dict, error: str, reopened: bool) -> None:
    prev["discard_parked"] = True
    prev["refuse_n"] = int(prev.get("refuse_n") or 0) + 1
    prev["error"] = error
    prev["draft_ok"] = False
    # A retry that misses does not get a second retry on the next round.
    if reopened:
        prev["hold_reopen_n"] = max(int(prev.get("hold_reopen_n") or 0), 1)
        prev["gate_reopen_n"] = max(int(prev.get("gate_reopen_n") or 0), 1)


def _remember(prior: dict, mid: str, prev: dict) -> None:
    if mid:
        prior[mid] = prev


def run_round(rows: list[dict], *, router=None, calendar=None, prior: dict | None = None, cleaner=None, now=None) -> dict:
    """One pass over the inbox. Nothing in this pass is sent.

    The same message id is handled once. A later copy is a duplicate skip.
    """
    prior = dict(prior or {})
    seen: set[str] = set()
    skipped: list[dict] = []
    lines: list[str] = []
    drafts: list[dict] = []

    def emit(reason: str, subject: str, sender: str, error: str = "") -> None:
        lines.append(skip_line(reason, subject, sender, error))
        item = {"subject": subject, "from": sender, "skip": reason}
        if error:
            item["error"] = error
        skipped.append(item)

    for row in rows or []:
        mid = normalize_mid(row.get("message_id") or row.get("id") or "")
        subject = str(row.get("subject") or "")
        sender = str(row.get("from") or row.get("sender") or "")
        body = str(row.get("body") or "")
        if mid and mid in seen:
            emit("duplicate", subject, sender)
            continue
        if mid:
            seen.add(mid)
        prev = dict(prior.get(mid) or {})
        if prev.get("discarded_at"):
            _remember(prior, mid, prev)
            emit("discarded", subject, sender)
            continue
        hold_again = hold_reopen_once(prev)
        gate_again = (not hold_again) and stamp_reopen_once(prev)
        reopened = hold_again or gate_again
        if hold_again:
            prev["hold_reopen_n"] = 1
        if gate_again:
            prev["gate_reopen_n"] = 1
        spent = int(prev.get("hold_reopen_n") or 0) >= 1 or int(prev.get("gate_reopen_n") or 0) >= 1
        if prev.get("discard_parked") and not reopened and (int(prev.get("refuse_n") or 0) >= 2 or spent):
            _remember(prior, mid, prev)
            emit("discard_parked", subject, sender, str(prev.get("error") or "")[:80])
            continue
        reply = scheduling_confirm(body, sender, calendar, now=now)
        if reply:
            if cleaner is not None:
                reply = preserve_kept(reply, cleaner(reply))
            reply = stamp_pass(reply)
            prev["draft_ok"] = True
            prev["body_preview"] = reply[:80]
            prev["discard_parked"] = False
            prev["error"] = ""
            _remember(prior, mid, prev)
            drafts.append({
                "message_id": mid,
                "subject": subject,
                "from": sender,
                "text": reply,
                "sent": False,
                "via": "sched_confirm",
            })
            continue
        newest = newest_unquoted(body)
        if scheduling_question(newest):
            _park(prev, "needs_input", reopened)
            _remember(prior, mid, prev)
            emit("scheduling", subject, sender)
            continue
        sources = list(row.get("sources") or [])
        ask = str(row.get("ask") or subject or newest)
        try:
            if router is None:
                text = draft(ask, sources)
                held = False
            else:
                from hailports.features import draft_reply
                got = draft_reply(router, ask, sources)
                held = bool(got.raw.get("held"))
                text = got.text or ""
        except NoSource:
            _park(prev, "no_source", reopened)
            _remember(prior, mid, prev)
            emit("no_source", subject, sender)
            continue
        if held:
            _park(prev, "need_citation", reopened)
            _remember(prior, mid, prev)
            emit("need_citation", subject, sender)
            continue
        if row.get("require_pass") and not has_pass(text):
            text = stamp_pass(text)
        prev["draft_ok"] = True
        prev["body_preview"] = (text or "")[:80]
        prev["discard_parked"] = False
        prev["error"] = ""
        _remember(prior, mid, prev)
        drafts.append({
            "message_id": mid,
            "subject": subject,
            "from": sender,
            "text": text,
            "sent": False,
            "via": "draft",
        })
    return {"drafted": len(drafts), "drafts": drafts, "skipped": skipped, "log": lines, "prior": prior}


def enqueue(con: sqlite3.Connection, ask: str, sources: list[str]) -> int:
    cur = con.execute(
        "insert into jobs (ask, sources, status, created) values (?, ?, 'queued', ?)",
        (ask, "\n---\n".join(sources), time.time()),
    )
    con.commit()
    return int(cur.lastrowid)


def run_next(con: sqlite3.Connection, router=None) -> dict | None:
    row = con.execute(
        "select id, ask, sources from jobs where status = 'queued' order by id limit 1"
    ).fetchone()
    if not row:
        return None
    job_id, ask, blob = row
    sources = [p for p in blob.split("\n---\n") if p.strip()]
    try:
        if router is None:
            text = draft(ask, sources)
            status = "done"
        else:
            from hailports.features import draft_reply
            reply = draft_reply(router, ask, sources)
            if reply.raw.get("held"):
                text = ""
                status = "held"
            else:
                text = reply.render()
                status = "done"
    except NoSource as exc:
        text = str(exc)
        status = "error"
    con.execute("update jobs set status = ?, result = ? where id = ?", (status, text, job_id))
    con.commit()
    return {"id": job_id, "status": status, "result": text}


def pending(con: sqlite3.Connection) -> int:
    return int(con.execute("select count(*) from jobs where status = 'queued'").fetchone()[0])
