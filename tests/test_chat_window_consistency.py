"""One window everywhere, trip dates that agree with the calendar, people topics, and the stale-source note."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from hailports.chat_window import (
    trip_lead,
    in_window,
    is_cancellation,
    parse_flight_legs,
    person_summary,
    reconcile_trip,
    resolve_relative_window,
    stale_note,
    strip_stale_mentions,
    topic_of,
    travel_date,
    trip_summary,
    window_bounds,
    window_span,
)

CT = ZoneInfo("America/Chicago")
SUNDAY = datetime(2026, 11, 22, 9, 33, tzinfo=CT)
HOME = {"home", "springfield", "spi"}


# --- window ---------------------------------------------------------------

def test_last_week_bounds_are_ct_midnights_and_label_is_inclusive():
    w = resolve_relative_window("what happened at work last week", now=SUNDAY)
    t0, t1 = window_bounds(w)
    assert datetime.fromtimestamp(t0, CT) == datetime(2026, 11, 16, tzinfo=CT)
    assert datetime.fromtimestamp(t1, CT) == datetime(2026, 11, 23, tzinfo=CT)  # exclusive end
    assert window_span(w) == "Mon Nov 16 – Sun Nov 22 2026"  # never "... Mon Nov 23"
    assert not in_window(date(2026, 11, 15), w)
    assert in_window(date(2026, 11, 16), w) and in_window(datetime(2026, 11, 22, 23, 59), w)


def test_utc_late_evening_does_not_shift_the_window():
    # 03:30 UTC Monday is still Sunday evening in CT: the window must not move a day
    from datetime import timezone
    utc = datetime(2026, 11, 23, 3, 30, tzinfo=timezone.utc)
    w = resolve_relative_window("last week", now=utc)
    assert (w["start"], w["end"]) == (date(2026, 11, 16), date(2026, 11, 22))


# --- flight legs ----------------------------------------------------------

CHECKIN = (
    "CHECK IN NOW Your flight on Wednesday, March 3 is available for check-in. TRIP Wednesday March 3"
    "DEPARTARRIVESEAT ZZ 2364 Operated by Example Air, Inc  Rivertown, Nebraska 05:40 pm  Hubcity, Georgia<br>08:50 pm "
    "01B ZZ 1335 Operated by Example Air, Inc  Hubcity, Georgia 09:55 pm  Springfield, Illinois<br>11:25 pm 11B"
)
ROUND_TRIP = (
    "Your trip SPI > RVT. Monday, March 1 ZZ 2976 Springfield, Illinois 10:59 am Hubcity, Georgia 02:06 pm "
    "ZZ 2313 Hubcity, Georgia 03:20 pm Rivertown, Nebraska 04:40 pm "
    "Wednesday, March 3 ZZ 2364 Rivertown, Nebraska 05:40 pm Hubcity, Georgia 08:58 pm "
    "ZZ 1335 Hubcity, Georgia 09:55 pm Springfield, Illinois 11:30 pm"
)


def test_legs_take_the_flight_date_not_the_sent_date():
    legs = parse_flight_legs(CHECKIN, date(2027, 3, 2))  # check-in mail arrives the day before
    assert [(x["date"], x["flight"], x["origin"], x["dest"]) for x in legs] == [
        (date(2027, 3, 3), "ZZ 2364", "Rivertown", "Hubcity"),
        (date(2027, 3, 3), "ZZ 1335", "Hubcity", "Springfield"),
    ]
    assert legs[0]["dep"] == "05:40pm" and legs[1]["arr"] == "11:25pm"


def test_round_trip_keeps_out_and_back_days():
    legs = parse_flight_legs(ROUND_TRIP, date(2027, 2, 27))
    assert [x["date"].day for x in legs] == [1, 1, 3, 3]


def test_receipt_code_date_and_cancellations():
    assert travel_date("Your Flight Receipt - PASSENGER 29NOV26", date(2026, 11, 17)) == date(2026, 11, 29)
    assert is_cancellation("Cancel Itinerary Confirmation")
    assert is_cancellation("Your Refund Confirmation Is Enclosed")
    assert is_cancellation("Trip update", "You've successfully canceled your trip.")
    assert not is_cancellation("It's Time To Check In For Your Flight")


# --- reconcile: the "home Tuesday but on site Wednesday" contradiction ---------

def _fixture():
    """Out Mon Mar 1, flights home Wed Mar 3 (with an older itinerary and a cancelled one in the pile),
    a 3-day offsite Wed-Fri and a demo booked in an away-city room on Thu."""
    legs = [
        {"date": date(2027, 3, 1), "origin": "home", "dest": "airport", "kind": "ride", "dep": "08:00am"},
        *[dict(x, kind="flight", sent_at=100) for x in parse_flight_legs(ROUND_TRIP, date(2027, 2, 27))],
        *[dict(x, kind="flight", sent_at=200) for x in parse_flight_legs(CHECKIN, date(2027, 3, 2))],
        {"date": date(2027, 3, 5), "origin": "Rivertown", "dest": "Springfield", "flight": "ZZ 9", "cancelled": True},
        {"date": date(2027, 3, 3), "origin": "airport", "dest": "home", "kind": "ride", "dep": "11:30pm"},
    ]
    events = [
        {"title": "Team offsite", "start": date(2027, 3, 3), "end": date(2027, 3, 5), "place": "Rivertown campus"},
        {"title": "Review", "start": date(2027, 3, 3), "end": date(2027, 3, 3), "place": "Rivertown campus"},
        {"title": "Phase-2 demo", "start": date(2027, 3, 4), "end": date(2027, 3, 4), "place": "Rivertown HQ room 3"},
        {"title": "Standup", "start": date(2027, 3, 4), "end": date(2027, 3, 4), "place": ""},
        {"title": "Dentist", "start": date(2027, 3, 4), "end": date(2027, 3, 4), "place": "Springfield"},
    ]
    return legs, events


def test_confirmed_trip_home_vs_later_calendar_is_a_conflict_not_remote():
    legs, events = _fixture()
    rec = reconcile_trip(legs, events, HOME)
    assert rec["away_start"] == date(2027, 3, 1) and rec["home_date"] == date(2027, 3, 3)
    assert [e["title"] for e in rec["on_site"]] == ["Review"]
    assert [e["title"] for e in rec["partial"]] == ["Team offsite"]
    assert [e["title"] for e in rec["after_home"]] == ["Phase-2 demo"]
    c = rec["conflict"]
    assert rec["uncertain"] and c["home_date"] == date(2027, 3, 3)
    assert c["from"] == date(2027, 3, 4) and c["to"] == date(2027, 3, 5)
    line = trip_summary(rec, place="Rivertown")
    assert "left Mon Mar 1" in line and "home Wed Mar 3" in line
    assert "on site: Wed Mar 3 Review" in line and "on site through Wed Mar 3" in line
    assert ("flight/ride records indicate home Wed Mar 3; the calendar lists events elsewhere "
            "Thu Mar 4 through Fri Mar 5") in line
    assert "attendance/location needs confirmation" in line
    # never an invented attendance mode
    assert "remote" not in line.lower() and "remote" not in rec["note"].lower()
    # the cancelled Mar 5 flight never extends the trip
    assert "Mar 5 ZZ 9" not in line


def test_unconfirmed_return_does_not_stretch_dates_to_the_calendar():
    legs = [{"date": date(2027, 3, 1), "origin": "home", "dest": "airport"},
            {"date": date(2027, 3, 3), "origin": "airport", "dest": "home", "confirmed": False}]
    events = [{"title": "Offsite", "start": date(2027, 3, 3), "end": date(2027, 3, 5), "place": "Rivertown"}]
    rec = reconcile_trip(legs, events, HOME)
    assert rec["uncertain"] and rec["home_date"] == date(2027, 3, 3) and not rec["home_confirmed"]
    line = trip_summary(rec)
    assert "home Wed Mar 3" in line and "Fri Mar 5" in line and "needs confirmation" in line
    assert "home Fri Mar 5" not in line and "remote" not in line.lower()


def test_event_after_home_only_is_a_conflict():
    legs = [{"date": date(2027, 3, 1), "origin": "home", "dest": "airport"},
            {"date": date(2027, 3, 2), "origin": "airport", "dest": "home"}]
    events = [{"title": "Demo", "start": date(2027, 3, 4), "end": date(2027, 3, 4), "place": "Rivertown HQ"}]
    rec = reconcile_trip(legs, events, HOME)
    line = trip_summary(rec)
    assert "indicate home Tue Mar 2; the calendar lists events elsewhere Thu Mar 4 (Thu Mar 4 Demo)" in line
    assert "remote" not in line.lower()


def test_no_conflict_when_calendar_ends_by_the_trip_home():
    legs = [{"date": date(2027, 3, 1), "origin": "home", "dest": "airport"},
            {"date": date(2027, 3, 3), "origin": "airport", "dest": "home"}]
    events = [{"title": "Offsite", "start": date(2027, 3, 2), "end": date(2027, 3, 3), "place": "Rivertown"}]
    rec = reconcile_trip(legs, events, HOME)
    assert rec["conflict"] is None and not rec["uncertain"]
    line = trip_summary(rec)
    assert "on site: Tue Mar 2–Wed Mar 3 Offsite" in line and "DISAGREE" not in line


def test_weekday_labels_come_from_the_date():
    legs = [{"date": date(2026, 11, 15), "origin": "home", "dest": "airport"},
            {"date": date(2026, 11, 17), "origin": "airport", "dest": "home"}]
    line = trip_summary(reconcile_trip(legs, [], HOME))
    assert "left Sun Nov 15" in line and "home Tue Nov 17" in line


def test_latest_notice_wins_for_the_same_flight():
    legs = [
        {"date": date(2027, 3, 3), "flight": "ZZ 1", "origin": "Rivertown", "dest": "Springfield", "dep": "05:00pm", "sent_at": 1},
        {"date": date(2027, 3, 3), "flight": "ZZ 1", "origin": "Rivertown", "dest": "Springfield", "dep": "07:00pm", "sent_at": 2},
        {"date": date(2027, 3, 1), "origin": "home", "dest": "airport"},
    ]
    rec = reconcile_trip(legs, [], HOME)
    assert rec["return_legs"][0]["dep"] == "07:00pm"


# --- people ---------------------------------------------------------------

def test_people_lines_say_what_about_not_how_many():
    assert topic_of("RE: [EXTERNAL] Fw: Spring meeting logistics") == "Spring meeting logistics"
    assert topic_of("Accepted: Weekly sync") == "Weekly sync"
    assert topic_of("RE: 12345") == ""
    s = person_summary("Pat", ["Re: Spring meeting logistics", "spring meeting logistics", "Budget review"])
    assert s == "Pat — Spring meeting logistics; Budget review"
    assert person_summary("Lee", [], fallback="zoom chat") == "Lee — zoom chat"
    assert not any(ch.isdigit() for ch in person_summary("Kim", ["Re: hello"]))


# --- stale note -----------------------------------------------------------

def test_stale_note_is_one_clear_line_with_ct_time():
    n = stale_note([("personal mail", datetime(2026, 11, 21, 21, 23, tzinfo=CT))])
    assert n == "Note: personal mail last synced Sat Nov 21 9:23 PM CT; it may be missing recent items."
    assert stale_note([]) == ""
    two = stale_note([("a", datetime(2026, 11, 21, 8, 0, tzinfo=CT)), ("b", None)])
    assert two.endswith("they may be missing recent items.") and "b has not synced" in two


def test_strip_stale_mentions_drops_the_dangling_bullet_only():
    ans = "you were busy.\n- met Pat about logistics\n- personal mail is marked stale\n- closed 2 tickets"
    out = strip_stale_mentions(ans)
    assert "stale" not in out and "met Pat" in out and "closed 2 tickets" in out


# --- output packing ---------------------------------------------------------

def test_cap_bullets_keeps_lead_and_first_n():
    from hailports.chat_window import cap_bullets
    ans = "for the last 7 days: busy week.\n" + "\n".join(f"- item {i}" for i in range(1, 25))
    out = cap_bullets(ans, 10)
    assert out.splitlines()[0] == "for the last 7 days: busy week."
    assert "- item 10" in out and "- item 11" not in out
    assert out.count("\n- ") == 10


def test_cap_bullets_drops_half_written_last_line_when_truncated():
    from hailports.chat_window import cap_bullets
    ans = "lead.\n- one done.\n  - sub stays\n- two half writ"
    assert cap_bullets(ans, 10, truncated=True) == "lead.\n- one done.\n  - sub stays"
    assert cap_bullets(ans, 10) == ans


def test_max_after_days_drops_later_room_bookings_from_the_conflict():
    legs = [{"date": date(2027, 3, 1), "origin": "home", "dest": "airport"},
            {"date": date(2027, 3, 2), "origin": "airport", "dest": "home"}]
    events = [{"title": "Demo", "start": date(2027, 3, 3), "end": date(2027, 3, 3), "place": "Rivertown HQ"},
              {"title": "Weekly room", "start": date(2027, 3, 8), "end": date(2027, 3, 8), "place": "Rivertown HQ"}]
    rec = reconcile_trip(legs, events, HOME, max_after_days=3)
    assert [e["title"] for e in rec["after_home"]] == ["Demo"]
    assert rec["conflict"]["to"] == date(2027, 3, 3) and "Weekly room" not in trip_summary(rec)


def test_trip_lead_keeps_travel_and_calendar_dates_apart():
    legs, events = _fixture()
    rec = reconcile_trip(legs, events, HOME)
    lead = trip_lead(rec, "Rivertown")
    assert lead == ("flight/ride records show you in Rivertown from Mon Mar 1 to Wed Mar 3; the calendar lists "
                    "events in Rivertown Thu Mar 4 through Fri Mar 5; attendance/location for those needs confirmation.")
    assert "remote" not in lead.lower()
    plain = reconcile_trip([{"date": date(2027, 3, 1), "origin": "home", "dest": "airport"},
                            {"date": date(2027, 3, 3), "origin": "airport", "dest": "home"}], [], HOME)
    assert trip_lead(plain) == "flight/ride records show you away from Mon Mar 1 to Wed Mar 3."
    assert trip_lead(reconcile_trip([], [], HOME)) == ""
