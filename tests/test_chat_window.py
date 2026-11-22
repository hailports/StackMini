"""Relative chat windows, stray leading punctuation, and the idle-quit decision."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from hailports.chat_window import (
    local_today,
    resolve_relative_window,
    should_idle_quit,
    strip_leading_punct,
)

CT = ZoneInfo("America/Chicago")
SUNDAY = datetime(2026, 11, 22, 9, 19, tzinfo=CT)  # a Sunday
WEDNESDAY = datetime(2026, 11, 18, 15, 0, tzinfo=CT)


def _span(q, now=SUNDAY):
    w = resolve_relative_window(q, now=now)
    assert w is not None, q
    return w["start"].isoformat(), w["end"].isoformat()


@pytest.mark.parametrize(
    "q",
    [
        "what happened this last week at work",
        "what did i do last week",
        "the last week",
        "anything in the past week?",
        "this past week",
        "does my last week show anything about a trip",
    ],
)
def test_last_week_is_rolling_seven_days(q):
    assert _span(q) == ("2026-11-16", "2026-11-22")


def test_rolling_label_names_dates():
    w = resolve_relative_window("this last week", now=SUNDAY)
    assert w["label"] == "last 7 days (2026-11-16 to 2026-11-22)"
    assert w["kind"] == "rolling"


def test_this_week_is_monday_to_today():
    assert _span("what's on this week", now=WEDNESDAY) == ("2026-11-16", "2026-11-18")
    assert _span("this week") == ("2026-11-16", "2026-11-22")


def test_calendar_week_phrases():
    assert _span("the previous week") == ("2026-11-09", "2026-11-15")
    assert _span("and the week before?") == ("2026-11-09", "2026-11-15")
    assert _span("the week before last") == ("2026-11-02", "2026-11-08")
    assert _span("2 weeks ago") == ("2026-11-02", "2026-11-08")
    assert _span("two weeks ago") == ("2026-11-02", "2026-11-08")
    assert _span("one week ago", now=WEDNESDAY) == ("2026-11-09", "2026-11-15")


def test_past_n_days():
    assert _span("past 3 days") == ("2026-11-20", "2026-11-22")
    assert _span("the last 10 days") == ("2026-11-13", "2026-11-22")
    assert _span("last 1 day") == ("2026-11-22", "2026-11-22")
    assert _span("last fourteen days") == ("2026-11-09", "2026-11-22")


def test_single_days():
    assert _span("yesterday") == ("2026-11-21", "2026-11-21")
    assert _span("what's on today") == ("2026-11-22", "2026-11-22")


def test_no_window():
    assert resolve_relative_window("who owns the portal", now=SUNDAY) is None
    assert resolve_relative_window("", now=SUNDAY) is None
    assert resolve_relative_window("last weekend plans", now=SUNDAY) is None


def test_today_is_local_not_utc():
    # 04:30 UTC Monday is still Sunday evening in Chicago.
    late = datetime(2026, 11, 23, 4, 30, tzinfo=ZoneInfo("UTC"))
    assert local_today(late) == date(2026, 11, 22)
    assert _span("last week", now=late) == ("2026-11-16", "2026-11-22")
    # A naive now is read as local time.
    assert local_today(datetime(2026, 11, 22, 23, 59)) == date(2026, 11, 22)


@pytest.mark.parametrize(
    "raw, want",
    [
        (". what's up?", "what's up?"),
        ("  , hi there", "hi there"),
        ("! ok", "ok"),
        ("-- here you go", "here you go"),
        ("— sure", "sure"),
        ("... and then", "and then"),
        (";: x", "x"),
        ("what's up?", "what's up?"),
        ("- first item\n- second", "- first item\n- second"),
        ("* bold list", "* bold list"),
        ("1. step one", "1. step one"),
        ("# heading", "# heading"),
        ("```py\nx\n```", "```py\nx\n```"),
        ("---\nfront", "---\nfront"),
        (".5 is a decimal", ".5 is a decimal"),
        ("wait... what", "wait... what"),
        ("", ""),
        (" .  ", ""),
    ],
)
def test_strip_leading_punct(raw, want):
    assert strip_leading_punct(raw) == want


def test_idle_quit_decision():
    # nothing typed, drafts waiting -> quit
    assert should_idle_quit(300, "", True) is True
    # nothing waiting -> stay
    assert should_idle_quit(300, "", False) is False
    # unknown -> stay (fail-open)
    assert should_idle_quit(300, "", None) is False
    # partially typed line -> stay, even with drafts waiting
    assert should_idle_quit(300, "was i in", True) is False
    assert should_idle_quit(300, True, True) is False
    # whitespace only counts as empty
    assert should_idle_quit(300, "   ", True) is True
    # timer disabled -> never
    assert should_idle_quit(0, "", True) is False
