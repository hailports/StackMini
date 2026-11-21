"""Regression: "what did I do this week" must include a trip even when tickets dominate the week."""
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hailports.brain import period as p

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 3, 13, 10, 0, tzinfo=TZ)  # a Friday
MON = datetime(2026, 3, 9, 9, 0, tzinfo=TZ).timestamp()


def _items():
    items = [{"source": "ado", "ts": MON + i * 300, "title": f"[Story {1000 + i}] ticket work", "participants": "you"}
             for i in range(600)]
    items += [
        {"source": "pcal", "ts": MON + 86400, "title": "Customer workshop @ Springfield office", "participants": "you"},
        {"source": "pmail", "ts": MON - 3600, "title": "Your AUS > SGF trip details", "participants": "Airline"},
        {"source": "pmail", "ts": MON + 86400, "title": "Thanks for staying at Springfield Inn", "participants": "Hotel"},
        {"source": "pmail", "ts": MON + 2 * 86400, "title": "Your verification code", "participants": "Some app"},
        {"source": "trip_ledger", "ts": MON, "title": "work-trip-springfield expense: Springfield Inn 300.00", "participants": ""},
    ]
    return items


@pytest.mark.parametrize("q", ["what'd i do this week", "what\u2019d i do this week", "whatd i do this week",
                               "what did i do this week"])
def test_contractions_are_activity_and_period_asks(q):
    assert p.is_activity_ask(q)
    assert p.is_period_ask(q, NOW)


def test_schedule_ask_is_not_activity():
    assert not p.is_activity_ask("what do i have this week")


def test_this_week_window_includes_sunday_before():
    t0, t1 = p.window("what'd i do this week", NOW)
    assert t0.weekday() == 6 and t0.day == 8 and t1 > NOW


def test_trip_survives_hundreds_of_tickets():
    t0, t1 = p.window("what'd i do this week", NOW)
    d = p.digest(_items(), (t0.timestamp(), t1.timestamp()), tz=TZ, me=re.compile(r"^you$"))
    flat = " ".join(i["title"] for v in d.values() for i in v).lower()
    assert "springfield" in flat
    assert any("springfield" in i["title"].lower() for i in d["trip ledgers"])
    assert len(d["tickets"]) <= 5
    assert "verification code" not in flat


def test_missing_sources_are_named_not_hidden():
    t0, t1 = p.window("what'd i do this week", NOW)
    win = (t0.timestamp(), t1.timestamp())
    newest = {"ado": NOW.timestamp(), "pmail": NOW.timestamp() - 5 * 86400, "pcal": NOW.timestamp()}
    cov = p.coverage(_items(), newest, win, NOW.timestamp())
    assert cov["texts"] == "EMPTY" and cov["personal mail"].startswith("STALE")
    line = p.gap_line(cov, answer="- you flew to SGF")
    assert "texts: EMPTY" in line and "personal mail: STALE" in line and "(not indexed)" in line
    assert "texts" not in p.gap_line(cov, answer="i don't see texts for this week")


def test_load_window_reads_fts_table(tmp_path):
    db = tmp_path / "ctx.db"
    c = sqlite3.connect(db)
    c.execute("CREATE VIRTUAL TABLE docs USING fts5(source UNINDEXED, ref UNINDEXED, ts UNINDEXED, url UNINDEXED, "
              "title, participants, body)")
    c.execute("INSERT INTO docs VALUES ('pmail','m1',?,'','Trip details','Airline','body')", (str(MON),))
    c.execute("INSERT INTO docs VALUES ('pmail','m0',?,'','Old','Airline','body')", (str(MON - 90 * 86400),))
    c.commit()
    c.close()
    rows = p.load_window(db, MON - 86400, MON + 86400)
    assert [r["ref"] for r in rows] == ["m1"]


def test_gap_line_needs_the_answer_to_say_the_source_is_missing():
    cov = {"personal mail": "STALE (900 in window)", "meeting notes": "STALE (7 in window)", "work digests": "300 in window"}
    line = p.gap_line(cov, not_indexed=[], answer="you had meetings all week [personal mail].")
    assert "personal mail: STALE" in line and "meeting notes: STALE" in line and "work digests" not in line
    line = p.gap_line(cov, not_indexed=[], answer="you had meetings.\n\npersonal mail is stale.")
    assert "personal mail" not in line and "meeting notes: STALE" in line
