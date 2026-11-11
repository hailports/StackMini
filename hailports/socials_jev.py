#!/usr/bin/env python3
"""Social draft gates. Fail-open. Never raises. Never logs state or keys.

Confident only: noul >= 0.9 or <= 0.1 changes a decision. A miss, a timeout,
a mid score, or JEV_DISABLE=1 leaves the caller's existing behavior.

Callers are socials-*. Secrets are masked before the request. Legal, finance,
and credential text is not sent.
"""
from __future__ import annotations

HI = 0.9
LO = 0.1

Q_QUALITY = (
    "The draft is specific, finished, and worth posting. "
    "It is not a stub, a placeholder, or generic praise"
)
Q_VOICE = (
    "The draft is plain human speech. It has no AI tells such as certainly, "
    "delve, as an AI, leverage, or tapestry"
)
Q_HOOK = (
    "The first line is a concrete hook. It is not a generic opener such as "
    "great post, in today's world, or excited to share"
)
Q_RISK = (
    "This draft is unsafe to post. It harasses, tells people to pile on, "
    "attacks a private person, promises money or results, or leaks a private system"
)
Q_DUP = (
    "The draft repeats one of the RECENT lines. Same claim or joke with only "
    "small wording changes"
)


def _g():
    try:
        import jev_gates as g  # type: ignore
        return g
    except Exception:
        pass
    try:
        from hailports import jev_gates as g  # type: ignore
        return g
    except Exception:
        return None


def _ask(state: str, questions: dict, caller: str, timeout: float = 6.0):
    g = _g()
    if g is None:
        return None
    try:
        return g._ask(state, questions, caller, timeout)
    except Exception:
        return None


def _noul(ans, qid: str):
    g = _g()
    if g is None or not ans:
        return None
    try:
        return g._noul(ans, qid)
    except Exception:
        return None


def _clip(text: str, limit: int = 800) -> str:
    g = _g()
    if g is None:
        return (text or "").strip()[:limit]
    try:
        return g.clip(text, limit)
    except Exception:
        return (text or "").strip()[:limit]


def _sensitive(text: str) -> bool:
    g = _g()
    if g is None:
        return False
    try:
        return bool(g.sensitive(text))
    except Exception:
        return False


def _bars():
    g = _g()
    if g is None:
        return HI, LO
    try:
        return float(g.HI), float(g.LO)
    except Exception:
        return HI, LO


def _recent_list(recent) -> list[str]:
    if recent is None:
        return []
    if isinstance(recent, str):
        parts = [recent]
    else:
        try:
            parts = list(recent)
        except Exception:
            return []
    out = []
    for part in parts:
        line = _clip(str(part or ""), 160)
        if line:
            out.append(line)
        if len(out) >= 3:
            break
    return out


def _one(state: str, qid: str, instructions: str, caller: str):
    if not (state or "").strip() or _sensitive(state):
        return None
    ans = _ask(state, {qid: {"type": "noul", "instructions": instructions}}, caller, 6.0)
    return _noul(ans, qid)


def draft_quality(text: str, caller: str = "socials-draft") -> bool | None:
    """False only when the draft is confidently too weak to post."""
    score = _one("DRAFT:\n" + _clip(text), "quality", Q_QUALITY, caller)
    hi, lo = _bars()
    if score is None:
        return None
    if score <= lo:
        return False
    if score >= hi:
        return True
    return None


def voice_ok(text: str, caller: str = "socials-voice") -> bool | None:
    """False only when the draft is confidently not plain human speech."""
    score = _one("DRAFT:\n" + _clip(text), "voice", Q_VOICE, caller)
    hi, lo = _bars()
    if score is None:
        return None
    if score <= lo:
        return False
    if score >= hi:
        return True
    return None


def hook_ok(text: str, caller: str = "socials-hook") -> bool | None:
    """False only when the first line is confidently a weak opener."""
    score = _one("DRAFT:\n" + _clip(text), "hook", Q_HOOK, caller)
    hi, lo = _bars()
    if score is None:
        return None
    if score <= lo:
        return False
    if score >= hi:
        return True
    return None


