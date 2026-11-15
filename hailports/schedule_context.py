"""Schedule-intent detection + calendar context block for the local chat.

Reads a local calendar snapshot JSON (written by a periodic sync job) and
renders a compact, dated block in the user's time zone. Read-only: this
module never writes to any calendar.

Snapshot shape (one file, refreshed every few minutes by the sync job):
  {"sync_epoch": <unix>, "events": [{"subject", "start", "end",
    "is_all_day", "cancelled", "my_response", "show_as", "location"}]}
start/end are ISO-8601 UTC ("...Z").

Env:
  SCHEDULE_SNAPSHOT   path to the snapshot JSON (default below)
  SCHEDULE_TZ         display time zone, IANA name (default: machine local zone)
  SCHEDULE_DAYS       forward window in days (default 10)
  SCHEDULE_CONTEXT=0  turn the feature off
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SNAPSHOT = Path(
    os.environ.get(
        "SCHEDULE_SNAPSHOT",
        str(Path.home() / ".hailports/calendar_snapshot.json"),
    )
)
TZ_NAME = os.environ.get("SCHEDULE_TZ", "")  # empty = machine local zone
DAYS = int(os.environ.get("SCHEDULE_DAYS", "10"))
STALE_S = int(os.environ.get("SCHEDULE_STALE_S", str(6 * 3600)))
MAX_LINES = int(os.environ.get("SCHEDULE_MAX_LINES", "70"))

_SCHEDULE_RE = re.compile(
    r"(?i)("
    r"\b(what('?s| is| do i have)|anything)\b.{0,30}\b(going on|on (my )?(plate|calendar|schedule)|coming up|planned|happening)\b"
    r"|\bwhat do i have\b"
    r"|\b(my|the) (calendar|schedule|agenda|week|day)\b"
    r"|\b(am i|are we) (traveling|travelling|flying|going anywhere|out|ooo|off)\b"
    r"|\b(any|my) (trips?|travel|flights?|meetings?)\b"
    r"|\bwhere am i (going|flying|headed)\b"
    r"|\b(meetings?|calls?) (today|tomorrow|this week|next week)\b"
    r"|\bam i (free|busy)\b"
    r")"
)
_WHEN_RE = re.compile(r"(?i)\b(today|tonight|tomorrow|this week|next week|week|weekend|monday|tuesday|wednesday|thursday|friday|upcoming|coming up)\b")
_TRAVEL_RE = re.compile(r"(?i)\b(flight|fly|flying|travel|trip|hotel|airport|onsite|on-site|in[- ]person|offsite|off-site|ooo|out of office|pto|vacation|conference|summit|team meeting|meet and greet)\b")
_VIRTUAL_LOC_RE = re.compile(r"(?i)(microsoft teams|teams meeting|zoom|webex|meet\.google|https?://|personal meeting room|^\s*$)")


def physical_location(loc: str) -> str:
    """Keep only the in-person part of a location; drop meeting links (they carry passcodes)."""
    parts = [p.strip() for p in re.split(r"[;\n]", str(loc or ""))]
    keep = [re.sub(r"\s+", " ", p) for p in parts if p and not _VIRTUAL_LOC_RE.search(p)]
    keep = [re.sub(r"(?i)\s*-\s*p\w*se accept or decline.*$", "", p).strip() for p in keep]
    return "; ".join(p for p in keep if p)[:80]


def is_schedule_ask(text: str) -> bool:
    if os.environ.get("SCHEDULE_CONTEXT", "1") == "0":
        return False
    t = (text or "").strip()
    if not t:
        return False
    # topic questions ("what's going on with ticket 12345 this week") are not schedule asks
    if re.search(r"\b\d{5,}\b", t) or re.search(r"(?i)\bgoing on (with|in|on) (?!my\b)", t):
        return False
    if re.search(r"(?i)\b(am i|are we) (traveling|travelling|flying)\b|\bwhere am i (going|flying|headed)\b|\b(my|any) (trips?|travel|flights?)\b", t):
        return True
    return bool(_SCHEDULE_RE.search(t) and (_WHEN_RE.search(t) or re.search(r"(?i)calendar|schedule|agenda|going on", t)))


def _tz():
    return ZoneInfo(TZ_NAME) if TZ_NAME else datetime.now().astimezone().tzinfo


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def load_events(days: int | None = None, *, now: datetime | None = None) -> tuple[list[dict], dict]:
    """Return (events in window, meta). Raises on unreadable snapshot."""
    days = days or DAYS
    raw = json.loads(SNAPSHOT.read_text())
    tz = _tz()
    now = now or datetime.now(tz)
    day0 = datetime(now.year, now.month, now.day, tzinfo=tz)
    end = day0 + timedelta(days=days)
    out = []
    for ev in raw.get("events") or []:
        if not isinstance(ev, dict) or ev.get("cancelled"):
            continue
        if str(ev.get("my_response") or "").lower() == "declined":
            continue
        try:
            s, e = _parse(ev["start"]).astimezone(tz), _parse(ev["end"]).astimezone(tz)
        except Exception:
            continue
        if e <= now or s >= end:
            continue
        out.append({**ev, "_s": s, "_e": e})
    out.sort(key=lambda x: (x["_s"], x["_e"]))
    age = time.time() - float(raw.get("sync_epoch") or 0)
    return out, {"age_s": age, "stale": age > STALE_S, "now": now, "end": end}


def _is_travel(ev: dict) -> bool:
    dur_h = (ev["_e"] - ev["_s"]).total_seconds() / 3600
    physical = bool(physical_location(ev.get("location") or ""))
    return dur_h >= 8 or bool(ev.get("is_all_day")) or bool(_TRAVEL_RE.search(str(ev.get("subject") or ""))) or (physical and dur_h >= 3) or str(ev.get("show_as") or "").lower() == "oof"


def _fmt(ev: dict, tz) -> str:
    s, e = ev["_s"], ev["_e"]
    subj = re.sub(r"\s+", " ", str(ev.get("subject") or "(no title)")).strip()[:90]
    loc = physical_location(ev.get("location") or "")
    if ev.get("is_all_day"):
        when = s.strftime("%a %b %-d") + ("" if (e - s) <= timedelta(days=1) else " to " + (e - timedelta(minutes=1)).strftime("%a %b %-d")) + " all day"
    elif s.date() != e.date():
        when = s.strftime("%a %b %-d %-I:%M%p") + " to " + e.strftime("%a %b %-d %-I:%M%p")
    else:
        when = s.strftime("%a %b %-d %-I:%M%p") + "-" + e.strftime("%-I:%M%p")
    bits = [when.replace("AM", "am").replace("PM", "pm"), subj]
    if loc:
        bits.append("@ " + loc)
    resp = str(ev.get("my_response") or "")
    if resp.lower().startswith("tentative"):
        bits.append("(tentative)")
    return " · ".join(bits)


def calendar_block(days: int | None = None) -> str:
    """Compact dated block. Raises if the snapshot can't be read."""
    tz = _tz()
    events, meta = load_events(days)
    now = meta["now"]
    head = (
        f"CALENDAR (real events from the local calendar snapshot; times {TZ_NAME or 'local'}, "
        f"now {now.strftime('%a %b %-d %-I:%M%p').replace('AM','am').replace('PM','pm')}, "
        f"window to {meta['end'].strftime('%a %b %-d')}; synced {int(meta['age_s']//60)} min ago"
        + ("; STALE" if meta["stale"] else "") + ")"
    )
    if not events:
        return head + "\nno events in this window."
    travel = [e for e in events if _is_travel(e)]
    lines = [head]
    if travel:
        lines.append("TRAVEL / MULTI-DAY / OUT-OF-OFFICE:")
        lines += ["- " + _fmt(e, tz) for e in travel]
    # On-site summary: venues listed on events that fall inside a multi-day block (+/- 1 day).
    for blk in [e for e in travel if (e["_e"] - e["_s"]) >= timedelta(hours=8)]:
        lo, hi = blk["_s"] - timedelta(days=1), blk["_e"] + timedelta(days=1)
        venues = []
        for e in events:
            v = physical_location(e.get("location") or "")
            if v and lo <= e["_s"] <= hi and v not in venues:
                venues.append(v)
        if venues:
            lines.append(
                "ON-SITE DURING '" + str(blk.get("subject") or "")[:50].strip() + "' ("
                + blk["_s"].strftime("%a %b %-d") + " to " + blk["_e"].strftime("%a %b %-d")
                + "), venues on events: " + " | ".join(venues[:6])
            )
    places = [e for e in events if physical_location(e.get("location") or "")]
    if places:
        lines.append("PHYSICAL LOCATIONS LISTED ON EVENTS (rooms/venues; links removed):")
        lines += ["- " + e["_s"].strftime("%a %b %-d") + ": " + physical_location(e.get("location") or "") + " (" + str(e.get("subject") or "")[:50].strip() + ")" for e in places]
    lines.append("MEETINGS BY DAY:")
    cur = None
    noise = re.compile(r"(?i)^(lunch|focus|hold|block)\b|hard block")
    for e in events:
        if e in travel or noise.search(str(e.get("subject") or "")):
            continue
        d = e["_s"].strftime("%a %b %-d")
        if d != cur:
            lines.append(d + ":")
            cur = d
        lines.append("  - " + _fmt(e, tz).replace(d + " ", "", 1))
        if len(lines) >= MAX_LINES:
            lines.append("  …(more)")
            break
    return "\n".join(lines)


