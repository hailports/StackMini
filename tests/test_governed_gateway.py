#!/usr/bin/env python3
"""Tests for the governed tool gateway."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hailports import governed_gateway as gw  # noqa: E402


POLICY = {
    "version": 1,
    "default": {
        "allow_tools": ["*"],
        "deny_tools": [],
        "allow_targets": ["*"],
        "deny_targets": ["crm-prod"],
    },
    "callers": {
        "crm-bot": {
            "allow_tools": ["*"],
            "deny_tools": ["mail.send"],
            "deny_targets": ["crm-prod"],
        },
        "claude": {
            "deny_tools": ["crm.mutate"],
            "deny_targets": ["crm-prod"],
        },
    },
}


class GovernedGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["GATEWAY_AUDIT_DISABLE"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("GATEWAY_AUDIT_DISABLE", None)

    def test_allow_benign(self) -> None:
        r = gw.evaluate(caller="claude", tool="notes.read", target="notes", policy=POLICY, enforce=True)
        self.assertEqual(r["decision"], "allow")
        self.assertTrue(r["allowed"])

    def test_shadow_does_not_block_send(self) -> None:
        r = gw.evaluate(caller="claude", tool="outlook.send", action="send", policy=POLICY, enforce=False)
        self.assertEqual(r["decision"], "shadow_deny")
        self.assertTrue(r["allowed"])
        self.assertTrue(r["would_deny"])

    def test_enforce_blocks_send(self) -> None:
        r = gw.evaluate(caller="claude", tool="outlook.send", action="send", policy=POLICY, enforce=True)
        self.assertEqual(r["decision"], "deny")
        self.assertFalse(r["allowed"])

    def test_hard_data_line_password(self) -> None:
        r = gw.evaluate(
            caller="claude",
            tool="notes.read",
            text="here is the password: hunter2",
            policy=POLICY,
            enforce=True,
        )
        self.assertEqual(r["decision"], "deny")
        self.assertTrue(any(x.startswith("hard_data_line") for x in r["reasons"]))

    def test_hard_data_line_legal_path(self) -> None:
        r = gw.evaluate(
            caller="claude",
            tool="files.read",
            path="/vault/Legal/Finance/contract.pdf",
            policy=POLICY,
            enforce=True,
        )
        self.assertEqual(r["decision"], "deny")

    def test_only_crm_bot_may_mutate(self) -> None:
        other = gw.evaluate(
            caller="claude",
            tool="crm.mutate",
            target="crm-sandbox",
            action="mutate",
            policy=POLICY,
            enforce=True,
        )
        bot = gw.evaluate(
            caller="crm-bot",
            tool="crm.mutate",
            target="crm-sandbox",
            action="mutate",
            policy=POLICY,
            enforce=True,
        )
        self.assertEqual(other["decision"], "deny")
        self.assertEqual(bot["decision"], "allow")

    def test_prod_mutate_denied(self) -> None:
        r = gw.evaluate(
            caller="crm-bot",
            tool="crm.mutate",
            target="crm-prod",
            action="mutate",
            policy=POLICY,
            enforce=True,
        )
        self.assertEqual(r["decision"], "deny")
        self.assertTrue(any("prod" in x for x in r["reasons"]))

    def test_ai_fingerprint_on_employer_surface(self) -> None:
        r = gw.evaluate(
            caller="claude",
            tool="outlook.draft",
            target="employer",
            surface="employer",
            text="As an AI language model I can help.",
            policy=POLICY,
            enforce=True,
        )
        self.assertEqual(r["decision"], "deny")
        self.assertTrue(any("ai_fingerprint" in x for x in r["reasons"]))

    def test_audit_and_rollup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            audit = Path(td) / "gateway-audit.jsonl"
            rollup = Path(td) / "offload-today.md"
            rollup.write_text("# offload today\n\n- local requests: 0\n", encoding="utf-8")
            result = gw.evaluate(
                caller="claude",
                tool="outlook.send",
                action="send",
                policy=POLICY,
                enforce=False,
            )
            os.environ.pop("GATEWAY_AUDIT_DISABLE", None)
            gw.audit(result, audit_path=audit, rollup_path=rollup)
            rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "shadow_deny")
            body = rollup.read_text(encoding="utf-8")
            self.assertIn("## gateway", body)
            self.assertIn("shadow_deny: 1", body)
            self.assertIn("local requests: 0", body)


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
