"""Fail-closed checks for the production access gate. No live org calls."""
import os
import tempfile
from pathlib import Path

import sdplus_access_gate as gate


def setup_function():
    tmp = Path(tempfile.mkdtemp(prefix="sdplus-"))
    os.environ["SDPLUS_AUDIT_PATH"] = str(tmp / "audit.jsonl")
    os.environ.pop("SDPLUS_API_BASE", None)
    gate.clear_verifier()


def teardown_function():
    gate.clear_verifier()


def test_prod_without_ticket_blocks():
    decision = gate.decide("create a user in prod", prod=True, op="user_create")
    assert decision["block"] is True
    assert "Service Desk Plus" in decision["message"]
    assert decision["decision"] == "missing_ticket"


def test_open_ticket_blocks():
    gate.set_verifier(lambda ticket: "open")
    decision = gate.decide("SD+ 555001", prod=True, op="permset_assign")
    assert decision["block"] is True
    assert decision["ticket_id"] == "555001"
    assert "not confirmed approved" in decision["message"]


def test_approved_ticket_allows_and_unknown_does_not():
    gate.set_verifier(lambda ticket: "approved" if ticket == "555001" else "unknown")
    allowed = gate.decide("SDP-555001", prod=True, op="user_create")
    assert allowed["allow"] is True and allowed["decision"] == "approved"
    blocked = gate.decide("Service Desk Plus #555002", prod=True, op="user_create")
    assert blocked["block"] is True and blocked["decision"] == "unknown"


def test_sandbox_does_not_require_a_ticket():
    decision = gate.decide("create a user", prod=False, op="user_create")
    assert decision["allow"] is True
    noted = gate.decide("SD+ 555001", prod=False, op="user_create")
    assert noted["allow"] is True and noted["ticket_id"] == "555001"


def test_privileged_password_is_refused():
    decision = gate.decide(
        "set the shared privileged password for the admin account",
        prod=True,
        op="user_create",
    )
    assert decision["block"] is True
    assert "CyberArk" in decision["message"]
    assert "Passwordstate" in decision["message"]
    audit = Path(os.environ["SDPLUS_AUDIT_PATH"]).read_text(encoding="utf-8")
    assert "password_refused" in audit
    assert "admin account" not in audit


def test_operator_alias_is_production_and_sandbox_names_are_not():
    assert gate.is_prod_org("operator")
    assert gate.is_prod_org("production")
    assert not gate.is_prod_org("partial")
    assert not gate.is_prod_org("fullsand")
    assert not gate.is_prod_org("")


def test_plain_ticket_word_needs_service_desk_context():
    assert gate.parse_ticket_id("ticket 555001") == ""
    assert gate.parse_ticket_id("SD+ ticket 555001") == "555001"
    assert gate.parse_ticket_id("work item 555001") == ""
