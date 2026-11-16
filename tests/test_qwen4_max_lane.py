import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from qwen4_max_lane import (  # noqa: E402
    DAILY_CAP_USD,
    PER_CALL_CAP_USD,
    allow,
    estimate_usd,
    work_or_employer,
)


class WorkEmployerBlock(unittest.TestCase):
    def test_personal_ok(self):
        blocked, _ = work_or_employer("Write a short grocery list for pasta night.")
        self.assertFalse(blocked)

    def test_employer(self):
        blocked, _ = work_or_employer("Summarize this employer policy memo.")
        self.assertTrue(blocked)

    def test_work_ticket(self):
        blocked, _ = work_or_employer("Need an update on the work ticket from today.")
        self.assertTrue(blocked)

    def test_internal_ticket(self):
        blocked, _ = work_or_employer("Please close the internal ticket if it is done.")
        self.assertTrue(blocked)

    def test_customer_account(self):
        blocked, _ = work_or_employer("Look up this customer account in the notes.")
        self.assertTrue(blocked)

    def test_allow_blocks_work(self):
        ok, reason = allow("Draft a reply about the employer handbook.")
        self.assertFalse(ok)
        self.assertEqual(reason, "work_or_employer")

    def test_caps(self):
        self.assertEqual(DAILY_CAP_USD, 1.00)
        self.assertEqual(PER_CALL_CAP_USD, 0.25)
        self.assertGreater(estimate_usd(200_000, 40_000), PER_CALL_CAP_USD)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
