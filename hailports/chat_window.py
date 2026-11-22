"""Small, data-free helpers for a local chat CLI.

Three pure functions, all unit-tested in tests/test_chat_window.py:

- resolve_relative_window(question, now=None, tz="America/Chicago")
    Turn a relative time phrase in a chat question into an explicit date window.
- strip_leading_punct(text)
    Drop stray punctuation left at the very start of a reply (e.g. ". what's up?").
- should_idle_quit(idle_s, partial_line, drafts_waiting)
    Decide whether an idle interactive prompt should exit so background work can
    have the model, or just re-arm its timer and keep the session.

Answer-consistency helpers (also pure, also tested):

- window_bounds / window_span / in_window
    One resolved window, used the same way by every block of a prompt
    (epoch [start, end) bounds and an inclusive "Mon Nov 16 - Sun Nov 22" label).
- parse_flight_legs / travel_date / is_cancellation / reconcile_trip / trip_summary
    Read flight legs with their real departure dates (not the date the mail was
    sent), drop cancelled itineraries, and reconcile the trip with calendar
    events in other places, so a reply never says "home Tuesday" and "on site
    Wednesday" at once.
- topic_of / person_summary
    "Name - what about" lines instead of bare message counts.
- stale_note / strip_stale_mentions
    One clear closing note for a source that has not synced recently, instead
    of a dangling "X is stale" bullet.

Relative-window convention ("today" is the local date in ``tz``, never UTC):

    "last week", "this last week", "the last week",
    "past week", "this past week", "the past week"  -> rolling 7 days: today-6 .. today
    "past N days" / "last N days"                    -> today-(N-1) .. today
    "this week"                                      -> Monday of this week .. today
    "previous week", "the week before"               -> previous calendar week, Mon..Sun
    "week before last"                               -> calendar week two back, Mon..Sun
    "N weeks ago"                                    -> calendar week N back, Mon..Sun
    "yesterday" / "today"                            -> that single day

Every result carries a ``label`` that names the window with explicit dates, e.g.
``last 7 days (2026-11-16 to 2026-11-22)``, so an answer can state exactly which
days it covered.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Chicago"

_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fourteen": 14, "thirty": 30,
}
_NUM = r"(\d{1,3}|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"

_DAYS_RE = re.compile(r"(?i)\b(?:the\s+)?(?:past|last)\s+" + _NUM + r"\s+days?\b")
_WEEKS_AGO_RE = re.compile(r"(?i)\b" + _NUM + r"\s+weeks?\s+ago\b")
_WEEK_BEFORE_LAST_RE = re.compile(r"(?i)\bweek\s+before\s+last\b")
_PREVIOUS_WEEK_RE = re.compile(r"(?i)\b(?:previous\s+week|the\s+week\s+before)\b")
_ROLLING_WEEK_RE = re.compile(r"(?i)\b(?:this\s+|the\s+)?(?:last|past)\s+week\b")
_THIS_WEEK_RE = re.compile(r"(?i)\bthis\s+week\b")
_YESTERDAY_RE = re.compile(r"(?i)\byesterday\b")
_TODAY_RE = re.compile(r"(?i)\btoday\b")


def _num(raw: str) -> int:
    raw = raw.lower()
    return _NUM_WORDS[raw] if raw in _NUM_WORDS else int(raw)


def local_today(now: datetime | None = None, tz: str = DEFAULT_TZ) -> date:
    """The calendar date in ``tz``. A naive ``now`` is read as already local."""
    zone = ZoneInfo(tz)
    if now is None:
        return datetime.now(zone).date()
    if now.tzinfo is None:
        return now.replace(tzinfo=zone).date()
    return now.astimezone(zone).date()


def calendar_week(d: date, back: int = 0) -> tuple[date, date]:
    """Monday..Sunday of the week containing ``d``, shifted ``back`` weeks earlier."""
    monday = d - timedelta(days=d.weekday()) - timedelta(days=7 * back)
    return monday, monday + timedelta(days=6)


def _win(kind: str, name: str, start: date, end: date) -> dict:
    return {
        "kind": kind,
        "name": name,
        "start": start,
        "end": end,
        "label": f"{name} ({start.isoformat()} to {end.isoformat()})",
    }


def resolve_relative_window(
    question: str, now: datetime | None = None, tz: str = DEFAULT_TZ
) -> dict | None:
    """Return {kind, name, start, end, label} for a relative time phrase, or None.

    ``start``/``end`` are inclusive local dates. See the module docstring for the
    convention. Absolute dates and month names are left to the caller.
    """
    q = re.sub(r"\s+", " ", (question or "").replace("’", "'")).strip()
    if not q:
        return None
    today = local_today(now, tz)

    m = _DAYS_RE.search(q)
    if m:
        n = max(1, _num(m.group(1)))
        return _win("rolling", f"last {n} days", today - timedelta(days=n - 1), today)

    m = _WEEKS_AGO_RE.search(q)
    if m:
        n = max(1, _num(m.group(1)))
        start, end = calendar_week(today, n)
        name = "the previous week" if n == 1 else f"{n} weeks ago"
        return _win("calendar", name, start, end)

    if _WEEK_BEFORE_LAST_RE.search(q):
        start, end = calendar_week(today, 2)
        return _win("calendar", "the week before last", start, end)

    if _PREVIOUS_WEEK_RE.search(q):
        start, end = calendar_week(today, 1)
        return _win("calendar", "the previous week", start, end)

    if _ROLLING_WEEK_RE.search(q):
        return _win("rolling", "last 7 days", today - timedelta(days=6), today)

    if _THIS_WEEK_RE.search(q):
        start, _ = calendar_week(today, 0)
        return _win("this_week", "this week", start, today)

    if _YESTERDAY_RE.search(q):
        d = today - timedelta(days=1)
        return _win("day", "yesterday", d, d)

    if _TODAY_RE.search(q):
        return _win("day", "today", today, today)
    return None


# Stray characters a reply should never open with. A single "-", "*", "+", "#",
# ">", "`", "|" or a digit is markdown/content and is kept.
_STRAY_LEAD = ".,;:!?…—–"


def strip_leading_punct(text: str) -> str:
    """Remove stray punctuation/whitespace at the very start of ``text``.

    Kept: markdown list markers ("- item", "* item", "1. item"), headings, quotes,
    code fences, tables, a leading decimal (".5"). Removed: ". ", ", ", "; ", ": ",
    "! ", "? ", "... ", "-- ", em/en dashes. Only the start is touched; "..." later
    in the text is left alone.
    """
    if not text:
        return text or ""
    t = text
    while True:
        s = t.lstrip()
        if not s:
            return ""
        if s.startswith("---"):
            return s  # markdown rule / front matter
        if s.startswith("--"):
            t = s[2:]
            continue
        c = s[0]
        if c in _STRAY_LEAD:
            if c == "." and len(s) > 1 and s[1].isdigit():
                return s
            t = s[1:]
            continue
        return s


def should_idle_quit(idle_s: float, partial_line: str | bool, drafts_waiting: bool | None) -> bool:
    """True only when an idle prompt should exit so queued background work can run.

    - a disabled timer (idle_s <= 0) never quits
    - anything typed but not yet sent (partial_line truthy) never quits
    - quit only when drafts_waiting is exactly True; False or None (unknown) keeps the session
    """
    if not idle_s or idle_s <= 0:
        return False
    if isinstance(partial_line, str):
        if partial_line.strip():
            return False
    elif partial_line:
        return False
    return drafts_waiting is True


# ---------------------------------------------------------------------------
# One window everywhere
# ---------------------------------------------------------------------------

def window_bounds(win: dict, tz: str = DEFAULT_TZ) -> tuple[float, float]:
    """Epoch seconds [start, end) for an inclusive local-date window from resolve_relative_window."""
    zone = ZoneInfo(tz)
    s = datetime(win["start"].year, win["start"].month, win["start"].day, tzinfo=zone)
    e = datetime(win["end"].year, win["end"].month, win["end"].day, tzinfo=zone) + timedelta(days=1)
    return s.timestamp(), e.timestamp()


def _day(d: date) -> str:
    return f"{d.strftime('%a %b')} {d.day}"


def window_span(win: dict) -> str:
    """Inclusive human span, e.g. 'Mon Nov 16 – Sun Nov 22 2026' (the end is the last day, not the day after)."""
    return f"{_day(win['start'])} – {_day(win['end'])} {win['end'].year}"


def in_window(d: date | datetime, win: dict) -> bool:
    if isinstance(d, datetime):
        d = d.date()
    return win["start"] <= d <= win["end"]


# ---------------------------------------------------------------------------
# Flight legs and trip reconcile
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON_RX = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DDMMMYY = re.compile(r"(?i)\b(\d{1,2})(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(\d{2})\b")
_LONG_DATE = re.compile(
    r"(?i)\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+" + _MON_RX + r"\s+(\d{1,2})(?!\d)(?:,?\s+(\d{4})(?!\d))?")
_WORDS = r"[A-Z][A-Za-z.'-]*(?: [A-Z][A-Za-z.'-]*){0,3}"
_LEG = re.compile(
    r"\b([A-Z]{2}|[A-Z]\d|\d[A-Z])\s?(\d{1,4})\b"                    # carrier + number, e.g. ZZ 2364
    r"(?:(?!\b(?:[A-Z]{2}|[A-Z]\d|\d[A-Z])\s?\d{1,4}\b).){0,90}?"   # never across the next flight number
    r"(" + _WORDS + r"),\s*" + _WORDS + r"(?:\s|<br\s*/?>)+(\d{1,2}:\d\d\s*[ap]\.?m\.?)"
    r"\s+(" + _WORDS + r"),\s*" + _WORDS + r"(?:\s|<br\s*/?>)+(\d{1,2}:\d\d\s*[ap]\.?m\.?)",
    re.S)
_CANCEL = re.compile(r"(?i)\b(cancel(?:l?ed|lation)?|refund(?:ed)?|void(?:ed)?)\b")
_CORP = re.compile(r"(?i)^(?:(?:inc|llc|ltd|co|corp|plc|operated|by)\.?\s+)+")


def _year_for(month: int, day: int, ref: date) -> int:
    """Year for a month/day with no year: the one closest to ref (mail about a trip is near the trip)."""
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((d - ref).days) < abs((best - ref).days):
            best = d
    return best.year if best else ref.year


def _dates_in(text: str, sent: date) -> list[tuple[int, date]]:
    """(position, date) for every '29NOV26' / 'Tuesday, November 17' in text, in order."""
    out = []
    for m in _DDMMMYY.finditer(text or ""):
        try:
            out.append((m.start(), date(2000 + int(m.group(3)), _MONTHS[m.group(2).lower()[:3]], int(m.group(1)))))
        except ValueError:
            pass
    for m in _LONG_DATE.finditer(text or ""):
        mo, dd = _MONTHS[m.group(1).lower()[:3]], int(m.group(2))
        yy = int(m.group(3)) if m.group(3) else _year_for(mo, dd, sent)
        try:
            out.append((m.start(), date(yy, mo, dd)))
        except ValueError:
            pass
    return sorted(out, key=lambda x: x[0])


def travel_date(text: str, sent: date) -> date | None:
    """The travel date a flight mail is about ('29NOV26', 'Tuesday, November 17'), not the day it was sent."""
    ds = _dates_in(text, sent)
    return ds[0][1] if ds else None


def is_cancellation(title: str, body: str = "") -> bool:
    """A cancel / refund / void notice: that itinerary was not travelled."""
    return bool(_CANCEL.search(title or "")) or bool(
        re.search(r"(?i)you(?:'ve| have)? (?:successfully )?cancel", (body or "")[:600]))


def _city(x: str) -> str:
    return _CORP.sub("", (x or "").strip())


def parse_flight_legs(text: str, sent: date | datetime) -> list[dict]:
    """Flight legs in an itinerary / check-in mail, each dated by the flight's own date.

    Returns [{date, flight, origin, dest, dep, arr}]. Each leg takes the nearest date
    written before it in the body ('Tuesday, November 17', '29NOV26'), so a round trip
    keeps its out and back days; the sent day is used only when the body names no date.
    """
    if isinstance(sent, datetime):
        sent = sent.date()
    t = " ".join(str(text or "").split())
    ds = _dates_in(t, sent)
    out = []
    for m in _LEG.finditer(t):
        before = [d for pos, d in ds if pos < m.start()]
        d = before[-1] if before else (ds[0][1] if ds else sent)
        out.append({"date": d, "flight": f"{m.group(1)} {m.group(2)}", "origin": _city(m.group(3)),
                    "dest": _city(m.group(5)), "dep": m.group(4).replace(" ", "").lower(),
                    "arr": m.group(6).replace(" ", "").lower()})
    return out


def _minutes(t: str | None) -> int:
    """'05:40pm' -> 1060; unknown -> -1 (sorts first in its day)."""
    m = re.match(r"(?i)\s*(\d{1,2}):(\d\d)\s*([ap])", t or "")
    if not m:
        return -1
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    return h * 60 + int(m.group(2))


def _norm(x: str) -> str:
    return re.sub(r"\s+", " ", (x or "").strip().lower())


def _is_home(place: str, home: set[str]) -> bool:
    p = _norm(place)
    return bool(p) and any(h in p for h in home)


def reconcile_trip(legs: list[dict], events: list[dict], home: set[str] | list[str],
                   max_after_days: int | None = None) -> dict:
    """Reconcile travel legs with calendar events that have a place.

    legs:   [{date, origin, dest, cancelled?, sent_at?, flight?, dep?, confirmed?}] - flights or rides
    events: [{title, start: date, end: date, place}]
    home:   lower-case names that mean "home" (city, airport code, suburb)

    Cancelled legs are dropped; for the same flight on the same day the latest notice wins.
    The trip runs from the first leg leaving home to the first later leg arriving home.
    Events elsewhere are sorted into on_site, partial (block runs past the trip home),
    after_home and before_trip. With max_after_days, single events starting more than that many
    days after the trip home are left out (a weekly room booking later says nothing about the trip).
    Travel records can show where you were; they cannot show
    whether a calendar item was attended remotely. So when a confirmed trip home comes
    before a calendar item elsewhere, nothing is classed "remote": the record carries a
    `conflict` (home date vs the calendar's away dates) and attendance stays unknown.
    If the arrival home is not confirmed, the range is not stretched to the calendar
    either; the same conflict is reported and marked uncertain.
    """
    home = {_norm(h) for h in home if h}
    live: dict = {}
    for lg in legs or []:
        if lg.get("cancelled"):
            continue
        key = (lg["date"], lg.get("flight") or (lg.get("origin"), lg.get("dest")))
        if key not in live or (lg.get("sent_at") or 0) >= (live[key].get("sent_at") or 0):
            live[key] = lg
    seq = sorted(live.values(), key=lambda x: (x["date"], _minutes(x.get("dep")), x.get("sent_at") or 0))
    start = home_leg = None
    for lg in seq:
        if start is None and _is_home(lg.get("origin", ""), home):
            start = lg
        elif start is not None and lg["date"] >= start["date"] and _is_home(lg.get("dest", ""), home):
            home_leg = lg
            break
    away_start = start["date"] if start else None
    home_date = home_leg["date"] if home_leg else None
    rec = {"away_start": away_start, "home_date": home_date, "return_legs": [], "out_legs": [],
           "on_site": [], "partial": [], "after_home": [], "before_trip": [], "uncertain": False, "note": ""}
    if start:
        rec["out_legs"] = [x for x in seq if x["date"] == start["date"] and not _is_home(x.get("dest", ""), home)]
    if home_leg:
        rec["return_legs"] = [x for x in seq if x["date"] == home_date and not _is_home(x.get("origin", ""), home)]
    away_ev = [e for e in events or [] if e.get("place") and not _is_home(e["place"], home)]
    for e in sorted(away_ev, key=lambda x: (x["start"], x.get("title", ""))):
        s, en = e["start"], e.get("end") or e["start"]
        if away_start and en < away_start:
            rec["before_trip"].append(e)
        elif home_date and s > home_date:
            if max_after_days is None or (s - home_date).days <= max_after_days:
                rec["after_home"].append(e)
        elif home_date and en > home_date:
            rec["partial"].append(e)
        elif away_start:
            rec["on_site"].append(e)
    confirmed = bool(home_leg) and home_leg.get("confirmed", True)
    rec["home_confirmed"] = confirmed
    rec["conflict"] = None
    later = rec["partial"] + rec["after_home"]
    if later:
        first_after = min(max(e["start"], home_date + timedelta(days=1)) if home_date else e["start"] for e in later)
        last = max(e.get("end") or e["start"] for e in later)
        rec["conflict"] = {"home_date": home_date, "from": first_after, "to": last, "events": later}
        rec["uncertain"] = True
        rec["note"] = ("travel records indicate home " + (_day(home_date) if home_date else "(date not seen)")
                       + ("" if confirmed else " (not confirmed)") + "; the calendar lists events elsewhere "
                       + _span(first_after, last) + "; attendance/location needs confirmation")
    return rec


def _span(a: date, b: date) -> str:
    return _day(a) if a == b else f"{_day(a)} through {_day(b)}"


def trip_summary(rec: dict, place: str = "") -> str:
    """One plain line from reconcile_trip, with explicit dates, safe to put in a prompt."""
    a, h = rec.get("away_start"), rec.get("home_date")
    if not a and not h:
        return ""

    def legs(ls):
        return ", ".join(f"{x.get('flight') or 'ride'} {x.get('origin', '')}→{x.get('dest', '')}"
                         + (f" {x['dep']}" if x.get("dep") else "") for x in ls)

    def ev(e):
        span = _day(e["start"]) + (f"–{_day(e['end'])}" if e.get("end") and e["end"] != e["start"] else "")
        return f"{span} {e.get('title', '')}".strip()

    s = "trip" + (f" to {place}" if place else "") + ": "
    s += (f"left {_day(a)}" if a else "start not seen") + " → " + (f"home {_day(h)}" if h else "no trip home seen yet")
    if rec.get("out_legs"):
        s += f" (out {_day(rec['out_legs'][0]['date'])}, all legs same day: {legs(rec['out_legs'])})"
    if rec.get("return_legs"):
        s += f" (back {_day(rec['return_legs'][0]['date'])}, all legs same day: {legs(rec['return_legs'])})"
    if rec.get("on_site"):
        s += ". on site: " + "; ".join(ev(e) for e in rec["on_site"])
    c = rec.get("conflict")
    if rec.get("partial") and h:
        s += f". on site through {_day(h)}" + ("" if c else " (calendar block continues: "
                                                + "; ".join(ev(e) for e in rec["partial"]) + ")")
    if c:
        s += (". SOURCES DISAGREE: flight/ride records indicate home " + (_day(c["home_date"]) if c["home_date"] else "(date not seen)")
              + "; the calendar lists events elsewhere " + _span(c["from"], c["to"]) + " ("
              + "; ".join(ev(e) for e in c["events"]) + "); attendance/location needs confirmation")
    return s + "."


def trip_lead(rec: dict, place: str = "") -> str:
    """One sentence for the start of an answer. Travel dates and calendar dates stay separate:
    a range is never stretched past what the travel records show."""
    a, h, c = rec.get("away_start"), rec.get("home_date"), rec.get("conflict")
    if not a and not h:
        return ""
    where = f" in {place}" if place else " away"
    if a and h:
        s = f"flight/ride records show you{where} from {_day(a)} to {_day(h)}"
    elif a:
        s = f"flight/ride records show you left for{(' ' + place) if place else ' a trip'} {_day(a)}; no trip home is seen yet"
    else:
        s = f"flight/ride records show you home {_day(h)}"
    if c:
        s += (f"; the calendar lists events{(' in ' + place) if place else ' elsewhere'} {_span(c['from'], c['to'])}"
              "; attendance/location for those needs confirmation")
    return s + "."


# ---------------------------------------------------------------------------
# People: what about, not how many
# ---------------------------------------------------------------------------

_SUBJ_PREFIX = re.compile(
    r"(?i)^\s*(?:(?:re|fw|fwd|aw|tr)\s*:\s*|\[[^\]]{0,24}\]\s*|(?:external|ext)\s*:\s*|"
    r"(?:cancel(?:l)?ed|accepted|declined|tentative|updated invitation|invitation)\s*:\s*)+")


def topic_of(subject: str, n: int = 60) -> str:
    """A short topic from a mail subject / meeting title: no Re:/Fw:/[tags]/Accepted:, no punctuation-only noise."""
    t = _SUBJ_PREFIX.sub("", " ".join(str(subject or "").split()))
    t = t.strip(" -:|")
    if not t or re.fullmatch(r"[\W\d_]+", t):
        return ""
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


def person_summary(name: str, topics: list[str], max_topics: int = 2, fallback: str = "") -> str:
    """'Name — topic; topic' (deduped, in the order given). Never a bare count."""
    seen, keep = set(), []
    for t in topics or []:
        t = topic_of(t)
        k = t.lower()
        if t and k not in seen:
            seen.add(k)
            keep.append(t)
        if len(keep) >= max_topics:
            break
    what = "; ".join(keep) or fallback
    return f"{name} — {what}" if what else name


# ---------------------------------------------------------------------------
# Stale sources: one closing note, never a dangling bullet
# ---------------------------------------------------------------------------

def stale_note(stale: list[tuple[str, float | datetime]], tz: str = DEFAULT_TZ) -> str:
    """'Note: personal mail last synced Sat Nov 21 9:23 PM CT; it may be missing recent items.'"""
    zone = ZoneInfo(tz)
    bits = []
    for name, when in stale or []:
        if not name:
            continue
        if isinstance(when, (int, float)) and when:
            when = datetime.fromtimestamp(when, zone)
        if isinstance(when, datetime):
            w = when.astimezone(zone) if when.tzinfo else when.replace(tzinfo=zone)
            abbr = "CT" if tz == "America/Chicago" else w.strftime("%Z")
            bits.append(f"{name} last synced {w.strftime('%a %b')} {w.day} {w.strftime('%-I:%M %p')} {abbr}")
        else:
            bits.append(f"{name} has not synced")
    if not bits:
        return ""
    return "Note: " + "; ".join(bits) + ("; it may" if len(bits) == 1 else "; they may") + " be missing recent items."


_STALE_WORDS = re.compile(r"(?i)\b(stale|not covered|out of date|not (?:been )?synced|last synced|marked stale)\b")


def strip_stale_mentions(answer: str) -> str:
    """Drop short lines / bullets whose point is that a source is stale (the closing note says it once)."""
    out = []
    for ln in (answer or "").splitlines():
        core = ln.strip().lstrip("-*• ").strip()
        if _STALE_WORDS.search(core) and len(core) <= 200:
            continue
        out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ---------------------------------------------------------------------------
# Output packing: a short answer, not a list of everything
# ---------------------------------------------------------------------------

_BULLET = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")


def cap_bullets(answer: str, max_bullets: int = 10, truncated: bool = False) -> str:
    """Keep the lead text and at most max_bullets top-level bullet lines; drop the rest.

    Indented sub-lines stay with their bullet. When the model hit its token cap (truncated=True),
    a last line that does not end like a sentence is dropped instead of shown half-written.
    """
    lines = (answer or "").splitlines()
    if truncated and lines and not re.search(r"[.!?)\]`]\s*$", lines[-1]):
        lines = lines[:-1]
    out, n, keep = [], 0, True
    for ln in lines:
        if _BULLET.match(ln) and not ln.startswith(("  ", "\t")):
            n += 1
            keep = n <= max_bullets
        elif not ln.strip():
            keep = keep or n <= max_bullets
        if keep:
            out.append(ln)
    return "\n".join(out).rstrip()