INSTRUCT = (
    "This is a schedule question. Speak to the user as 'you' (never 'i'). Lead with the CALENDAR block. First sentence: any travel, "
    "multi-day, on-site, or out-of-office item, saying where and which days. If ON-SITE or "
    "PHYSICAL LOCATIONS name a city or office (for example '<City> HQ'), say plainly that he is "
    "in that city those days. Then list the key meetings (up to 8) with weekday and time in "
    "local time; always say the start time, for example 'Monday 9am Team sync, 1pm Design review'. Then one or two sentences on WORK ITEMS if any. Use only "
    "events and places that appear in CALENDAR; never invent a trip, city, or meeting. If "
    "CALENDAR says STALE, say the calendar may be out of date."
)

SYSTEM = (
    "You are the user's local assistant answering a schedule question from real calendar data. "
    "Format:\n"
    "1) One opening sentence on travel / on-site / out-of-office: where and which days. If ON-SITE "
    "or PHYSICAL LOCATIONS name a city or office (for example '<City> HQ'), say plainly that the "
    "user is in that city those days.\n"
    "2) A short day-by-day list of the key meetings for the asked window (default: the next 7 "
    "days), one line per day, each meeting with its start time in local time, for example "
    "'- Mon: 9:00am Team sync, 1:00pm Design review'. Skip lunch blocks.\n"
    "3) One or two sentences on WORK ITEMS, if any.\n"
    "Speak to the user as 'you'. Use only events and places that appear in CALENDAR; never invent "
    "a trip, city, time, or meeting. If CALENDAR says STALE, say the calendar may be out of date."
)


def sanitize_location(loc: str) -> str:
    """For sync jobs writing the snapshot: keep rooms/venues, drop meeting links (passcodes)."""
    return "; ".join(p.strip() for p in str(loc or "").split(";") if p.strip() and "://" not in p)[:200]
