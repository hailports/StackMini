"""Owner scope and the send cap. No network and no mail."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hailports import note_scope as ns

OWNER = "Sam Rivera"


def _card(**extra):
    base = {
        "id": "100010",
        "state": "Active",
        "assignee": "Other Person",
        "blocking_who": "Pat",
        "likely_next_action": "Pat posts the result",
    }
    base.update(extra)
    return base


def test_scope_is_assignee_or_blocking_who_only():
    assert ns.in_scope(_card(assignee=OWNER, blocking_who="nobody"), OWNER)
    assert ns.in_scope(_card(assignee="  Sam   Rivera "), OWNER)
    assert ns.assigned_to_owner({"System.AssignedTo": {"displayName": OWNER}}, OWNER)
    assert ns.in_scope(_card(blocking_who="Sam"), OWNER)
    assert ns.in_scope(_card(assignee="", blocking_who="Sam Rivera"), OWNER)
    assert not ns.in_scope(_card(), OWNER)
    sneak = _card(blocking_who="nobody", likely_next_action="Sam Rivera reviews the criteria")
    assert not ns.in_scope(sneak, OWNER)
    assert not ns.blocking_names_owner({"blocking_who": "Samantha"}, OWNER)
    assert not ns.blocking_names_owner({"blocking_who": "nobody"}, OWNER)


def test_cap_aborts_with_no_sends():
    cards = [_card(id=str(200000 + i), assignee=OWNER, blocking_who="Sam") for i in range(26)]
    over = ns.select_notes(cards, OWNER)
    assert over["aborted"] == "cap"
    assert over["send"] == []
    assert over["would_send"] == 26
    under = ns.select_notes(cards[:25], OWNER)
    assert under["aborted"] == ""
    assert len(under["send"]) == 25
    assert ns.cap_abort(25) is False
    assert ns.cap_abort(26) is True


def test_already_sent_is_not_sent_again():
    cards = [_card(id="100010", assignee=OWNER)]
    picked = ns.select_notes(cards, OWNER, already_sent={"100010"})
    assert picked["send"] == []
    assert picked["skipped"].get("unchanged") == 1
    assert picked["aborted"] == ""
