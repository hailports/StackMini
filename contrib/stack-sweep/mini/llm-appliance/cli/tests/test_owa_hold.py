"""Hold backoff retries a few times, then stops. It does not park for a year."""
import tempfile
import time
import unittest
from pathlib import Path

import owa_compose_backoff as backoff


class HoldBackoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        backoff.PATH = self.tmp / "backoff.json"

    def test_hold_waits_then_retries_then_reviews(self):
        row = backoff.record_empty("m1", "h1", "hold:off_topic: old question", best_text="sounds good")
        self.assertEqual(row["fails"], 1)
        self.assertFalse(row["blocked"])
        self.assertEqual(backoff.peek("m1", "h1"), "wait")
        self.assertEqual(backoff.check("m1", "h1"), "owa_compose_backoff")
        self.assertEqual(backoff.check("m1", "h2"), "")
        data = backoff._load()
        data["m1"]["next_ok"] = time.time() - 5
        backoff._save(data)
        self.assertEqual(backoff.peek("m1", "h1"), "")
        backoff.record_empty("m1", "h1", "hold:off_topic: old question", best_text="shorter")
        backoff.record_empty("m1", "h1", "hold:off_topic: old question", best_text="sounds good Nick the stages are current")
        self.assertEqual(backoff.peek("m1", "h1"), "review")
        self.assertIn("stages", backoff.best_text("m1"))
        backoff.mark_reviewed("m1", "told")
        self.assertEqual(backoff.peek("m1", "h1"), "settled")

    def test_year_long_block_becomes_one_review(self):
        backoff.record_empty("old", "h", "hold:askgate:no_pass")
        backoff.record_empty("old", "h", "hold:askgate:no_pass")
        backoff.record_empty("old", "h", "hold:askgate:no_pass")
        data = backoff._load()
        data["old"]["blocked"] = True
        data["old"]["next_ok"] = time.time() + 365 * 24 * 3600
        data["old"]["review_due"] = False
        backoff._save(data)
        healed = backoff.heal_permanent_holds()
        self.assertIn("old", healed)
        self.assertEqual(backoff.peek("old", "h"), "review")


if __name__ == "__main__":
    unittest.main()
