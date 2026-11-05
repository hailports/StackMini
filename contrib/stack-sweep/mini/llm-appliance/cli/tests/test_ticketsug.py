"""Dedupe, single-use tokens, edit, and no create without a click."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.home() / "claude-stack/tools"))

import open_ado_loop  # noqa: E402
import ticket_suggest  # noqa: E402


class TicketSuggestTests(unittest.TestCase):
    def test_open_loop_rules(self) -> None:
        open_ado_loop.self_check()

    def test_suggest_rules(self) -> None:
        ticket_suggest.self_check()

    def test_preview_does_not_create(self) -> None:
        calls = {"n": 0}

        def search(_story):
            calls["n"] += 1
            return {"ok": True, "ids": []}

        def create(_story):
            calls["n"] += 1
            return {"ok": True, "work_item_id": "1"}

        item = {
            "status": "pending",
            "expires": 9_999_999_999,
            "action": {"kind": "ado_suggest", "payload": {"story": {"title": "Dealer usernames are not working", "description": "usernames are not working"}, "dry_run": False}},
        }
        out = ticket_suggest.decide(item, None, "get", search, create)
        self.assertTrue(out["preview"])
        self.assertFalse(out["created"])
        self.assertEqual(calls["n"], 0)

    def test_dry_token_cannot_create(self) -> None:
        calls = {"n": 0}
        item = {
            "status": "pending",
            "dry_run": True,
            "expires": 9_999_999_999,
            "action": {"payload": {"dry_run": True, "story": {"title": "Test suggestion does not post"}}},
        }
        out = ticket_suggest.decide(item, None, "approve", lambda s: calls.__setitem__("n", 1), lambda s: calls.__setitem__("n", 1))
        self.assertTrue(out["dry_run"])
        self.assertFalse(out["mutated"])
        self.assertEqual(calls["n"], 0)
        item["status"] = "approved"
        again = ticket_suggest.decide(item, None, "approve", lambda s: calls.__setitem__("n", 1), lambda s: calls.__setitem__("n", 1))
        self.assertEqual(again["error"], "status_approved")
        self.assertEqual(calls["n"], 0)


if __name__ == "__main__":
    unittest.main()
