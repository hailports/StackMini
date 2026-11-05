"""A prepare-only request cannot stage a draft that says access was added."""

import json

from hailports.contextpack import apply_check
from hailports.factclaim import (
    allowed,
    completion_claims,
    stage_block,
    write_proven,
)


HONEST = (
    "I have not changed the records. "
    "The compliance check has not been run. This is still waiting on a go."
)
LIE = (
    "I have run the compliance check on the new account. "
    "I have also added access for the two contacts in the record system."
)
ASK = "Please run the compliance check and add access for the two contacts."
WHY = (
    "The draft does not confirm the compliance check was run nor does it "
    "confirm that access was added."
)


def _work():
    return {
        "route": "prepare_only",
        "record_write": False,
        "executed": False,
        "needs_go": True,
        "status": "packaged_not_executed",
        "draft": HONEST,
    }


def test_denials_are_not_claims():
    assert completion_claims("I have not changed the records.") == []
    assert completion_claims("The compliance check has not been run.") == []
    assert completion_claims("access was not added") == []
    assert completion_claims("I have added access")
    assert completion_claims("I enabled it")
    assert completion_claims("I ran the compliance check")
    assert completion_claims("done in the record system")


def test_create_only_history_is_not_proof():
    assert write_proven({"history_changes": [{"action": "create", "field": "Name", "old": None, "new": "A"}]}) is False
    assert write_proven({"history_changes": [{"action": "update", "field": "Access", "old": "No", "new": "Yes"}]}) is True
    assert write_proven({"access_readback": "Yes"}) is True
    assert write_proven({"executed": True, "record_ids": ["003000000000001"]}) is True
    assert write_proven({"executed": True}) is False
    assert write_proven(_work()) is False


def test_prepare_only_false_confirm_does_not_rewrite_into_a_lie():
    calls = {"rewrite": 0}

    def complete(messages, max_tokens=80, json_mode=False):
        blob = messages[0]["content"]
        if "Grade one draft" in blob:
            return {"ok": True, "text": json.dumps({"pass": False, "reason": WHY}), "reason": ""}
        calls["rewrite"] += 1
        return {"ok": True, "text": LIE, "reason": ""}

    out = apply_check(ASK, ASK, HONEST, complete=complete, proof=_work(), work=_work())
    assert out["ok"] is True
    assert calls["rewrite"] == 0
    assert "I have also added" not in out["text"]
    assert "I have run the compliance" not in out["text"]
    assert "not changed" in out["text"].lower()
    assert stage_block(out["text"], work=_work(), proof=_work()) == ""


def test_prepare_only_lie_cannot_stage():
    out = apply_check(
        ASK,
        ASK,
        LIE,
        complete=lambda *a, **k: {"ok": True, "text": '{"pass": true, "reason": "done"}', "reason": ""},
        proof=_work(),
        work=_work(),
    )
    assert out["ok"] is True
    assert "I have also added" not in out["text"]
    assert "not changed" in out["text"].lower()
    assert stage_block(LIE, work=_work(), proof=_work()) == "hold:unproven_work"
    assert stage_block(LIE) == "hold:unproven_work"
    assert allowed(LIE, context="please run the compliance check and add access") is False


def test_context_backed_claim_still_passes():
    def complete(messages, max_tokens=80, json_mode=False):
        return {"ok": True, "text": '{"pass": true, "reason": "grounded"}', "reason": ""}

    out = apply_check(
        "can they edit contacts?",
        "I added a contact mapping. The account stays read only.",
        "I added the contact mapping. the account stays read only.",
        complete=complete,
    )
    assert out["ok"] is True
    assert "added the contact mapping" in out["text"]


def test_proven_write_may_say_access_was_enabled():
    proof = {"executed": True, "record_ids": ["003000000000001"], "access_readback": "Yes"}
    assert stage_block("I enabled access.", proof=proof) == ""
    assert allowed("I enabled access.", proof=proof) is True
