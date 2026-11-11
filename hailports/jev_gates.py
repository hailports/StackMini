#!/usr/bin/env python3
"""Jev decision gates. Fail-open. Never raises. Never logs state or keys.

Confident only: noul >= 0.9 or <= 0.1 changes a decision. Mid scores and
any error, timeout, or null answer leave the caller's existing behavior.
"""
from __future__ import annotations

import os
import re

HI = 0.9
LO = 0.1

_SECRET = re.compile(
    r"\b(password|passwd|passcode|api[_-]?key|token|secret|bearer)\b\s*[:=]\s*\S+"
    r"|\b(otp|2fa|verification code)\b\s*[:=]?\s*\d{4,8}"
    r"|\b(?:sk-|ghp_|github_pat_|xox[baprs]-|AKIA)[A-Za-z0-9_\-]{6,}",
    re.I,
)
_SENSITIVE = re.compile(
    r"(?i)\b(attorney|lawyer|legal counsel|accountant|bank account|routing number|"
    r"social security|\bssn\b|seed phrase|private key)\b"
)


def mask(text: str) -> str:
    raw = text or ""
    raw = _SECRET.sub("[redacted]", raw)
    return raw


def clip(text: str, limit: int = 900) -> str:
    raw = mask(text).strip()
    if len(raw) <= limit:
        return raw
    return raw[:limit]


def sensitive(text: str) -> bool:
    return bool(_SENSITIVE.search(text or ""))


def _jc():
    try:
        from hailports import jev as jc
        return jc
    except Exception:
        return None


def _ask(state: str, questions: dict, caller: str, timeout: float = 4.0):
    if os.environ.get("JEV_DISABLE", "") == "1":
        return None
    if sensitive(state):
        return None
    jc = _jc()
    if jc is None:
        return None
    try:
        return jc.classify(clip(state, 1800), questions, timeout=timeout, caller=caller)
    except Exception:
        return None


def _noul(ans, qid: str):
    jc = _jc()
    if jc is None or not ans:
        return None
    try:
        return jc.noul(ans, qid)
    except Exception:
        return None


def score_local(task: str, answer: str, caller: str = "local-first") -> str | None:
    """'stop' keeps the local answer. 'escalate' sends it up. None keeps the old rule."""
    if not (answer or "").strip():
        return None
    state = "TASK:\n" + clip(task, 700) + "\n\nANSWER:\n" + clip(answer, 900)
    ans = _ask(state, {
        "quality": {"type": "noul", "instructions": "The answer is correct, specific, and usable for the task"},
        "complete": {"type": "noul", "instructions": "The answer fully finishes the task with nothing important left out"},
    }, caller=caller, timeout=4.0)
    quality, complete = _noul(ans, "quality"), _noul(ans, "complete")
    if quality is None or complete is None:
        return None
    if quality >= HI and complete >= HI:
        return "stop"
    if quality <= LO or complete <= LO:
        return "escalate"
    return None


def pre_route(task: str, caller: str = "cli-pre") -> str | None:
    """'escalate' when a small local model should not finish this alone. Else None."""
    state = "TASK:\n" + clip(task, 800)
    ans = _ask(state, {
        "needs_stronger": {
            "type": "noul",
            "instructions": "This task needs tools, a code change, a deploy, a private-data lookup, or a long uncertain investigation that a small local model should not finish alone",
        },
    }, caller=caller, timeout=4.0)
    score = _noul(ans, "needs_stronger")
    if score is not None and score >= HI:
        return "escalate"
    return None


def done_check(task: str, output: str, caller: str = "done") -> bool | None:
    """True when confidently done, False when confidently not done, else None."""
    if not (output or "").strip():
        return None
    state = "TASK:\n" + clip(task, 600) + "\n\nRESULT:\n" + clip(output, 900)
    ans = _ask(state, {
        "done": {"type": "noul", "instructions": "The result actually finishes the task. It is not a stub, a refusal, or an error report"},
    }, caller=caller, timeout=4.0)
    score = _noul(ans, "done")
    if score is None:
        return None
    if score >= HI:
        return True
    if score <= LO:
        return False
    return None


def risk_gate(text: str, caller: str = "risk") -> bool | None:
    """True only when the proposal is confidently risky. Callers then refuse to stage it."""
    if not (text or "").strip():
        return None
    ans = _ask("PROPOSAL:\n" + clip(text, 900), {
        "risky": {
            "type": "noul",
            "instructions": "This proposal is risky or hard to undo: it would send, delete, deploy, pay, or change shared records",
        },
    }, caller=caller, timeout=4.0)
    score = _noul(ans, "risky")
    if score is None:
        return None
    if score >= HI:
        return True
    if score <= LO:
        return False
    return None


