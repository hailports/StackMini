"""A prepare-only ask cannot stage a draft that says access was added."""

import json
import unittest
from pathlib import Path

import draft_context as dc
import fact_claim as fc


HONEST = (
    "hey,\n\nI have not changed operator. "
    "The compliance check has not been run. This is still waiting on your go."
)
LIE = (
    "hey,\n\nI have run the compliance check on the new dealer. "
    "I have also added access for the two contacts in operator."
)
ASK = "Please run the compliance check and add access for the two contacts."
WHY = (
    "The draft does not confirm the compliance check was run nor does it "
    "confirm that access was added."
)


def _work():
    return {
        "route": "operatorbot_prepare_only",
        "sf_write": False,
        "execute": False,
        "executed": False,
        "needs_operator_ok": True,
        "status": "packaged_not_executed",
        "draft": HONEST,
    }


class FactClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.id().replace(".", "_"))
        # Tests share the process. Keep each home under the system temp via draft_context's home override.
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="factclaim-"))
        fc.clear_lap()

    def test_denials_are_not_claims(self):
        self.assertEqual(fc.completion_claims("I have not changed operator."), [])
        self.assertEqual(fc.completion_claims("The compliance check has not been run."), [])
        self.assertEqual(fc.completion_claims("access was not added"), [])
        self.assertTrue(fc.completion_claims("I have added access"))
        self.assertTrue(fc.completion_claims("I enabled it"))
        self.assertTrue(fc.completion_claims("I ran the compliance check"))
        self.assertTrue(fc.completion_claims("done in operator"))

    def test_create_only_history_is_not_proof(self):
        self.assertFalse(fc.write_proven({
            "contact_history": [{"action": "create", "field": "Name", "old": None, "new": "A"}],
        }))
        self.assertTrue(fc.write_proven({
            "contact_history": [{"action": "update", "field": "LMS_Access__c", "old": "No", "new": "Yes"}],
        }))
        self.assertTrue(fc.write_proven({"LMS_Access__c": "Yes"}))
        self.assertTrue(fc.write_proven({"executed": True, "record_ids": ["003000000000001"]}))
        self.assertFalse(fc.write_proven({"executed": True}))
        self.assertFalse(fc.write_proven(_work()))

    def test_prepare_only_false_confirm_does_not_rewrite_into_a_lie(self):
        calls = {"rewrite": 0}

        def complete(messages, max_tokens=80, json_mode=False):
            blob = messages[0]["content"]
            if "Grade one draft" in blob:
                return {"ok": True, "text": json.dumps({"pass": False, "reason": WHY}), "reason": ""}
            calls["rewrite"] += 1
            return {"ok": True, "text": LIE, "reason": ""}

        out = dc.apply_check(
            ASK,
            ASK,
            HONEST,
            surface="outlook",
            complete=complete,
            home=self.tmp,
            proof=_work(),
            work=_work(),
        )
        self.assertTrue(out["ok"])
        self.assertEqual(calls["rewrite"], 0)
        self.assertNotIn("I have also added", out["text"])
        self.assertNotIn("I have run the compliance", out["text"])
        self.assertIn("not changed", out["text"].lower())
        self.assertEqual(fc.stage_block(out["text"], work=_work(), proof=_work()), "")

    def test_prepare_only_lie_cannot_stage(self):
        out = dc.apply_check(
            ASK,
            ASK,
            LIE,
            surface="outlook",
            complete=lambda *a, **k: {"ok": True, "text": '{"pass": true, "reason": "done"}', "reason": ""},
            home=self.tmp,
            proof=_work(),
            work=_work(),
        )
        self.assertTrue(out["ok"])
        self.assertNotIn("I have also added", out["text"])
        self.assertIn("not changed", out["text"].lower())
        self.assertEqual(fc.stage_block(LIE, work=_work(), proof=_work()), "hold:unproven_work")
        self.assertEqual(fc.stage_block(LIE), "hold:unproven_work")

    def test_guard_blocks_a_failed_check_that_would_have_staged_the_lie(self):
        dc.stash({"text": ASK, "links": []})

        def complete(messages, max_tokens=80, json_mode=False):
            if "Grade one draft" in messages[0]["content"]:
                return {"ok": True, "text": json.dumps({"pass": False, "reason": WHY}), "reason": ""}
            return {"ok": True, "text": LIE, "reason": ""}

        original = dc.local_complete
        dc.local_complete = complete
        try:
            out = dc.guard(
                {"ok": True, "text": HONEST, "work": _work(), "never_send": True, "sent": False},
                ask=ASK,
                surface="outlook",
                home=self.tmp,
            )
        finally:
            dc.local_complete = original
            dc.stash(None)
        self.assertNotIn("I have also added", out.get("text") or "")
        self.assertNotIn("I have run the compliance", out.get("text") or "")
        self.assertTrue(out.get("text"))
        self.assertIn("not changed", out["text"].lower())
        self.assertFalse(out.get("sent"))

    def test_context_backed_claim_still_passes_without_a_prepare_flag(self):
        def complete(messages, max_tokens=80, json_mode=False):
            return {"ok": True, "text": '{"pass": true, "reason": "grounded"}', "reason": ""}

        out = dc.apply_check(
            "can dealers edit contacts?",
            "I added a contact mapping. The account stays read only.",
            "I added the contact mapping. the account stays read only.",
            surface="ado",
            complete=complete,
            home=self.tmp,
        )
        self.assertTrue(out["ok"])
        self.assertIn("added the contact mapping", out["text"])

    def test_proven_write_may_say_access_was_enabled(self):
        proof = {"executed": True, "record_ids": ["003000000000001"], "LMS_Access__c": "Yes"}
        text = "I enabled access."
        self.assertEqual(fc.stage_block(text, proof=proof), "")
        self.assertTrue(fc.allowed(text, proof=proof))


if __name__ == "__main__":
    unittest.main()
