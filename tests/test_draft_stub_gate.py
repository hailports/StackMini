"""Synthetic fixtures for the greeting / closer / signature shell gate."""

from __future__ import annotations

import unittest

from hailports import draft_stub_gate as gate

SIG = (
    "Pat Example | Technical Lead\n"
    "Example Co | 1 Example Plaza | Town ST\n"
    "Mobile: listed on the card\n"
    "example.com | www.example.com"
)

MULTI = """To: Riley Nguyen; Pat Lee

My asks:
Could you please confirm the three discount tiers for the north area?
Could you please share the label names we should keep on the sheet?
Could you please review the attached query and say if the date filter is right?

Thanks
"""

EXACT = (
    "hey Pat & Nguyen,\n"
    "\n"
    "hey Pat, I have some ready for your go. I have not changed CRM.\n"
    "\n"
    + SIG
)

ONE_GREET = (
    "hey Pat, I have some ready for your go. I have not changed CRM.\n"
    "\n"
    + SIG
)

GOOD = (
    "hey Pat -- the tiers are 5, 10, and 15 percent. "
    "I wrote those three into the sheet this morning. "
    "I have that ready for your go. I have not changed CRM.\n"
    "\n"
    + SIG
)

ONE_ASK = "Can you send the tier list for the north area?"


class ShellGateTests(unittest.TestCase):
    def test_exact_shell_is_not_grounded(self):
        reasons = gate.outlook_shell_reasons(EXACT, MULTI)
        self.assertIn("dup_greeting", reasons)
        self.assertIn("recipient_surname_greeting", reasons)
        self.assertTrue(
            {"empty_after_strip", "closer_only"} & set(reasons),
            reasons,
        )
        self.assertTrue(gate.blocks_grounded_record(EXACT, MULTI))
        self.assertEqual(gate.grounded_evidence_or_refuse(EXACT, MULTI), "")

    def test_single_greeting_closer_is_empty(self):
        reasons = gate.outlook_shell_reasons(ONE_GREET, MULTI)
        self.assertNotIn("dup_greeting", reasons)
        self.assertIn("empty_after_strip", reasons)
        self.assertIn("closer_only", reasons)
        self.assertEqual(gate.grounded_evidence_or_refuse(ONE_GREET, MULTI), "")

    def test_that_ready_only(self):
        text = "I have that ready for your go.\n\nI have not changed CRM.\n\n" + SIG
        reasons = gate.outlook_shell_reasons(text, MULTI)
        self.assertIn("closer_only", reasons)
        self.assertIn("empty_after_strip", reasons)
        self.assertEqual(gate.grounded_evidence_or_refuse(text, ""), "")

    def test_the_noun_ready_only(self):
        text = "I have the report ready for your go. I have not changed CRM.\n\n" + SIG
        reasons = gate.outlook_shell_reasons(text, "")
        self.assertIn("closer_only", reasons)
        self.assertEqual(gate.grounded_evidence_or_refuse(text, MULTI), "")

    def test_dup_greeting_with_substance_refuses(self):
        text = (
            "hey Pat,\n\n"
            "hey Pat, the tiers are 5, 10, and 15 percent and I put those on the sheet today.\n"
        )
        reasons = gate.outlook_shell_reasons(text, ONE_ASK)
        self.assertIn("dup_greeting", reasons)
        self.assertNotIn("empty_after_strip", reasons)
        self.assertEqual(gate.grounded_evidence_or_refuse(text, ONE_ASK), "")

    def test_recipient_surname_refuses(self):
        text = (
            "hey Pat & Nguyen,\n\n"
            "The tiers are 5, 10, and 15 percent and I put those on the sheet today.\n"
        )
        reasons = gate.outlook_shell_reasons(text, MULTI)
        self.assertIn("recipient_surname_greeting", reasons)
        self.assertNotIn("empty_after_strip", reasons)
        self.assertEqual(gate.grounded_evidence_or_refuse(text, MULTI), "")
        first = gate.outlook_shell_reasons(
            "hey Riley -- the tiers are 5, 10, and 15 percent and the sheet is updated.",
            MULTI,
        )
        self.assertNotIn("recipient_surname_greeting", first)

    def test_substantive_reply_with_closer_passes(self):
        reasons = gate.outlook_shell_reasons(GOOD, ONE_ASK)
        self.assertEqual(reasons, [])
        self.assertFalse(gate.blocks_grounded_record(GOOD, ONE_ASK))
        self.assertEqual(
            gate.grounded_evidence_or_refuse(GOOD, ONE_ASK),
            gate.GROUNDED_ASK_EVIDENCE,
        )


if __name__ == "__main__":
    unittest.main()
