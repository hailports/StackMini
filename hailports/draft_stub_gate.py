"""Refuse an Outlook reply that is only a greeting, closer, disclaimer, or signature.

A closer in a reply that still says something is removed for the emptiness
check and does not, by itself, refuse the draft. A repeated greeting, or a
greeting that uses a recipient surname, refuses even when other text remains.
"""

from __future__ import annotations

import re

GROUNDED_ASK_EVIDENCE = "grounded draft for a real ask"

_GREET_LINE = re.compile(r"(?i)^\s*(?:hey|hi|hello)\b")
_GREET_PREFIX = re.compile(
    r"(?i)^\s*(?:hey|hi|hello)\s+.{1,80}?(?:--|—|,|:)\s*"
)
_CLOSER_RE = re.compile(
    r"(?i)\bi have"
    r"(?:\s+(?:a|an|the|that|some|this|those|these|it|them|my|our|your|[A-Za-z][A-Za-z0-9'-]{1,24})){0,8}"
    r"\s+ready for your go\b[.!,]?"
)
_DISCLAIMER_RE = re.compile(r"(?i)\bi have not changed crm\b[.!,]?")
_GREET_SKIP = {"hey", "hi", "hello", "there", "all", "team", "both", "folks", "everyone"}
_SURNAME_STOP = {
    "hey", "hi", "hello", "the", "this", "that", "please", "thanks", "thank",
    "from", "to", "cc", "subject", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "crm", "outlook", "best", "regards",
    "dear", "team", "all", "and", "for", "you", "your", "my", "our", "asks",
    "ask", "could", "would", "can", "have", "not", "changed", "ready", "with",
    "about", "just", "also", "there", "here", "review", "confirm", "share",
    "attached", "query", "date", "filter", "right", "north", "region",
    "discount", "tiers", "label", "names", "sheet", "report", "inc", "llc",
    "ltd", "corp", "street", "plaza", "avenue", "usa", "mobile", "email",
    "phone", "example", "technical", "lead",
}


def _norm_newlines(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _sig_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return True
    if re.fullmatch(r"[-_=]{2,}", s):
        return True
    if re.fullmatch(
        r"(?i)(?:best|thanks|thank you|regards|cheers|sincerely|talk soon)[,!]?",
        s,
    ):
        return True
    if len(s) < 160 and "|" in s:
        return True
    if len(s) < 180 and re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", s):
        return True
    if len(s) < 80 and re.search(r"(?i)\b(?:mobile|phone|tel|email)\s*:", s):
        return True
    if re.fullmatch(r"(?i)(?:https?://\S+|www\.\S+)", s):
        return True
    if re.fullmatch(
        r"(?i)[\w.-]+\.(?:com|net|org)(?:\s*\|\s*[\w.-]+\.(?:com|net|org))*",
        s,
    ):
        return True
    return False


def _name_line(line: str) -> bool:
    s = (line or "").strip()
    return bool(
        s
        and len(s) < 40
        and re.fullmatch(r"[A-Z][A-Za-z.'’-]*(?:\s+[A-Z][A-Za-z.'’-]*){0,3}", s)
    )


def _strip_trailing_signature(text: str) -> str:
    lines = _norm_newlines(text).split("\n")
    end = len(lines)
    while end > 0 and _sig_line(lines[end - 1]):
        end -= 1
    if end < len(lines) and end > 0 and _name_line(lines[end - 1]):
        end -= 1
    return "\n".join(lines[:end]).strip()


def _strip_greetings(text: str) -> str:
    kept = []
    for line in _norm_newlines(text).split("\n"):
        if not _GREET_LINE.match(line):
            kept.append(line)
            continue
        rest = _GREET_PREFIX.sub("", line, count=1).strip()
        if rest and rest != line.strip():
            kept.append(rest)
        elif rest and not re.search(r"(?i)(?:--|—|,|:)", line):
            # Greeting words with no separator and real text after them.
            kept.append(line)
    return "\n".join(kept)


def _greeting_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for line in _norm_newlines(text).split("\n"):
        match = re.match(r"(?i)^\s*(?:hey|hi|hello)\s+(.{1,80})", line.strip())
        if not match:
            continue
        head = re.split(r",|--|—|:", match.group(1), maxsplit=1)[0]
        for part in re.split(r"(?i)\s*(?:&|and)\s*", head):
            for word in re.findall(r"[A-Za-z][A-Za-z'’-]*", part):
                if word.lower() not in _GREET_SKIP:
                    tokens.append(word)
    return tokens


def _people_surnames(source: str) -> set[str]:
    firsts: set[str] = set()
    lasts: set[str] = set()
    text = source or ""
    for match in re.finditer(
        r"\b([A-Z][A-Za-z'’-]{1,30})\s*,\s*([A-Z][A-Za-z'’-]{1,30})\b",
        text,
    ):
        last, first = match.group(1), match.group(2)
        if last.lower() in _SURNAME_STOP or first.lower() in _SURNAME_STOP:
            continue
        if len(last) < 3:
            continue
        lasts.add(last.lower())
        firsts.add(first.lower())
    for match in re.finditer(
        r"\b([A-Z][A-Za-z'’-]{1,30})\s+([A-Z][A-Za-z'’-]{1,30})\b",
        text,
    ):
        first, last = match.group(1), match.group(2)
        if first.lower() in _SURNAME_STOP or last.lower() in _SURNAME_STOP:
            continue
        if len(last) < 3:
            continue
        firsts.add(first.lower())
        lasts.add(last.lower())
    return {last for last in lasts if last not in firsts}


def _has_substance(text: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]{2,}", text or ""))


def _dup_greeting(text: str) -> bool:
    return sum(1 for line in _norm_newlines(text).split("\n") if _GREET_LINE.match(line)) >= 2


def outlook_shell_reasons(text: str, source: str = "") -> list[str]:
    """Reasons this Outlook body must not be saved or called grounded.

    empty_after_strip / closer_only: nothing substantive remains once greeting,
    closer, disclaimer, and signature are gone. dup_greeting and
    recipient_surname_greeting refuse even when other text remains.
    """
    raw = _norm_newlines(text)
    reasons: list[str] = []
    if _dup_greeting(raw):
        reasons.append("dup_greeting")
    surnames = _people_surnames(source)
    if surnames and any(tok.lower() in surnames for tok in _greeting_tokens(raw)):
        reasons.append("recipient_surname_greeting")
    stripped = _strip_greetings(raw)
    stripped, closer_n = _CLOSER_RE.subn("", stripped)
    stripped = _DISCLAIMER_RE.sub("", stripped)
    stripped = _strip_trailing_signature(stripped)
    if not _has_substance(stripped):
        if closer_n:
            reasons.append("closer_only")
        reasons.append("empty_after_strip")
    return reasons


def blocks_grounded_record(text: str, source: str = "") -> bool:
    return bool(outlook_shell_reasons(text, source))


def grounded_evidence_or_refuse(text: str, source: str = "") -> str:
    """The grounded-ask label, or empty when this body must not be recorded as one."""
    if blocks_grounded_record(text, source):
        return ""
    return GROUNDED_ASK_EVIDENCE
