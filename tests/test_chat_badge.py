"""A chat badge is restored once, and a closed chat that cannot be read is skipped."""

from hailports.chat_badge import (
    after_save,
    decide_rebadge,
    note_operator_cleared,
    note_restored,
    repay_owed,
    secret_hold,
)
from hailports.draft_runner import run_round

TOKEN = "UNIQUEBODYTOKEN"


def _open(book, **extra):
    row = {
        "chat_id": "chat-1",
        "opened_by_runner": True,
        "unread_before": True,
        "latest_from_self": False,
        "message_id": "m-1",
        "source_message_id": "line-9",
    }
    row.update(extra)
    return decide_rebadge(
        book,
        row["chat_id"],
        opened_by_runner=row["opened_by_runner"],
        unread_before=row["unread_before"],
        latest_from_self=row["latest_from_self"],
        message_id=row["source_message_id"],
    )


def test_restore_happens_once_and_only_after_this_open():
    book = {}
    first = _open(book)
    assert first["action"] == "restore"
    assert first["marks_unread"] is True
    note_restored(book, "chat-1", "line-9")
    again = _open(book)
    assert again["action"] == "skip"
    assert again["reason"] == "already_restored"
    assert again["marks_unread"] is False
    assert _open(book, opened_by_runner=False)["reason"] == "not_this_open"
    assert _open(book, unread_before=False, source_message_id="line-10")["reason"] == "not_unread"
    assert _open(book, latest_from_self=True, source_message_id="line-10")["reason"] == "latest_from_self"


def test_a_cleared_dot_stays_clear_and_a_new_line_can_be_restored():
    book = {}
    note_restored(book, "chat-1", "line-9")
    note_operator_cleared(book, "chat-1", "line-9", "dot_cleared")
    cleared = _open(book)
    assert cleared["reason"] == "operator_read"
    fresh = _open(book, source_message_id="line-10")
    assert fresh["action"] == "restore"


def test_the_owed_list_does_not_mark_anything():
    book = {}
    rows = [
        {"chat_id": "chat-1", "message_id": "line-9"},
        {"chat_id": "chat-2", "message_id": "line-2", "latest_from_self": True},
        {"chat_id": "chat-3", "message_id": "line-3"},
    ]
    missed = repay_owed(rows, opened_ids=set(), live_unread_ids=None, book=book)
    assert [item["reason"] for item in missed] == ["not_this_open", "latest_from_self", "not_this_open"]
    assert book == {}
    gone = repay_owed(rows, opened_ids={"chat-3"}, live_unread_ids=set(), book=book)
    assert gone[0]["reason"] == "operator_read"
    assert gone[0]["marks_unread"] is False
    assert gone[2]["reason"] == "not_this_open"
    assert "line-9" in book["chat-1"]["cleared_ids"]
    later = decide_rebadge(
        book, "chat-1",
        opened_by_runner=True, unread_before=True, latest_from_self=False, message_id="line-9",
    )
    assert later["reason"] == "operator_read"


def test_a_draft_save_leaves_the_badge_alone():
    saved = after_save("")
    assert saved["marks_unread"] is False
    assert saved["ok"] is True
    left = after_save("operator_read")
    assert left["left_read"] == "operator_read"
    assert left["error"] == ""
    assert after_save("sidebar_unread_missing")["error"] == "remark_failed"
    assert after_save("sidebar_unread_missing")["marks_unread"] is False


def test_a_closed_empty_chat_is_skipped_with_a_reason():
    result = run_round([
        {
            "message_id": "m-empty",
            "subject": "room",
            "from": "Pat",
            "body": "",
            "may_open": False,
            "read_error": "history_empty",
        }
    ])
    assert result["drafted"] == 0
    assert result["skipped"][0]["skip"] == "no_open passive_empty"
    assert result["skipped"][0]["error"] == "history_empty"
    line = result["log"][0]
    assert "reason=no_open passive_empty" in line
    assert "detail=history_empty" in line
    assert "subj=room" in line
    assert TOKEN not in line
    assert "body" not in result["skipped"][0]


def test_a_named_passkey_error_is_not_a_secret_and_a_real_secret_is_held():
    named = "No matching passkey saved in password."
    assert secret_hold(named) == ""
    assert secret_hold("password = example-value") == "secret_assignment"
    assert secret_hold("the verification code is 123456") == "one_time_code"
    staged = run_round([
        {
            "message_id": "m-passkey",
            "subject": "screen",
            "from": "Pat",
            "body": named,
            "ask": "passkey",
            "sources": ["No matching passkey saved in password. Skip that screen."],
            "opened_by_runner": True,
            "unread_before": True,
            "chat_id": "chat-9",
            "source_message_id": "line-1",
        }
    ])
    assert staged["drafted"] == 1
    assert staged["drafts"][0]["sent"] is False
    assert staged["drafts"][0]["marks_unread"] is False
    assert staged["drafts"][0]["badge_action"] == "restore"
    assert "line-1" in staged["badges"]["chat-9"]["restored_ids"]
    held = run_round([
        {
            "message_id": "m-secret",
            "subject": "screen",
            "from": "Pat",
            "body": f"password = example-value {TOKEN}",
            "sources": ["The screen is a passkey check."],
        }
    ])
    assert held["drafted"] == 0
    assert held["skipped"][0]["skip"] == "secret"
    assert held["skipped"][0]["error"] == "secret_assignment"
    assert TOKEN not in "\n".join(held["log"])
    assert "example-value" not in "\n".join(held["log"])
