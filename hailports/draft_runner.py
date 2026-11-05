"""A small queue of draft jobs. The runner does not send anything by itself."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from hailports.chat_badge import (
    after_save,
    decide_rebadge,
    note_restored,
    read_code,
    secret_hold,
)
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


_FINAL_DECISIONS = ("no_reply_needed", "reply", "add_only_new")


def decision_cached(prev: dict | None) -> str:
    """A stored decision for this message. Empty when the message still needs a read.

    The same message is not judged again. A new message is a new id, or a new
    content hash when the caller has one.
    """
    prev = prev or {}
    dec = str(prev.get("decision") or "")
    if dec not in _FINAL_DECISIONS:
        return ""
    if not str(prev.get("decision_hash") or ""):
        return ""
    return dec


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


_HOLD_WAIT_S = (120, 300, 600)
_HOLD_MAX = 3


def _is_grade_hold(reason: str) -> bool:
    """A grader hold. Scheduling and missing-stamp parks stay on their own path."""
    text = str(reason or "").lower()
    return "off_topic" in text or text.startswith("hold:")


def _grade_waiting(prev: dict, now_s: float) -> bool:
    err = str(prev.get("error") or "")
    if not err.startswith("hold:") or prev.get("settled") or prev.get("discard_parked"):
        return False
    if int(prev.get("grade_fails") or 0) >= _HOLD_MAX:
        return False
    return float(prev.get("next_ok") or 0) > now_s


def _apply_badge(row: dict, book: dict) -> dict | None:
    """Judge a badge only when the row says how the chat was opened."""
    if "unread_before" not in row and "opened_by_runner" not in row:
        return None
    chat_id = str(row.get("chat_id") or "")
    decision = decide_rebadge(
        book,
        chat_id,
        opened_by_runner=bool(row.get("opened_by_runner")),
        unread_before=row.get("unread_before") is True,
        latest_from_self=bool(row.get("latest_from_self")),
        message_id=str(row.get("source_message_id") or ""),
    )
    if decision["action"] == "restore":
        note_restored(book, chat_id, str(row.get("source_message_id") or ""))
    return decision


def _with_badge(item: dict, decision: dict | None) -> dict:
    """A saved draft does not itself mark the chat unread."""
    item["marks_unread"] = False
    if not decision:
        return item
    item["badge_action"] = decision["action"]
    item["badge_reason"] = decision["reason"]
    saved = after_save(decision["reason"] if decision["action"] == "skip" else "")
    item["left_read"] = saved["left_read"]
    if saved["error"]:
        item["error"] = saved["error"]
    return item


def run_round(rows: list[dict], *, router=None, calendar=None, prior: dict | None = None, cleaner=None, now=None, badges: dict | None = None) -> dict:
    """One pass over the inbox. Nothing in this pass is sent.

    The same message id is handled once. A later copy is a duplicate skip.
    A chat that cannot be read while closed is skipped with a reason.
    Pass the same badge book each round so a restored message stays once.
    """
    prior = dict(prior or {})
    book = badges if badges is not None else {}
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
        if row.get("may_open") is False and not body.strip():
            emit("no_open passive_empty", subject, sender, read_code(row.get("read_error") or "history_empty"))
            continue
        newest_for_hold = newest_unquoted(body)
        held_secret = secret_hold(newest_for_hold)
        if held_secret:
            emit("secret", subject, sender, held_secret)
            continue
        decision = _apply_badge(row, book)
        prev = dict(prior.get(mid) or {})
        cached = decision_cached(prev)
        incoming = str(row.get("content_hash") or "")
        stored_hash = str(prev.get("decision_hash") or "")
        if cached and (not incoming or incoming == stored_hash):
            noted = bool(prev.get("decision_noted"))
            prev["decision_noted"] = True
            _remember(prior, mid, prev)
            if not noted:
                emit("decision_" + cached, subject, sender)
            continue
        now_s = time.time()
        if str(prev.get("error") or "").startswith("hold:") and prev.get("settled"):
            if not prev.get("settled_noted"):
                prev["settled_noted"] = True
                _remember(prior, mid, prev)
                emit("hold_settled", subject, sender, str(prev.get("error") or "")[:80])
            else:
                _remember(prior, mid, prev)
            continue
        if _grade_waiting(prev, now_s):
            _remember(prior, mid, prev)
            emit("hold_wait", subject, sender, str(prev.get("error") or "")[:80])
            continue
        fetcher = row.get("fetch_body")
        if callable(fetcher) and not body.strip():
            body = str(fetcher() or "")
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
            drafts.append(_with_badge({
                "message_id": mid,
                "subject": subject,
                "from": sender,
                "text": reply,
                "sent": False,
                "via": "sched_confirm",
            }, decision))
            continue
        newest = newest_unquoted(body)
        if scheduling_question(newest):
            _park(prev, "needs_input", reopened)
            _remember(prior, mid, prev)
            emit("scheduling", subject, sender)
            continue
        sources = list(row.get("sources") or [])
        ask = str(row.get("ask") or subject or newest)
        link_kinds: list[str] = []
        if row.get("pages"):
            from hailports.contextpack import pack_pages
            packed = pack_pages(
                "\n".join([subject, body, ask]),
                list(row.get("pages") or []),
                ticket_id=str(row.get("ticket_id") or ""),
                allowed_repos=row.get("allowed_repos"),
            )
            if packed.get("blocked"):
                emit("data_line", subject, sender)
                continue
            if packed.get("text"):
                sources = sources + [packed["text"]]
            link_kinds = list(packed.get("kinds") or [])
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
        draft_secret = secret_hold(text)
        if draft_secret:
            _park(prev, "secret", reopened)
            _remember(prior, mid, prev)
            emit("secret", subject, sender, draft_secret)
            continue
        if row.get("check"):
            if router is None:
                _park(prev, "hold", reopened)
                _remember(prior, mid, prev)
                emit("hold", subject, sender, "no_model")
                continue
            from hailports.contextpack import apply_check

            def _one(messages, max_tokens=180, _router=router):
                got = _router.complete(messages, task="read", max_tokens=max_tokens)
                return {"ok": True, "text": getattr(got, "text", "") or "", "reason": ""}

            checked = apply_check(ask, "\n".join(sources), text, complete=_one)
            if not checked.get("ok"):
                reason = str(checked.get("reason") or "hold")
                if _is_grade_hold(reason):
                    fails = int(prev.get("grade_fails") or 0) + 1
                    prev["grade_fails"] = fails
                    prev["discard_parked"] = False
                    prev["error"] = (reason if reason.startswith("hold:") else "hold:" + reason)[:160]
                    best = str(checked.get("best_text") or text or "").strip()
                    if len(best) > len(str(prev.get("best_text") or "")):
                        prev["best_text"] = best[:800]
                    if fails >= _HOLD_MAX:
                        prev["settled"] = True
                        prev["review"] = True
                        staged_text = str(prev.get("best_text") or "").strip()
                        _remember(prior, mid, prev)
                        if staged_text:
                            drafts.append(_with_badge({
                                "message_id": mid,
                                "subject": subject,
                                "from": sender,
                                "text": staged_text,
                                "sent": False,
                                "review": True,
                                "via": "hold_review",
                            }, decision))
                        else:
                            emit("hold_review", subject, sender, prev["error"][:80])
                        continue
                    prev["next_ok"] = time.time() + _HOLD_WAIT_S[min(fails, len(_HOLD_WAIT_S)) - 1]
                    _remember(prior, mid, prev)
                    emit("hold_wait", subject, sender, prev["error"][:80])
                    continue
                _park(prev, "hold", reopened)
                _remember(prior, mid, prev)
                emit("hold", subject, sender, reason[:80])
                continue
            text = checked.get("text") or ""
        if row.get("require_pass") and not has_pass(text):
            text = stamp_pass(text)
        prev["draft_ok"] = True
        prev["body_preview"] = (text or "")[:80]
        prev["discard_parked"] = False
        prev["error"] = ""
        _remember(prior, mid, prev)
        staged = {
            "message_id": mid,
            "subject": subject,
            "from": sender,
            "text": text,
            "sent": False,
            "via": "draft",
        }
        if link_kinds:
            staged["links"] = link_kinds
        drafts.append(_with_badge(staged, decision))
    return {"drafted": len(drafts), "drafts": drafts, "skipped": skipped, "log": lines, "prior": prior, "badges": book}


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
