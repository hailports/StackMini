"""Opt-in self-consistency check for a local chat CLI. Pure, data-free, unit-tested.

Idea: for a high-stakes answer, ask the same model the same packed prompt 2 more
times (sequentially, at a slightly higher temperature) and compare the hard facts
across the runs. Facts every run agrees on stay. A fact the other runs do not
back up is not guessed and not majority-voted into the answer: it is marked in
the answer and listed in one plain closing line, e.g.

    unsure: Lakeport vs (no location); 12 pull requests vs 3 pull requests

The visible answer is always the FIRST (normal-temperature) sample, qualified.
No fourth "blended" answer is ever synthesized, and no extra model call is used
to compare. Tests: tests/test_self_consistency.py.

Public functions
----------------
- gate(question, *, trigger=False, env=None, flag_path=None, context="")
    -> (run: bool, reason: str). Decides whether to take extra samples.
      precedence: env L_SELFCONSISTENCY=0 -> off (kill switch, beats everything)
                  trigger (--selfcons / /selfcons) -> on for this question
                  env L_SELFCONSISTENCY=1 or flag file exists -> on, high-stakes only
                  otherwise -> off (single sample, unchanged behavior)
- is_high_stakes(question, context="") -> bool
    True for recaps / "what happened" summaries, questions about dates, places
    or counts, and reply drafts. Casual chat ("hello", "what's your name") is
    False even when the packed context is full of dates.
- extract_facts(text) -> list[Fact]
    Deterministic regex extraction of dates (month-day, ISO, m/d, weekdays,
    clock times), counts ("12 pull requests", "$1,200.50", "three meetings"),
    places (after locative words such as in / at / near / flew to / out of,
    and "the X area"), and names ("with Dana Reyes", capitalized pairs).
- compare_facts(samples) -> dict(agreed=[Fact], disagreed=[(Fact, alt_text)])
    samples[0] is the primary. A primary fact is agreed when at least one other
    sample states it and none contradicts it. A count whose noun appears with a
    different number elsewhere is a contradiction ("12 pull requests vs 3 pull
    requests"); a fact no other sample states is unsupported ("Lakeport vs (no
    location)").
- merge_answer(primary, samples) -> dict(answer, flags, agreed, disagreed, n_samples)
    Marks each disagreed span "(unsure)" in the primary text and appends one
    "unsure: ..." line. With fewer than 2 usable samples it returns the primary
    unchanged and says so in ``note``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "L_SELFCONSISTENCY"
EXTRA_SAMPLES = 2
EXTRA_TEMPERATURE = 0.5

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_WEEKDAYS = {
    "mon": "mon", "monday": "mon", "tue": "tue", "tues": "tue", "tuesday": "tue",
    "wed": "wed", "wednesday": "wed", "thu": "thu", "thur": "thu", "thurs": "thu",
    "thursday": "thu", "fri": "fri", "friday": "fri", "sat": "sat", "saturday": "sat",
    "sun": "sun", "sunday": "sun",
}
_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "twenty": 20,
}

_MON_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WD_RE = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))

_MONTH_DAY = re.compile(r"(?i)\b(" + _MON_RE + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?")
_DAY_MONTH = re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MON_RE + r")\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MD = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])")
_WEEKDAY = re.compile(r"(?i)\b(" + _WD_RE + r")\b\.?")
_TIME = re.compile(r"(?i)\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b([01]?\d|2[0-3]):([0-5]\d)\b")

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
# number + 1-3 word noun phrase. nouns are matched lowercased; stop at punctuation.
_COUNT = re.compile(
    r"(?i)\b(\d[\d,]*|" + "|".join(_NUM_WORDS) + r")\s+((?:[a-z][a-z0-9'-]*\s+){0,2}?[a-z][a-z0-9'-]*s)\b"
)

_LOC_LEAD = re.compile(
    r"(?i)\b(?:located in|based in|held in|flew to|flew into|flying to|flew from|out of|trip to|"
    r"visit(?:ed|ing)? to|headed to|drove to|in|at|near)\s+(?:the\s+)?"
    r"([A-Za-z][\w'.-]*(?:\s+(?-i:[A-Z])[\w'.-]*){0,2})"
)
_AREA = re.compile(r"(?i)\bthe\s+([a-z][\w'-]*(?:\s+[a-z][\w'-]*)?)\s+(?:area|office|airport|campus)\b")
_WITH_NAME = re.compile(r"\bwith\s+([A-Za-z][a-z'-]+\s+[A-Za-z][a-z'-]+)\b")
_CAP_NAME = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z'-]+\s+[A-Z][a-z'-]+)\b")

# words that follow "in/at/near" but are not places. kept small and generic.
_NOT_PLACE = set(
    """
    a an the this that these those my your our their his her its some any all each every no one
    least most first last next least all same time times total person once least mind fact order
    general particular addition case place advance progress person detail full short line touch
    calendar window mail email inbox drafts draft notes note meeting meetings call calls chat thread
    work office home ticket tickets queue list sprint review standup sync it them him me us you
    morning afternoon evening night noon midnight today tomorrow yesterday week weeks month day days
    year years hour hours minute minutes moment end start middle beginning front back top bottom
    which what who whom where when how why least risk charge sight trouble hand play use stock
    sources source records record logs log ledger digest digests context facts history data
    """.split()
)
_NAME_STOP = set(_WEEKDAYS) | set(_MONTHS) | {
    "i", "the", "you", "and", "unsure", "note", "for", "from", "with", "on", "in", "at", "to",
}

_HIGH_STAKES = [
    # recaps / what happened
    r"\bwhat\s+(?:happened|went on|did i (?:do|miss|work on))\b",
    r"\b(?:recap|summar(?:y|ise|ize)|catch me up|rundown|run-down|update me|status of)\b",
    # dates and times
    r"\b(?:when|what (?:day|date|time)|which day|dates?|deadline|due|schedule[ds]?)\b",
    r"\b(?:yesterday|tomorrow|tonight|last week|this week|next week|past week|weekend)\b",
    r"\b(?:" + _WD_RE + r")\b",
    r"\b(?:" + _MON_RE + r")\.?\s+\d{1,2}\b",
    r"\b\d{1,2}/\d{1,2}\b|\b\d{4}-\d{2}-\d{2}\b",
    # places
    r"\b(?:where|location|address|city|venue|which office|travel(?:ed|led|ing)?|trip|flight|hotel)\b",
    # counts
    r"\bhow\s+(?:many|much|often|long)\b",
    r"\b(?:count|number of|total|tally|how much)\b",
    # drafts
    r"\b(?:draft|reply|respond|write (?:back|a note|an email|a message)|compose|answer (?:him|her|them))\b",
]
_HIGH_STAKES_RE = re.compile("(?i)" + "|".join(_HIGH_STAKES))
_CASUAL_RE = re.compile(
    r"(?i)^\s*(?:hi|hey|hello|yo|sup|howdy|thanks|thank you|thx|ok|okay|cool|nice|lol|"
    r"good (?:morning|afternoon|evening|night)|how are you|how's it going|what'?s up|"
    r"what(?:'s| is) your name|who are you|are you there)\b[\s!?.,]*(?:there|buddy|man|claude)?[\s!?.,]*$"
)


@dataclass(frozen=True)
class Fact:
    kind: str      # "date" | "time" | "weekday" | "count" | "money" | "place" | "name"
    key: str       # comparison slot: noun for counts, value for the rest
    value: str     # normalized value
    text: str      # the span as written in the sample


def _truthy_env(v: str | None) -> bool | None:
    if v is None:
        return None
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return None


def default_flag_path() -> Path:
    return Path.home() / "claude-stack" / "data" / "flags" / "l_selfconsistency"


def is_casual(question: str) -> bool:
    q = (question or "").strip()
    if not q:
        return True
    return bool(_CASUAL_RE.match(q))


def is_high_stakes(question: str, context: str = "") -> bool:
    """Recaps, date/place/count questions, and reply drafts. Casual chat never is.

    ``context`` is the packed prompt. It only tips a non-casual question with no
    trigger words over when it is dense with dated or counted facts (3+), so a
    generic "anything new on the portal?" over a dated packet still qualifies,
    while "hello" over the same packet does not.
    """
    q = (question or "").strip()
    if is_casual(q):
        return False
    if _HIGH_STAKES_RE.search(q):
        return True
    if context and len(q.split()) >= 3:
        dense = [f for f in extract_facts(context[:6000]) if f.kind in ("date", "count", "money")]
        return len(dense) >= 3
    return False


def gate(
    question: str,
    *,
    trigger: bool = False,
    env: dict | None = None,
    flag_path: str | Path | None = None,
    context: str = "",
) -> tuple[bool, str]:
    """Should this question take extra samples? Returns (run, reason)."""
    env = os.environ if env is None else env
    forced = _truthy_env(env.get(ENV_VAR))
    if forced is False:
        return False, "env-off"
    if trigger:
        return True, "trigger"
    fp = Path(flag_path) if flag_path is not None else default_flag_path()
    on = forced is True or fp.exists()
    if not on:
        return False, "off"
    if is_high_stakes(question, context):
        return True, "env-on" if forced else "flag-on"
    return False, "casual"


# ---------------------------------------------------------------- extraction

def _norm_noun(phrase: str) -> str:
    words = phrase.lower().split()
    out = []
    for w in words:
        if len(w) > 3 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return " ".join(out)


def _num(tok: str) -> str:
    t = tok.lower().replace(",", "")
    if t in _NUM_WORDS:
        return str(_NUM_WORDS[t])
    try:
        f = float(t)
    except ValueError:
        return t
    return str(int(f)) if f == int(f) else ("%.2f" % f)


def extract_facts(text: str) -> list[Fact]:
    t = text or ""
    facts: list[Fact] = []
    seen: set[tuple[str, str, str]] = set()
    taken: list[tuple[int, int]] = []

    def add(kind: str, key: str, value: str, span: str, start: int = -1, end: int = -1) -> None:
        sig = (kind, key, value)
        if sig in seen:
            return
        seen.add(sig)
        facts.append(Fact(kind, key, value, span.strip()))
        if start >= 0:
            taken.append((start, end))

    def overlaps(s: int, e: int) -> bool:
        return any(s < b and a < e for a, b in taken)

    for m in _ISO.finditer(t):
        v = "%02d-%02d" % (int(m.group(2)), int(m.group(3)))
        add("date", v, v, m.group(0), m.start(), m.end())
    for m in _MONTH_DAY.finditer(t):
        if overlaps(m.start(), m.end()):
            continue
        d = int(m.group(2))
        if 1 <= d <= 31:
            v = "%02d-%02d" % (_MONTHS[m.group(1).lower()], d)
            add("date", v, v, m.group(0), m.start(), m.end())
    for m in _DAY_MONTH.finditer(t):
        if overlaps(m.start(), m.end()):
            continue
        d = int(m.group(1))
        if 1 <= d <= 31:
            v = "%02d-%02d" % (_MONTHS[m.group(2).lower()], d)
            add("date", v, v, m.group(0), m.start(), m.end())
    for m in _MD.finditer(t):
        if overlaps(m.start(), m.end()):
            continue
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            v = "%02d-%02d" % (mo, d)
            add("date", v, v, m.group(0), m.start(), m.end())
    for m in _WEEKDAY.finditer(t):
        w = m.group(1).lower()
        # "sun" / "sat" / "wed" are also plain words; take them only as "on sat" or next to a date
        if w in ("sun", "sat", "wed"):
            before = t[max(0, m.start() - 3):m.start()].lower()
            after = t[m.end():m.end() + 6].lower()
            if not (before.endswith("on ") or re.match(r"[\s,]*(?:" + _MON_RE + r"|\d)", after)):
                continue
        v = _WEEKDAYS[w]
        add("weekday", v, v, m.group(1), m.start(), m.end())
    for m in _TIME.finditer(t):
        if overlaps(m.start(), m.end()):
            continue
        if m.group(3):
            h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
            mi = int(m.group(2) or 0)
        else:
            h, mi = int(m.group(4)), int(m.group(5))
        v = "%02d:%02d" % (h, mi)
        add("time", v, v, m.group(0), m.start(), m.end())
    for m in _MONEY.finditer(t):
        v = _num(m.group(1))
        add("money", "$" + v, v, m.group(0), m.start(), m.end())
    for m in _COUNT.finditer(t):
        if overlaps(m.start(), m.end()):
            continue
        raw = m.group(1)
        prev = t[max(0, m.start() - 4):m.start()].lower()
        # ids are not counts: "#1426", "sf-3007", "ado 382662", long bare numbers
        if (raw.isdigit() and len(raw) >= 5) or prev.endswith(("#", "-", "ado ", "pr ", "id ")):
            continue
        noun = _norm_noun(m.group(2))
        first = noun.split()[0]
        if first in _MONTHS or first in _WEEKDAYS or first in ("am", "pm", "and", "or", "of", "to"):
            continue
        add("count", noun, _num(m.group(1)), m.group(0), m.start(), m.end())

    for m in _AREA.finditer(t):
        name = m.group(1).strip()
        w0 = name.split()[0].lower()
        if w0 in _NOT_PLACE or w0 in _WEEKDAYS or w0 in _MONTHS:
            continue
        add("place", name.lower(), name.lower(), name, m.start(1), m.end(1))
    for m in _LOC_LEAD.finditer(t):
        s, e = m.start(1), m.end(1)
        if overlaps(s, e):
            continue
        name = m.group(1).strip(" .,'")
        words = name.split()
        # drop trailing generic words ("lakeport area" -> "lakeport")
        while words and words[-1].lower() in {"area", "office", "airport", "campus"} | _NOT_PLACE:
            words.pop()
        if not words:
            continue
        w0 = words[0].lower()
        if (w0 in _NOT_PLACE or w0 in _WEEKDAYS or w0 in _MONTHS or w0[0].isdigit()
                or len(w0) < 3):
            continue
        # lowercase text: only take the first word unless the rest is capitalized
        name = " ".join(words[:1] + [w for w in words[1:] if w[:1].isupper()])
        add("place", name.lower(), name.lower(), name, s, s + len(name))

    places = {f.value for f in facts if f.kind == "place"}
    for rx in (_WITH_NAME, _CAP_NAME):
        for m in rx.finditer(t):
            name = m.group(1).strip()
            parts = [p.lower() for p in name.split()]
            if any(p in _NAME_STOP or p in _NOT_PLACE for p in parts):
                continue
            # lowercase "with x y" is a name only if it looks like one: no hyphens, no plural nouns
            if rx is _WITH_NAME and not name[:1].isupper() and any(
                    "-" in p or (p.endswith("s") and not p.endswith("ss")) or p.endswith("ing") for p in parts):
                continue
            if name.lower() in places or parts[0] in places:
                continue
            add("name", name.lower(), name.lower(), name)
    return facts


# ---------------------------------------------------------------- compare

def _alt_text(kind: str, others: list[Fact]) -> str:
    if others:
        return " / ".join(dict.fromkeys(o.text for o in others))
    return {"place": "(no location)", "name": "(not named)", "count": "(no count)",
            "money": "(no amount)", "date": "(no date)", "weekday": "(no day)",
            "time": "(no time)"}.get(kind, "(not stated)")


def compare_facts(samples: list[str]) -> dict:
    """samples[0] is the primary answer. Empty/failed samples should be dropped by the caller."""
    texts = [s for s in samples if (s or "").strip()]
    if not texts:
        return {"agreed": [], "disagreed": []}
    sets = [extract_facts(s) for s in texts]
    primary, rest = sets[0], sets[1:]
    agreed: list[Fact] = []
    disagreed: list[tuple[Fact, str]] = []
    if not rest:
        return {"agreed": list(primary), "disagreed": []}
    for f in primary:
        supports = 0
        contra: list[Fact] = []
        for other in rest:
            same_slot = [o for o in other if o.kind == f.kind and o.key == f.key]
            if any(o.value == f.value for o in same_slot):
                supports += 1
            elif f.kind == "count" and same_slot:
                contra.extend(same_slot)
        if contra:
            disagreed.append((f, _alt_text(f.kind, contra)))
        elif supports == 0:
            disagreed.append((f, _alt_text(f.kind, [])))
        else:
            agreed.append(f)
    return {"agreed": agreed, "disagreed": disagreed}


def merge_answer(primary: str, samples: list[str]) -> dict:
    """Qualify the primary answer with what the samples disagree on. Never blends.

    ``samples`` may or may not include ``primary`` as its first item; it is
    added when missing. Returns dict(answer, flags, agreed, disagreed, n_samples, note).
    """
    primary = primary or ""
    texts = [s for s in (samples or []) if (s or "").strip()]
    if not texts or texts[0] != primary:
        texts = [primary] + [s for s in texts if s != primary or not primary]
    texts = [s for s in texts if (s or "").strip()]
    n = len(texts)
    if n < 2 or not primary.strip():
        return {"answer": primary, "flags": [], "agreed": [], "disagreed": [], "n_samples": n,
                "note": "self-check skipped: only %d usable sample(s)" % n}
    res = compare_facts(texts)
    out = primary
    flags: list[str] = []
    for f, alt in res["disagreed"]:
        flags.append("%s vs %s" % (f.text, alt))
        # mark the first occurrence of the span in the answer, case-sensitively as written
        i = out.find(f.text)
        if i >= 0 and not out[i + len(f.text):].startswith(" (unsure)"):
            out = out[: i + len(f.text)] + " (unsure)" + out[i + len(f.text):]
    if flags:
        out = out.rstrip() + "\n\nunsure: " + "; ".join(flags)
    return {"answer": out, "flags": flags, "agreed": res["agreed"], "disagreed": res["disagreed"],
            "n_samples": n, "note": ""}
