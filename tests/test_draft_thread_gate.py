"""Exact source id, addressee, and greeting gates for a reply draft."""

from __future__ import annotations

import unittest

from hailports.draft_runner import run_round
from hailports.draft_thread_gate import (
    REJECT_GREETING,
    SKIP_NO_SOURCE_ID,
    SKIP_NOT_ADDRESSED,
    explicit_source_ids,
    reply_draft_gate,
)

SOURCE_ID = "AAMk-exact-source-id-001"
CONV_ID = "conv-10000001"


def exact_item() -> dict:
    return {
        "id": "spreadsheet-account-names-review",
        "draft_channel": "outlook",
        "title": "Mina asked about names in red on sheet",
        "why": "Mina asking Casey if the red account names on the sheet are what she expected",
        "notes": (
            "Mina wrote to check with Casey whether the red account names are wrong. "
            "Draft a reply for Casey to send or pass along."
        ),
    }


class GateTests(unittest.TestCase):
    def test_exact_case_is_not_addressed_and_has_no_id(self):
        item = exact_item()
        self.assertEqual(explicit_source_ids(item)["message_id"], "")
        decision = reply_draft_gate(item, "hey Casey -- mina is asking if the red account names match\n")
        self.assertFalse(decision["ok"])
        self.assertEqual(decision["disposition"], SKIP_NOT_ADDRESSED)
        self.assertFalse(decision["sent"])

    def test_addressed_without_id_is_skipped(self):
        item = exact_item()
        item["why"] = "Mina asking Pat if the red account names are right"
        item["notes"] = "waiting on a reply. match: Mina"
        item["title"] = "Mina asked about names in red on sheet"
        decision = reply_draft_gate(item, "hey Mina -- the red names match\n")
        self.assertEqual(decision["disposition"], SKIP_NO_SOURCE_ID)
        self.assertEqual(explicit_source_ids({"message_id": "Mina"})["message_id"], "")

    def test_cc_only_and_greeting(self):
        cc = {
            "draft_channel": "outlook",
            "message_id": SOURCE_ID,
            "title": "review",
            "why": "please look",
            "notes": "To: Casey\nCc: Pat\n",
        }
        self.assertEqual(reply_draft_gate(cc)["disposition"], SKIP_NOT_ADDRESSED)
        item = {
            "draft_channel": "outlook",
            "message_id": SOURCE_ID,
            "why": "Mina asking Pat if the red account names are right",
            "notes": f"message_id: {SOURCE_ID}",
        }
        source = {"from": "Mina Cole", "to": "Pat Operator"}
        ok = reply_draft_gate(item, "hey Mina -- the red names match\n", source=source)
        bad = reply_draft_gate(item, "hey Casey -- the red names match\n", source=source)
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["target"], SOURCE_ID)
        self.assertEqual(bad["disposition"], REJECT_GREETING)

    def test_zoom_name_is_not_a_target(self):
        zoom = {
            "draft_channel": "zoom",
            "why": "Mina asking Pat if the login works",
            "notes": "display name only",
            "participants": ["Mina", "Pat"],
        }
        self.assertEqual(reply_draft_gate(zoom, "hey Mina -- ok\n")["disposition"], SKIP_NO_SOURCE_ID)
        zoom["conversation_id"] = CONV_ID
        found = reply_draft_gate(zoom, "hey Mina -- ok\n")
        self.assertTrue(found["ok"])
        self.assertEqual(found["target"], CONV_ID)
        host = "xmpp." + "zoom.us"
        token = "abc12345@" + host
        noted = dict(zoom)
        noted.pop("conversation_id")
        noted["notes"] = "id " + token
        from_notes = reply_draft_gate(noted, "hey Mina -- ok\n")
        self.assertTrue(from_notes["ok"])
        self.assertEqual(from_notes["target"], token)

    def test_runner_skips_the_exact_case_and_stages_a_real_id(self):
        skipped = run_round([{
            **exact_item(),
            "subject": exact_item()["title"],
            "ask": exact_item()["why"],
            "sources": ["The red names are unchanged."],
        }])
        self.assertEqual(skipped["drafted"], 0)
        self.assertEqual(skipped["skipped"][0]["skip"], SKIP_NOT_ADDRESSED)

        staged = run_round([{
            "draft_channel": "outlook",
            "message_id": SOURCE_ID,
            "subject": "red names",
            "ask": "Mina asking Pat if the red account names are right",
            "from": "Mina Cole",
            "participants": ["Mina", "Pat"],
            "sources": ["The red names match the list."],
        }])
        self.assertEqual(staged["drafted"], 1)
        self.assertFalse(staged["drafts"][0]["sent"])
        self.assertEqual(staged["drafts"][0]["message_id"], SOURCE_ID.lower())
