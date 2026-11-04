"""Skip reasons, one id per round, and a time confirm from the newest line only."""

from __future__ import annotations

from datetime import datetime, timezone

from hailports.drafter import newest_unquoted, scheduling_question, stage_ready
from hailports.draft_runner import hold_reopen_once, run_round, skip_line, stamp_reopen_once

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
TOKEN = "UNIQUEBODYTOKEN"


def _confirm_body() -> str:
    return (
        f"is 2pm utc okay? {TOKEN}\n\n"
        "From: Old Note\n"
        "can we hop on a call tomorrow?\n"
        "what time works\n"
    )


def test_quoted_call_does_not_swallow_the_new_line():
    body = "The login is working now.\n\nFrom: Old Note\ncan we hop on a call tomorrow?\n"
    newest = newest_unquoted(body)
    assert "hop on a call" not in newest
    assert not scheduling_question(newest)
    assert scheduling_question("can we hop on a call tomorrow?")
    result = run_round([
        {
            "message_id": "m-login",
            "subject": "RE: access",
            "from": "Lee, Pat",
            "body": body,
            "ask": "login",
            "sources": ["The login is working now."],
        }
    ])
    assert result["drafted"] == 1
    assert result["skipped"] == []
    assert result["drafts"][0]["sent"] is False
    assert "login" in result["drafts"][0]["text"]


def test_open_time_is_staged_and_a_booked_time_asks_again():
    free = [{"start": "2026-11-04T16:00:00+00:00", "end": "2026-11-04T17:00:00+00:00"}]
    booked = [{"start": "2026-11-04T14:00:00+00:00", "end": "2026-11-04T15:00:00+00:00"}]
    row = {
        "message_id": "AbC",
        "subject": "RE: access",
        "from": "Lee, Pat",
        "body": _confirm_body(),
    }
    opened = run_round([row], calendar=free, now=NOW)
    text = opened["drafts"][0]["text"]
    assert opened["drafted"] == 1
    assert opened["drafts"][0]["sent"] is False
    assert "2pm" in text
    assert "works" in text
    assert "I am on it" in text
    assert stage_ready(text)
    assert TOKEN not in text

    closed = run_round([row], calendar=booked, now=NOW)
    booked_text = closed["drafts"][0]["text"]
    assert "already booked" in booked_text
    assert "I am on it" in booked_text
    assert stage_ready(booked_text)

    unknown = run_round([row], calendar=None, now=NOW)
    assert unknown["drafts"][0]["text"].startswith("hey Pat,")
    assert "works" not in unknown["drafts"][0]["text"]
    assert "I am on it" in unknown["drafts"][0]["text"]


def test_voice_cleanup_keeps_the_confirm_sentence():
    def cleaner(_text: str) -> str:
        return "hey Pat,\n\n"

    result = run_round(
        [{
            "message_id": "t1",
            "subject": "RE: access",
            "from": "Pat",
            "body": _confirm_body(),
        }],
        calendar=[],
        now=NOW,
        cleaner=cleaner,
    )
    text = result["drafts"][0]["text"]
    assert "I am on it" in text
    assert "works" in text
    assert stage_ready(text)
    assert result["drafts"][0]["sent"] is False


def test_same_id_once_and_skip_line_has_reason_not_body():
    row = {
        "message_id": "AbC",
        "subject": "RE: access",
        "from": "Pat",
        "body": _confirm_body(),
    }
    other = dict(row, message_id="<abc>")
    result = run_round([row, other], calendar=[], now=NOW)
    assert result["drafted"] == 1
    assert result["skipped"][0]["skip"] == "duplicate"
    assert "body" not in result["skipped"][0]
    line = result["log"][0]
    assert "reason=duplicate" in line
    assert "subj=RE: access" in line
    assert "from=Pat" in line
    assert TOKEN not in line
    plain = skip_line("scheduling", "RE: access", "Pat")
    assert plain == "draft_skip reason=scheduling subj=RE: access from=Pat"


def test_parked_note_gets_one_more_try_then_stops():
    row = {
        "message_id": "m1",
        "subject": "RE: access",
        "from": "Pat",
        "body": "can we hop on a call tomorrow?\n",
    }
    prior = {"m1": {"discard_parked": True, "refuse_n": 2, "error": "needs_input"}}
    assert hold_reopen_once(prior["m1"])
    first = run_round([row], prior=prior)
    assert first["drafted"] == 0
    assert first["skipped"][0]["skip"] == "scheduling"
    assert first["prior"]["m1"]["hold_reopen_n"] >= 1
    assert not hold_reopen_once(first["prior"]["m1"])
    second = run_round([row], prior=first["prior"])
    assert second["drafted"] == 0
    assert second["skipped"][0]["skip"] == "discard_parked"
    assert TOKEN not in "\n".join(second["log"])


def test_a_real_delete_stays_parked_and_a_missing_stamp_gets_one_try():
    deleted = run_round(
        [{
            "message_id": "gone",
            "subject": "RE: access",
            "from": "Pat",
            "body": _confirm_body(),
        }],
        calendar=[],
        now=NOW,
        prior={"gone": {"discarded_at": 10, "discard_parked": True, "refuse_n": 2, "error": "needs_input"}},
    )
    assert deleted["drafted"] == 0
    assert deleted["skipped"][0]["skip"] == "discarded"

    assert stamp_reopen_once({"error": "no_pass"})
    assert not stamp_reopen_once({"error": "no_pass", "gate_reopen_n": 1})
    assert not stamp_reopen_once({"error": "no_pass", "draft_id": "d1"})
    row = {
        "message_id": "m9",
        "subject": "Hello",
        "from": "Pat",
        "body": f"Thanks for the note. {TOKEN}",
    }
    first = run_round([row], prior={"m9": {"error": "no_pass"}})
    assert first["drafted"] == 0
    assert first["skipped"][0]["skip"] == "no_source"
    assert first["prior"]["m9"]["gate_reopen_n"] >= 1
    assert TOKEN not in "\n".join(first["log"])
    second = run_round([row], prior=first["prior"])
    assert second["drafted"] == 0
    assert second["skipped"][0]["skip"] == "discard_parked"