def answers_question(question: str, pack: str, caller: str = "work-brain") -> bool | None:
    """False only when the pack confidently does not answer the question."""
    if not (pack or "").strip():
        return None
    state = "QUESTION:\n" + clip(question, 400) + "\n\nPACK:\n" + clip(pack, 900)
    ans = _ask(state, {
        "answers": {"type": "noul", "instructions": "The pack contains the facts needed to answer the question"},
    }, caller=caller, timeout=4.0)
    score = _noul(ans, "answers")
    if score is None:
        return None
    if score <= LO:
        return False
    if score >= HI:
        return True
    return None


_DRAFT_Q = {
    "voice_ok": {"type": "noul", "instructions": "The draft is casual human voice with no AI tells such as certainly, delve, as an AI, or a wall of labels"},
    "is_thin_stub": {"type": "noul", "instructions": "The draft is a thin stub, placeholder, or generic acknowledgement with no real substance"},
    "is_technical_dump": {"type": "noul", "instructions": "The draft dumps internal logs, ids, or tooling jargon a business recipient would not want"},
    "answers_the_ask": {"type": "noul", "instructions": "The draft directly answers what the inbound message asked"},
    "addressed_here": {"type": "noul", "instructions": "The inbound message is for the recipient of this mailbox, not someone else"},
    "commits": {"type": "noul", "instructions": "The draft commits the sender to a new promise, deadline, payment, or deliverable"},
    "asks_lookup": {"type": "noul", "instructions": "The draft asks the sender for information the replier could look up"},
    "right_thread": {"type": "noul", "instructions": "The draft replies to this inbound message, not a different topic or thread"},
}


def draft_gate(inbound: str, draft: str, caller: str = "draft-stage") -> tuple:
    """(ok, reason, scores). ok is False only on a confident fail. None means Jev was down."""
    state = "INBOUND:\n" + clip(inbound, 700) + "\n\nDRAFT:\n" + clip(draft, 700)
    ans = _ask(state, _DRAFT_Q, caller=caller, timeout=6.0)
    if not ans:
        return None, "jev_unavailable", {}
    scores = {key: _noul(ans, key) for key in _DRAFT_Q}
    reason = None
    if scores.get("voice_ok") is not None and scores["voice_ok"] <= LO:
        reason = "jev_voice"
    elif (scores.get("is_thin_stub") or 0) >= HI:
        reason = "jev_thin_stub"
    elif (scores.get("is_technical_dump") or 0) >= HI:
        reason = "jev_technical_dump"
    elif scores.get("answers_the_ask") is not None and scores["answers_the_ask"] <= LO:
        reason = "jev_misses_ask"
    elif scores.get("addressed_here") is not None and scores["addressed_here"] <= LO:
        reason = "jev_wrong_person"
    elif (scores.get("commits") or 0) >= HI:
        reason = "jev_commits"
    elif (scores.get("asks_lookup") or 0) >= HI:
        reason = "jev_asks_lookupable_info"
    elif scores.get("right_thread") is not None and scores["right_thread"] <= LO:
        reason = "jev_wrong_thread"
    if reason is None:
        return True, "jev_ok", scores
    return False, reason, scores


def triage(message: str, caller: str = "triage") -> str | None:
    """needs_reply, needs_action, or fyi_noise. fyi_noise only when confident. None keeps the old path."""
    ans = _ask(clip(message, 800), {
        "kind": {
            "type": "choice",
            "instructions": "What the recipient needs to do with this message",
            "criteria": {
                "needs_reply": "Someone expects a written reply",
                "needs_action": "A task has to be done",
                "fyi_noise": "Newsletter, automated notice, or FYI with no response needed",
            },
        },
        "needs_reply": {"type": "noul", "instructions": "A person is directly asking the recipient for something"},
    }, caller=caller, timeout=4.0)
    if not ans:
        return None
    jc = _jc()
    if jc is None:
        return None
    try:
        kind, conf = jc.choice(ans, "kind")
    except Exception:
        return None
    reply = _noul(ans, "needs_reply") or 0
    if kind == "fyi_noise" and (conf < 0.85 or reply > 0.15):
        return None
    if kind in ("needs_reply", "needs_action", "fyi_noise"):
        return kind
    return None
