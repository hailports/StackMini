"""Mail reads keep an unread badge, and a stored decision is not judged again."""

from hailports.draft_runner import decision_cached, run_round
from hailports.plugins.mail import read_message


def test_unread_read_is_restored_once_and_a_read_message_stays_read():
    calls = {"restore": 0}

    def fetch():
        return "hello"

    def restore():
        calls["restore"] += 1

    unread = read_message(False, fetch, restore)
    assert unread["restored"] is True
    assert unread["body"] == "hello"
    assert calls["restore"] == 1

    read_message(False, fetch, restore)
    assert calls["restore"] == 2

    left = read_message(True, fetch, restore)
    assert left["restored"] is False
    assert calls["restore"] == 2


def test_a_decided_message_is_not_read_again():
    fetches = {"n": 0}

    def fetch():
        fetches["n"] += 1
        return "body text that is long enough to draft from"

    row = {
        "message_id": "m-1",
        "subject": "RE: Project update",
        "from": "Pat Lee",
        "content_hash": "h1",
        "fetch_body": fetch,
    }
    prior = {"m-1": {"decision": "no_reply_needed", "decision_hash": "h1"}}
    first = run_round([row], prior=prior)
    assert fetches["n"] == 0
    assert first["drafted"] == 0
    assert len(first["log"]) == 1
    assert "decision_no_reply_needed" in first["log"][0]

    second = run_round([row], prior=first["prior"])
    assert fetches["n"] == 0
    assert second["log"] == []
    assert second["drafted"] == 0


def test_a_changed_hash_is_read():
    fetches = {"n": 0}

    def fetch():
        fetches["n"] += 1
        return "please reply about the schedule"

    row = {
        "message_id": "m-2",
        "subject": "RE: Project update",
        "from": "Pat Lee",
        "content_hash": "h2",
        "body": "",
        "fetch_body": fetch,
    }
    prior = {"m-2": {"decision": "no_reply_needed", "decision_hash": "h1"}}
    run_round([row], prior=prior)
    assert fetches["n"] == 1


def test_decision_cached_needs_a_hash():
    assert decision_cached({"decision": "no_reply_needed"}) == ""
    assert decision_cached({"decision": "no_reply_needed", "decision_hash": "h"}) == "no_reply_needed"
    assert decision_cached({"decision": "other", "decision_hash": "h"}) == ""
