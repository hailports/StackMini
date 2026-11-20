"""Period questions ("what did I do this week") answered from every source, balanced.

A question like "what'd i do this week" has no search words: every word is a stop word. A keyword
retriever then finds nothing in the mail, calendar, travel or expense stores, and the answer is built from
whatever else is nearby (usually tickets). This module handles that case without keywords:

* ``is_activity_ask`` / ``is_period_ask`` recognise the question, including contractions ("what'd",
  "whatd") and phone apostrophes.
* ``digest`` takes every item dated inside the window and gives each source family its own quota, so
  trips, calendar, receipts and texts are never crowded out by hundreds of tickets. Items are spread
  across the days of the window and the user's own items come first.
* ``coverage`` says, per source, how many items are in the window and whether the source is STALE, EMPTY,
  or not indexed at all, and ``gap_line`` turns that into one closing line, so the answer names what it
  could not see instead of silently leaving it out.

Pure functions over plain dicts; ``load_window`` reads an FTS5 ``docs`` table
(source, ref, ts, url, title, participants, body). No model calls, no network.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

PERIOD_ASK = re.compile(
    r"(?i)\b(what (did|have|has|was) i|what i did|what have i been|been (up to|doing)|was i doing|"
    r"my (week|day|month|weekend)|recap|summar\w*|catch me up|wrap ?up|what happened|"
    r"how was (my|the) (week|day|weekend)|where did i go|where have i been)\b")
_PAST = re.compile(r"(?i)\b(did|went|was|were|been|had|met|saw|visited|flew|travel\w*|trips?|took|spent|stayed|talked|texted|spoke)\b")
_SELF = re.compile(r"(?i)\b(i|me|my|i'?ve)\b")
NOISE_TITLE = re.compile(
    r"(?i)verification code|\bsign[- ]?in\b|new login|unrecognized device|password|newsletter|daily digest|% off|"
    r"\bsale\b|\bdeals?\b|\boffers?\b|webinar|unsubscribe|e?statement|promo|survey|automatic reply|out of office")
MAIL_SOURCES = {"email", "pmail", "email_attachment"}


@dataclass(frozen=True)
class Family:
    label: str
    sources: tuple[str, ...]
    quota: int
    stale_hours: float | None = None


DEFAULT_FAMILIES = (
    Family("calendar", ("calendar", "pcal"), 6, 72),
    Family("work mail", ("email",), 7, 12),
    Family("personal mail", ("pmail",), 5, 12),
    Family("texts", ("imessage",), 3, 96),
    Family("chat", ("zoom",), 5, 24),
    Family("meeting notes", ("meeting",), 4, 48),
    Family("tickets", ("ado", "ticket"), 5, 24),
    Family("code", ("github_pr", "github_commit"), 4, 48),
    Family("documents", ("sp_corpus", "pfile"), 3, 72),
    Family("trip ledgers", ("trip_ledger",), 4, None),
)
DEFAULT_NOT_INDEXED = ("team chat app (not connected)", "expense system (only local trip ledgers are read)")


def normalize(text: str) -> str:
    t = (text or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"(?i)\b(what|where|who|when|how)(?:'d|d)\s+(i|we)\b", r"\1 did \2", t)


def window(question: str, now: datetime) -> tuple[datetime, datetime] | None:
    t = normalize(question).lower()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r"\btoday\b", t):
        return day0, now
    if "yesterday" in t:
        return day0 - timedelta(days=1), day0
    if re.search(r"\blast week\b", t):
        start = day0 - timedelta(days=day0.weekday() + 7)
        return start, start + timedelta(days=7)
    if re.search(r"\bthis week\b|\bmy week\b", t):
        return day0 - timedelta(days=day0.weekday() + 1), now + timedelta(days=1)  # include the Sunday before
    if re.search(r"\bthis month\b|\bmy month\b", t):
        return day0.replace(day=1), now + timedelta(days=1)
    m = re.search(r"\blast (\d{1,3}) days\b", t)
    if m:
        return now - timedelta(days=int(m.group(1))), now + timedelta(days=1)
    return None


def is_activity_ask(question: str) -> bool:
    t = normalize(question)
    return bool(PERIOD_ASK.search(t) and _PAST.search(t) and _SELF.search(t))


def is_period_ask(question: str, now: datetime) -> bool:
    return window(question, now) is not None and bool(PERIOD_ASK.search(normalize(question)))


def _keep(item: dict) -> bool:
    title = " ".join(str(item.get("title") or "").split())
    if not title:
        return False
    return not (item.get("source") in MAIL_SOURCES and NOISE_TITLE.search(title))


def digest(items: list[dict], win: tuple[float, float], families=DEFAULT_FAMILIES, tz: tzinfo | None = None,
           me: re.Pattern | None = None) -> dict[str, list[dict]]:
    """Per family: up to ``quota`` items inside the window, deduped, round-robin over days, own items first."""
    t0, t1 = win
    out: dict[str, list[dict]] = {}
    for fam in families:
        pool = [i for i in items if i.get("source") in fam.sources and t0 <= float(i.get("ts") or 0) <= t1 and _keep(i)]
        mine = (lambda i: bool(me and me.search(str(i.get("participants") or "").split(";")[0])))
        seen, uniq = set(), []
        for it in sorted(pool, key=lambda i: (not mine(i), -float(i["ts"]))):
            key = (str(it["title"])[:60].lower(), datetime.fromtimestamp(float(it["ts"]), tz).date())
            if key not in seen:
                seen.add(key)
                uniq.append(it)
        by_day: dict = {}
        for it in uniq:
            by_day.setdefault(datetime.fromtimestamp(float(it["ts"]), tz).date(), []).append(it)
        picked: list[dict] = []
        while len(picked) < fam.quota and any(by_day.values()):
            for d in sorted(by_day):
                if by_day[d] and len(picked) < fam.quota:
                    picked.append(by_day[d].pop(0))
        out[fam.label] = sorted(picked, key=lambda i: float(i["ts"]))
    return out


def coverage(items: list[dict], newest: dict[str, float], win: tuple[float, float], now: float,
             families=DEFAULT_FAMILIES) -> dict[str, str]:
    """``newest`` maps source -> newest past timestamp in the store (0 or missing = nothing indexed)."""
    t0, t1 = win
    out = {}
    for fam in families:
        n = sum(1 for i in items if i.get("source") in fam.sources and t0 <= float(i.get("ts") or 0) <= t1)
        last = max((newest.get(s) or 0.0) for s in fam.sources)
        if not last:
            out[fam.label] = "EMPTY"
        elif fam.stale_hours is not None and now - last > fam.stale_hours * 3600:
            out[fam.label] = f"STALE ({n} in window)"
        else:
            out[fam.label] = f"{n} in window"
    return out


def gap_line(cov: dict[str, str], not_indexed=DEFAULT_NOT_INDEXED, answer: str = "") -> str:
    """One closing line naming every source the answer could not see; empty when nothing is missing."""
    low = (answer or "").lower()
    gaps = [f"{k}: {v.split(' (')[0]}" for k, v in cov.items() if v.startswith(("STALE", "EMPTY", "UNREACHABLE"))]
    gaps += [f"{x.split(' (')[0]} (not indexed)" for x in not_indexed]
    gaps = [g for g in gaps if g.split(":")[0].split(" (")[0].lower() not in low]
    return ("not covered for this period: " + "; ".join(gaps) + ".") if gaps else ""


def load_window(db: Path, t0: float, t1: float) -> list[dict]:
    """Every doc dated inside the window, read from the FTS5 content table (fast) or the docs table."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        try:
            rows = c.execute("SELECT c0, c1, cast(c2 as real), c4, c5 FROM docs_content "
                             "WHERE cast(c2 as real) BETWEEN ? AND ?", (t0, t1)).fetchall()
        except sqlite3.OperationalError:
            rows = c.execute("SELECT source, ref, cast(ts as real), title, participants FROM docs "
                             "WHERE cast(ts as real) BETWEEN ? AND ?", (t0, t1)).fetchall()
    finally:
        c.close()
    return [{"source": s, "ref": r, "ts": t, "title": ti, "participants": p} for s, r, t, ti, p in rows]
