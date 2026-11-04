"""Draft a reply using only the source notes you pass in."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class NoSource(ValueError):
    pass


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z0-9']+", text) if len(w) > 2}


_CITE = re.compile(r"\[(\d+)\]")


def citations_ok(text: str, source_count: int) -> bool:
    """Every non-empty line must cite a source number that exists."""
    if source_count < 1 or not (text or "").strip():
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()] or [text.strip()]
    marks: list[int] = []
    for line in lines:
        found = [int(n) for n in _CITE.findall(line)]
        if not found:
            return False
        marks.extend(found)
    return all(1 <= n <= source_count for n in marks)


def draft(ask: str, sources: list[str]) -> str:
    clean = [s.strip() for s in sources if s and s.strip()]
    if not clean:
        raise NoSource("no source notes")
    ask_words = _words(ask)
    picked: list[str] = []
    for src in clean:
        for sent in _sentences(src):
            if ask_words and _words(sent) & ask_words:
                picked.append(sent)
    if not picked:
        picked = _sentences(clean[0])[:2] or [clean[0]]
    # De-dupe, keep order.
    seen: set[str] = set()
    lines: list[str] = []
    for line in picked:
        if line not in seen:
            seen.add(line)
            lines.append(line)
    body = " ".join(lines[:6])
    return body


# Quoted history starts here. The earliest marker wins. The newest text is everything before it.
_QUOTE_MARKERS = (
    re.compile(r"(?i)(?:^|\s)from\s*:"),
    re.compile(r"(?i)-{5,}\s*original message"),
    re.compile(r"(?i)(?:^|\s)on\s+.{5,120}?wrote\s*:"),
    re.compile(r"(?i)(?:^|\s)sent\s*:"),
)
_SCHED_QUESTION = re.compile(
    r"(?i)\b(can|could|shall) we\b[^.?!\n]{0,50}\b(?:try|meet|schedule|set up|setup|get together|hop on|call|connect|catch up|reschedule)\b"
    r"|\b(?:are you|would you be) (?:free|available)\b"
    r"|\bwhat time works\b"
    r"|\bsend (?:me )?(?:an|a) (?:invite|invitation)\b"
)
_TIME_ANSWER = re.compile(
    r"(?i)\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)(?:\s+[A-Za-z]+){0,2}\s+time|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b"
    r".{0,40}?\b(?:okay|ok|works?|good|fine)\b"
)
_ASKED_TIME = re.compile(r"(?i)what time works|we can do a (?:zoom|call|meet)")
_CLOCK = re.compile(r"(?i)\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b(?:\s+([A-Za-z]+))?")
_ZONE_WORD = re.compile(r"(?i)\b([A-Za-z]+)\s+time\b")
_KEEP_SENTENCE = re.compile(
    r"(?i)\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b.{0,40}\bworks\b|\bi am on it\b"
)
PASS_MARK = "[[pass]]"
_ZONES = {
    "wat": "Africa/Luanda",
    "luanda": "Africa/Luanda",
    "angola": "Africa/Luanda",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "ct": "America/Chicago",
    "chicago": "America/Chicago",
    "est": "America/New_York",
    "edt": "America/New_York",
    "et": "America/New_York",
    "utc": "UTC",
    "gmt": "UTC",
    "dubai": "Asia/Dubai",
    "gst": "Asia/Dubai",
    "uk": "Europe/London",
    "london": "Europe/London",
}


def newest_unquoted(body: str) -> str:
    """Text the sender just wrote. Quoted history is not part of the new note."""
    text = body or ""
    cut = None
    for marker in _QUOTE_MARKERS:
        match = marker.search(text)
        if not match:
            continue
        index = match.start()
        while index < len(text) and text[index].isspace():
            index += 1
        if cut is None or index < cut:
            cut = index
    return text if cut is None else text[:cut]


def scheduling_question(text: str) -> bool:
    """True when this text, and only this text, is a new scheduling question."""
    return _SCHED_QUESTION.search(text or "") is not None


def greeting_name(sender: str) -> str:
    who = (sender or "").strip()
    if "," in who and "@" not in who:
        rest = who.split(",", 1)[1].strip()
        if rest:
            who = rest
    if "@" in who:
        who = who.split("@", 1)[0].split(".")[0]
    who = (who.replace(",", " ").split() or ["there"])[0]
    if not re.fullmatch(r"[A-Za-z][A-Za-z'\-]{0,30}", who):
        return "there"
    return who


def _zone_name(phrase: str, word: str) -> str:
    zone_word = (word or "").lower()
    if zone_word in {"", "time"}:
        match = _ZONE_WORD.search(phrase or "")
        if match:
            zone_word = match.group(1).lower()
    return _ZONES.get(zone_word) or "UTC"


def clock_window(phrase: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    match = _CLOCK.search(phrase or "")
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ap = match.group(3).lower()
    if ap == "pm" and hour < 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    try:
        tz = ZoneInfo(_zone_name(phrase, match.group(4) or ""))
    except Exception:
        return None
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    start = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start < current - timedelta(hours=2):
        start = start + timedelta(days=1)
    return start, start + timedelta(minutes=45)


def clock_is_free(phrase: str, events: list | None, now: datetime | None = None) -> bool | None:
    """True when the calendar is open, False when it is booked, None when unknown."""
    if events is None:
        return None
    window = clock_window(phrase, now)
    if window is None:
        return None
    start, end = window
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            began = datetime.fromisoformat(str(event.get("start") or "").replace("Z", "+00:00"))
            ended = datetime.fromisoformat(str(event.get("end") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if began.tzinfo is None:
            began = began.replace(tzinfo=start.tzinfo)
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=start.tzinfo)
        if ended > start and began < end:
            return False
    return True


def scheduling_confirm(body: str, sender: str, events: list | None = None, now: datetime | None = None) -> str:
    """Short confirm when the newest line answers a time already asked for.

    Call talk in the quoted history is not this ask. Empty means leave it alone.
    """
    newest = newest_unquoted(body or "")
    if not newest or "?" not in newest:
        return ""
    if not _ASKED_TIME.search(body or ""):
        return ""
    match = _TIME_ANSWER.search(newest)
    if not match:
        return ""
    phrase = re.sub(r"\s+", " ", match.group(1)).strip()
    who = greeting_name(sender)
    free = clock_is_free(phrase, events, now)
    if free is False:
        return f"hey {who},\n\nthat time is already booked. send another time. I am on it."
    if free is True:
        return f"hey {who},\n\n{phrase} works. I am on it."
    return f"hey {who},\n\nI am on it."


def preserve_kept(original: str, cleaned: str) -> str:
    """Keep the confirm sentence when a cleanup pass would drop it."""
    original = original or ""
    cleaned = cleaned or ""
    if re.search(r"(?i)\bi am on it\b", original) and not re.search(r"(?i)\bi am on it\b", cleaned):
        return original
    if _KEEP_SENTENCE.search(original) and not _KEEP_SENTENCE.search(cleaned):
        return original
    return cleaned


def has_pass(text: str) -> bool:
    return PASS_MARK in (text or "")


def stamp_pass(text: str) -> str:
    if has_pass(text):
        return text
    return (text or "").rstrip() + "\n" + PASS_MARK


def stage_ready(text: str) -> bool:
    return bool((text or "").strip()) and has_pass(text)
