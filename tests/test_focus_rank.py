"""Current-work ranking. Wrong owner, stale items, and guessed expansions stay out."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from hailports import focus_rank as fg
from hailports.worknow import pack

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "focus_current_work.json"
TODAY = date(2026, 11, 5)
USER = ("Example User",)


def test_current_work_question_and_plain_cards() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fg.is_self_focus(data["question"]) is True
    assert fg.is_self_focus("what am i  currently focused on at work") is True
    assert fg.is_self_focus("what's my current focus") is True
    assert fg.is_self_focus("what\u2019s my current focus") is True
    assert fg.is_self_focus("howdy") is False
    assert fg.is_self_focus("what is the team currently focused on") is False
    assert fg.is_self_focus("current focus of the release") is False
    assert fg.is_self_focus("what am i focused on for SF-10001") is False
    assert fg.is_self_focus("what am i currently focused on at work for 222222") is False
    assert pack([{"title": "Ship", "detail": "the readme"}]) == "- Ship: the readme"
    assert pack([]) == "Nothing on the focus list."


def test_rank_demotes_wrong_owner_and_stale() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ranked = fg.rerank_ticket_lines(
        "\n".join(data["ticket_lines"]),
        user_markers=USER,
        today=TODAY,
    )
    assert ranked.find("222222") < ranked.find("900001") < ranked.find("900002")
    assert ranked.find("Lower rank") < ranked.find("900001")
    assert ranked.find("222222") < ranked.find("Lower rank")
    answer = fg.focus_lines(data["docket_items"], as_of=data["as_of"])
    assert "222222" in answer
    assert "https://example.test/items/222222" in answer
    assert "900001" not in answer
    assert "900002" not in answer
    assert "900003" not in answer
    assert "?" not in answer
    cards = [
        {"id": "900001", "title": "Sample other owner", "state": "New (New)", "owner": "Other Person", "changed": "2026-11-04"},
        {"id": "222222", "title": "Sample active item", "state": "Active (Approved)", "mine": True, "changed": "2026-11-04"},
        {"id": "900002", "title": "Sample stale item", "state": "New (Moved to the backlog)", "mine": True, "changed": "2026-09-01"},
    ]
    packed = pack(cards, today=TODAY)  # fixed date: the sample cards would age past stale_days
    assert packed.find("222222") < packed.find("900001") < packed.find("900002")
    assert "owner: Example User self" not in packed
    assert fg.owner_is_user("Joanne", ("ann",)) is False
    assert fg.owner_is_user("Example User", ("example",)) is False
    assert fg.owner_is_user("Example User", ("Example User",)) is True
    assert fg.owner_is_user("Example User", ("example user",)) is True
    sneaky = fg.rerank_ticket_lines(
        "- 111111 | X | Active (Approved) | owner: Joanne | changed 2026-11-04\n"
        "- 222222 | Y | Active (Approved) | owner: Example User | changed 2026-11-03",
        user_markers=USER,
        today=TODAY,
    )
    assert sneaky.find("222222") < sneaky.find("111111")
    assert sneaky.find("Lower rank") < sneaky.find("111111")


def test_guessed_expansion_is_dropped() -> None:
    facts = fg.focus_lines(
        [{"id": "222222", "title": "Sample active item", "state": "Active (Approved)", "changed": "2026-11-04"}],
        as_of="2026-11-05T17:12:00-06:00",
    )
    guessed = fg.keep_grounded("ZZZ (Sample program?) is the focus.", facts)
    assert "?" not in guessed
    assert "222222" in guessed
    invented = fg.keep_grounded("Also see 900001.", facts)
    assert "900001" not in invented


def test_exact_id_and_owner_equality() -> None:
    facts = fg.focus_lines(
        [{"id": "SF-10001", "title": "Sample active item", "state": "Active (Approved)", "changed": "2026-11-04"}],
        as_of="2026-11-05T17:12:00-06:00",
    )
    assert fg.keep_grounded("Also see SF-1000.", facts) == facts
    assert fg.keep_grounded("See sf-10001.", facts) == "See sf-10001."
    only = fg.focus_lines(
        [{"id": "222222", "title": "Sample active item", "state": "Active (Approved)", "changed": "2026-11-04"}],
        as_of="2026-11-05",
    )
    assert fg.keep_grounded("Also see 22222.", only) == only
    assert fg.keep_grounded("Your focus is 222222.", "Item 1222222 is the only id.") == (
        "Item 1222222 is the only id."
    )
    other = fg.focus_lines(
        [{
            "id": "900001",
            "title": "Other active",
            "state": "Active (Approved)",
            "owner": "Other Person",
            "changed": "2026-11-04",
        }],
        as_of="2026-11-05",
    )
    assert "900001" not in other
    stale = fg.focus_lines(
        [{"id": "444444", "title": "Old active", "state": "Active (Approved)", "changed": "2026-01-01"}],
        as_of="2026-11-05",
    )
    assert "444444" not in stale
    bad = fg.focus_lines(
        [{"id": "555555", "title": "Bad date", "state": "Active (Approved)", "changed": "not-a-date"}],
        as_of="2026-11-05",
    )
    assert "555555" not in bad
    for state in (
        "Inactive",
        "Not active",
        "Inactive (Cancelled)",
        "Currently inactive",
        "non-active",
        "Nonactive",
        "Deactivated",
        "Active (Closed)",
        "Resolved",
    ):
        dropped = fg.focus_lines(
            [{"id": "900004", "title": "Sample inactive item", "state": state, "changed": "2026-11-04"}],
            as_of="2026-11-05T17:12:00-06:00",
        )
        assert "900004" not in dropped
    kept = fg.focus_lines(
        [{
            "id": "222222",
            "title": "[API] ZZZ work",
            "state": "Active (Unresolved)",
            "owner": "Example User",
            "changed": "2026-11-04",
            "url": "https://example.test/items/222222",
        }],
        as_of="2026-11-05",
        user="Example User",
    )
    assert "[API] ZZZ work" in kept
    assert "https://example.test/items/222222" in kept
    self_name = pack(
        [
            {
                "id": "111111",
                "title": "Other surname",
                "state": "Active (Approved)",
                "owner": "John Self",
                "changed": "2026-11-04",
            },
            {
                "id": "222222",
                "title": "Mine",
                "state": "Active (Approved)",
                "mine": True,
                "owner": "Example User",
                "changed": "2026-11-03",
            },
        ],
        today=TODAY,
    )
    assert self_name.find("222222") < self_name.find("Lower rank") < self_name.find("111111")
    assert "owner: Example User self" not in self_name
    assert fg.owner_is_user("John Self", ("self",)) is False
    assert fg.owner_is_user("Not-Example", ("example",)) is False