def unsafe(text: str, caller: str = "socials-risk") -> bool | None:
    """True only when the draft is confidently unsafe to post."""
    score = _one("DRAFT:\n" + _clip(text), "risk", Q_RISK, caller)
    hi, lo = _bars()
    if score is None:
        return None
    if score >= hi:
        return True
    if score <= lo:
        return False
    return None


def near_duplicate(text: str, recent, caller: str = "socials-dedupe") -> bool | None:
    """True only when the draft confidently repeats a recent line."""
    lines = _recent_list(recent)
    if not lines or not (text or "").strip():
        return None
    state = "DRAFT:\n" + _clip(text, 700) + "\n\nRECENT:\n" + "\n---\n".join(lines)
    score = _one(state, "near_dup", Q_DUP, caller)
    hi, lo = _bars()
    if score is None:
        return None
    if score >= hi:
        return True
    if score <= lo:
        return False
    return None


def _ledger_recent(lane: str) -> list[str]:
    recent_texts = None
    try:
        from posted_ledger import recent_texts as rt  # type: ignore
        recent_texts = rt
    except Exception:
        pass
    if recent_texts is None:
        try:
            from core.posted_ledger import recent_texts as rt  # type: ignore
            recent_texts = rt
        except Exception:
            return []
    persona = {
        "x_ima": "ima",
        "x_darkgary": "darkgary",
        "x_hailports": "hailports",
        "hailports_linkedin": "hailports",
        "realname": "realname",
    }.get(lane or "")
    try:
        rows = recent_texts(persona=persona) if persona else recent_texts()
    except Exception:
        return []
    return _recent_list(rows)


def _queue_questions(text: str, kind: str) -> dict:
    kind = kind or "post"
    questions = {"risk": {"type": "noul", "instructions": Q_RISK}}
    if kind == "react" or len((text or "").strip()) < 40:
        return questions
    questions["quality"] = {"type": "noul", "instructions": Q_QUALITY}
    questions["voice"] = {"type": "noul", "instructions": Q_VOICE}
    if kind in ("post", "draft"):
        questions["hook"] = {"type": "noul", "instructions": Q_HOOK}
    return questions


def before_queue(text: str, *, lane: str = "", kind: str = "post", recent=None) -> tuple[bool, str]:
    """(ok, reason). ok is False only on a confident fail. Never raises."""
    try:
        return _before_queue(text, lane=lane, kind=kind, recent=recent)
    except Exception:
        return True, "jev_open"


def _before_queue(text: str, *, lane: str = "", kind: str = "post", recent=None) -> tuple[bool, str]:
    raw = text or ""
    if not raw.strip() or _sensitive(raw):
        return True, "jev_open"
    hi, lo = _bars()
    questions = _queue_questions(raw, kind or "post")
    ans = _ask("DRAFT:\n" + _clip(raw, 800), questions, "socials-queue", 6.0)
    if "quality" in questions:
        quality = _noul(ans, "quality")
        if quality is not None and quality <= lo:
            return False, "jev_quality"
    if "voice" in questions:
        voice = _noul(ans, "voice")
        if voice is not None and voice <= lo:
            return False, "jev_voice"
    if "hook" in questions:
        hook = _noul(ans, "hook")
        if hook is not None and hook <= lo:
            return False, "jev_hook"
    risk = _noul(ans, "risk")
    if risk is not None and risk >= hi:
        return False, "jev_risk"
    lines = _recent_list(recent)
    if lines and (kind or "post") != "react":
        try:
            from x_dedupe import semantic_hold  # type: ignore
            held = semantic_hold(raw, lines)
        except Exception:
            held = near_duplicate(raw, lines) is True
        if held:
            return False, "jev_near_dup"
    return True, "jev_open"


def hold(text: str, lane: str = "", kind: str = "post", recent=None) -> tuple[bool, str]:
    """Production entry. Loads recent posted lines when the caller does not pass any."""
    try:
        if recent is None and (kind or "post") != "react":
            recent = _ledger_recent(lane)
        return before_queue(text, lane=lane, kind=kind, recent=recent)
    except Exception:
        return True, "jev_open"
