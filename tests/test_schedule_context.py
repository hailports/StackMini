import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from hailports import schedule_context as sc


@pytest.mark.parametrize("q,want", [
    ("what do i have going on this week", True),
    ("what's on my calendar tomorrow", True),
    ("am i traveling next week", True),
    ("any meetings today?", True),
    ("what's going on with ticket 12345 this week", False),
    ("what's going on with the portal this week", False),
    ("what did i do last week", False),
    ("summarize PR 42", False),
])
def test_intent(q, want):
    assert sc.is_schedule_ask(q) is want


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_block(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    d1 = (now + timedelta(days=1)).replace(hour=14, minute=0, second=0)
    ev = [
        {"subject": "Team offsite", "start": _iso(d1), "end": _iso(d1 + timedelta(days=2)),
         "is_all_day": False, "cancelled": False, "my_response": "Accepted", "show_as": "Busy",
         "location": "Example City HQ Room 1"},
        {"subject": "Design review", "start": _iso(d1 + timedelta(hours=2)), "end": _iso(d1 + timedelta(hours=3)),
         "is_all_day": False, "cancelled": False, "my_response": "Accepted", "show_as": "Busy",
         "location": "https://meet.example/j/1"},
        {"subject": "Cancelled thing", "start": _iso(d1), "end": _iso(d1 + timedelta(hours=1)),
         "cancelled": True},
    ]
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"sync_epoch": time.time(), "events": ev}))
    monkeypatch.setattr(sc, "SNAPSHOT", snap)
    monkeypatch.setattr(sc, "TZ_NAME", "UTC")
    out = sc.calendar_block(7)
    assert "TRAVEL / MULTI-DAY" in out and "Team offsite" in out
    assert "Example City HQ" in out and "ON-SITE DURING" in out
    assert "Design review" in out and "meet.example" not in out and "Cancelled thing" not in out
    assert "STALE" not in out


def test_missing_snapshot_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "SNAPSHOT", tmp_path / "nope.json")
    with pytest.raises(FileNotFoundError):
        sc.calendar_block()


def test_sanitize_location():
    assert sc.sanitize_location("Room 2; https://x.example/j/1") == "Room 2"
