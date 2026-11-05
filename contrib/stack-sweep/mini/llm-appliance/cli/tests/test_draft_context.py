"""Context packs, edit lessons, and the self-check. No network and no send."""
import json
import sqlite3
import unittest
from pathlib import Path

import draft_context as dc


def _db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "create virtual table docs using fts5(source, ref, ts, url, title, participants, body)"
    )
    rows = [
        ("ado", "redacted-ticket", "1", "", "Valley dealers", "",
         "State: Active\nParent: 370000\nDescription: valley pro dealers cannot edit branch contacts."),
        ("ado", "redacted-ticket", "1", "", "Parent account", "", "State: Active\nDescription: account stays read only."),
        ("github_pr", "pr_12", "1", "https://github.com/operator/operator-operator/pull/12",
         "contact mapping", "", "redacted-ticket state: open\nAdded a contact mapping."),
        ("github_pr", "pr_99", "1", "https://github.com/other/app/pull/9", "other", "", "redacted-ticket"),
        ("operator", "redacted-ticket", "1", "", "redacted-ticket branch", "", "redacted-ticket valley dealers record stays read only."),
        ("sp_corpus", "play-1", "1", "", "contact playbook", "", "valley dealers contact mapping playbook steps."),
    ]
    con.executemany("insert into docs values (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


class PackTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.id().replace(".", "_"))
        # isolated under tmp via env in the test runner; set here
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "claude-stack/data").mkdir(parents=True)
        _db(self.tmp / "claude-stack/data/work_context.db")
        book = self.tmp / "claude-stack/docs"
        book.mkdir(parents=True)
        (book / "contact_runbook.md").write_text(
            "valley dealers contact mapping runbook. The account stays read only.\n",
            encoding="utf-8",
        )
        (book / "legal").mkdir()
        (book / "legal" / "secret_runbook.md").write_text("valley dealers password is hunter2\n", encoding="utf-8")

    def test_pack_links_real_pages_and_skips_other_repos(self):
        pack = dc.build_pack(
            channel="ado",
            subject="ADO #378888 valley dealers",
            body="valley pro dealers cannot edit branch contacts. See redacted-ticket.",
            ticket_id="378888",
            thread=["operator, the contact edit is still blocked."],
            description="Earlier fix left the account read only.",
            surface="ado",
            home=self.tmp,
            use_cache=False,
        )
        kinds = set(pack["kinds"])
        self.assertIn("history", kinds)
        self.assertIn("parent", kinds)
        self.assertIn("pr", kinds)
        self.assertIn("operator", kinds)
        self.assertIn("comments", kinds)
        blob = pack["text"]
        self.assertIn("operator-operator", blob)
        self.assertNotIn("other/app", blob)
        self.assertNotIn("hunter2", blob)
        self.assertTrue(any(row["kind"] == "runbook" for row in pack["links"]))
        dc.log_links("ado", "378888", pack["links"], home=self.tmp)
        log = (self.tmp / "claude-stack/data/learning/draft_context_links.jsonl").read_text(encoding="utf-8")
        self.assertIn("378888", log)
        self.assertNotIn("hunter2", log)

    def test_data_line_blocks_the_pack(self):
        pack = dc.build_pack(
            channel="outlook",
            subject="password",
            body="password is hunter2",
            surface="outlook",
            home=self.tmp,
        )
        self.assertEqual(pack["blocked"], "data_line")
        self.assertEqual(pack["links"], [])

    def test_lessons_dedupe_and_retrieve(self):
        first = dc.store_outcome(
            "outlook", "e1",
            "I will look into the contact edit.",
            "I added the contact mapping. The account stays read only.",
            home=self.tmp,
        )
        again = dc.store_outcome(
            "outlook", "e1",
            "I will look into the contact edit.",
            "I added the contact mapping. The account stays read only.",
            home=self.tmp,
        )
        gold = dc.store_outcome(
            "zoom", "e2",
            "the mapping is in and the account stays read only.",
            "the mapping is in and the account stays read only.",
            home=self.tmp,
        )
        self.assertEqual(first, "lesson")
        self.assertEqual(again, "dup")
        self.assertEqual(gold, "gold")
        block = dc.similar_block("contact mapping account read only", k=3, home=self.tmp)
        self.assertIn("lesson", block)
        self.assertIn("example", block)
        lessons = (self.tmp / "claude-stack/data/learning/draft_edits.jsonl").read_text(encoding="utf-8")
        self.assertNotIn('"draft"', lessons)

    def test_self_check_rewrites_once_then_holds(self):
        calls = {"n": 0}

        def complete(messages, max_tokens=80):
            calls["n"] += 1
            blob = messages[-1]["content"]
            if "Grade one draft" in messages[0]["content"]:
                if "invented fourteen" in blob:
                    return {"ok": True, "text": '{"pass": false, "reason": "invented"}', "reason": ""}
                return {"ok": True, "text": '{"pass": true, "reason": "grounded"}', "reason": ""}
            return {"ok": True, "text": "I added the contact mapping. the account stays read only.", "reason": ""}

        good = dc.apply_check(
            "can dealers edit contacts?",
            "I added a contact mapping. The account stays read only.",
            "I added the contact mapping. the account stays read only.",
            surface="ado",
            complete=complete,
            home=self.tmp,
        )
        self.assertTrue(good["ok"])
        self.assertEqual(good["status"], "pass")
        bad = dc.apply_check(
            "can dealers edit contacts?",
            "I added a contact mapping. The account stays read only.",
            "the bot invented fourteen fixes",
            surface="ado",
            complete=complete,
            home=self.tmp,
        )
        self.assertTrue(bad["ok"])
        self.assertEqual(bad["status"], "rewrite_pass")
        self.assertNotIn("bot", bad["text"])

        def always_fail(messages, max_tokens=80):
            if "Grade one draft" in messages[0]["content"]:
                return {"ok": True, "text": '{"pass": false, "reason": "invented"}', "reason": ""}
            return {"ok": True, "text": "still invented fourteen dates", "reason": ""}

        held = dc.apply_check("ask", "context fact", "invented fourteen claims", surface="ado", complete=always_fail, home=self.tmp)
        self.assertFalse(held["ok"])
        self.assertEqual(held["text"], "")
        self.assertEqual(held["attempts"], 2)
        rows = [
            json.loads(line)
            for line in (self.tmp / "claude-stack/data/learning/draft_selfcheck.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(any(row["status"] == "fail" for row in rows))
        self.assertTrue(any(row["status"] == "pass" for row in rows))

    def test_invented_claim_is_held_even_when_the_model_says_pass(self):
        def complete(messages, max_tokens=80):
            if "Grade one draft" in messages[0]["content"]:
                return {"ok": True, "text": '{"pass": true, "reason": "looks fine"}', "reason": ""}
            return {"ok": True, "text": "I deployed fourteen lambdas last night.", "reason": ""}

        held = dc.apply_check(
            "can dealers edit contacts?",
            "the account stays read only",
            "I deployed fourteen lambdas last night.",
            surface="ado",
            complete=complete,
            home=self.tmp,
        )
        self.assertFalse(held["ok"])
        self.assertEqual(held["text"], "")
        self.assertIn("not_in_context", str(held.get("reason") or ""))

    def test_canned_mention_is_gone(self):
        import mention_comment_watch as watch
        text = Path(watch.__file__).read_text(encoding="utf-8")
        self.assertNotIn("I saw your note", text)
        self.assertIn("draft_hold no_grounded_reply", text)

    def test_guard_does_not_stage_a_second_failure(self):
        dc.stash({"text": "the account stays read only", "links": [{"kind": "history", "ref": "wi_1", "text": "read only"}]})

        def complete(messages, max_tokens=80):
            if "Grade one draft" in messages[0]["content"]:
                return {"ok": True, "text": '{"pass": false, "reason": "no"}', "reason": ""}
            return {"ok": True, "text": "a model rewrote this", "reason": ""}

        original = dc.local_complete
        dc.local_complete = complete
        try:
            out = dc.guard({"ok": True, "text": "hello there friend"}, ask="edit contacts?", surface="ado", home=self.tmp)
        finally:
            dc.local_complete = original
            dc.stash(None)
        self.assertEqual(out["text"], "")
        self.assertTrue(str(out.get("error") or "").startswith("hold:"))
        self.assertFalse(out.get("sent"))


ASK_368311 = (
    "Task 368311: Inventory approval-process public groups and design migration waves\n"
    "Tags: board-cleanup:2026-08-12\n"
    "Parent: 367071\n"
    "Description:\n"
    "Inventory the active approval processes that depend on public groups and propose safe replacement waves. "
    "The inventory names each active approval process, public group, members, business owner, and usage. "
    "Each group has a keep, replace, consolidate, or retire recommendation. "
    "Migration waves include regression coverage and rollback criteria."
)
DRAFT_368311 = (
    'on 2026-08-12, the tags for Task 368311 and its parent User Story 367071 were updated to include "board-cleanup:2026-08-12".'
)
ASK_378888 = (
    "Bug 378888: Valley Pro operator - My Dealership unable to edit employees\n"
    "Description:\n"
    "Permissions error when trying to edit employee information."
)
DRAFT_378888 = (
    "the issue was that the earlier fix didn't allow Valley Pro dealers to edit branch contacts. "
    "A contact mapping was added to the DOM sharing set so they can edit contacts while the account stays read-only."
)


class AskGradeTests(unittest.TestCase):
    def test_368311_tag_change_does_not_answer_the_ask(self):
        ok, why = dc.answers_the_ask(ASK_368311, DRAFT_368311)
        self.assertFalse(ok)
        self.assertIn("off_topic", why)
        self.assertIn("approval", dc.extract_ask(ASK_368311).lower())

        def complete(messages, max_tokens=80):
            if "Grade one draft" in messages[0]["content"]:
                return {"ok": True, "text": '{"pass": true, "answers_ask": true, "reason": "mentions the ticket"}', "reason": ""}
            return {"ok": True, "text": DRAFT_368311, "reason": ""}

        held = dc.apply_check(
            ASK_368311,
            "Tags: board-cleanup:2026-08-12\nParent: 367071\nTask 368311",
            DRAFT_368311,
            surface="ado",
            complete=complete,
            home=self.tmp if hasattr(self, "tmp") else None,
        )
        self.assertFalse(held["ok"])
        self.assertEqual(held["text"], "")
        self.assertIn("off_topic", held["reason"])

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="askgrade-"))
        (self.tmp / "claude-stack/data/learning").mkdir(parents=True)

    def test_378888_edit_fix_answers_the_ask(self):
        ok, why = dc.answers_the_ask(ASK_378888, DRAFT_378888)
        self.assertTrue(ok, why)

        def complete(messages, max_tokens=80):
            return {"ok": True, "text": '{"extracted_ask":"edit employees","answers_ask":true,"pass":true,"reason":"explains the edit"}', "reason": ""}

        good = dc.apply_check(
            ASK_378888,
            "dealers could not edit branch contacts. a contact mapping was added. the account stays read-only.",
            DRAFT_378888,
            surface="ado",
            complete=complete,
            home=self.tmp,
        )
        self.assertTrue(good["ok"])
        self.assertEqual(good["status"], "pass")
        self.assertIn("edit", good["text"])

    def test_unparsed_grade_retries_once_then_holds(self):
        calls = {"n": 0}

        def messy_then_json(messages, max_tokens=80):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ok": True, "text": "looks fine to me", "reason": ""}
            return {"ok": True, "text": '{"pass": true, "reason": "answers the edit"}', "reason": ""}

        kept = dc.apply_check(
            "can dealers edit contacts?",
            "dealers can edit contacts",
            "dealers can edit the contacts now",
            surface="ado",
            complete=messy_then_json,
            home=self.tmp,
        )
        self.assertTrue(kept["ok"])
        self.assertIn("edit", kept["text"])
        self.assertEqual(calls["n"], 2)

        def never_json(messages, max_tokens=80):
            return {"ok": True, "text": "still not json", "reason": ""}

        held = dc.apply_check(
            "can dealers edit contacts?",
            "dealers can edit contacts",
            "dealers can edit the contacts now",
            surface="ado",
            complete=never_json,
            home=self.tmp,
        )
        self.assertFalse(held["ok"])
        self.assertEqual(held["text"], "")
        self.assertEqual(held["reason"], "grade_unparsed")
        self.assertEqual(held["attempts"], 2)

    def test_tolerant_grade_parser(self):
        self.assertTrue(dc._parse_grade('note {"pass": true, "reason": "ok"}')["pass"])
        self.assertTrue(dc._parse_grade('pass: true, reason: "answers"')["pass"])
        self.assertFalse(dc._parse_grade('{"pass": true, "answers_ask": false, "reason": "tag only"}')["pass"])
        self.assertEqual(dc._parse_grade("no grade here")["reason"], "grade_unparsed")


if __name__ == "__main__":
    unittest.main()



class QuotedAskTests(unittest.TestCase):
    def test_quoted_questions_are_not_the_latest_ask(self):
        thread = (
            "Sounds good operator, I am offsite this week and I will aim to put some detail together around this.\n"
            "From: Someone\n"
            "Following up on this?\n"
            "Checking in to see if this is a setting that you have implemented?\n"
        )
        ask = dc.extract_ask(thread)
        self.assertNotIn("setting", ask.lower())
        self.assertIn("detail", ask.lower())
        ok, why = dc.answers_the_ask(
            thread,
            "sounds good. the stages I sent are what is in operator today.",
        )
        self.assertTrue(ok, why)

    def test_failed_check_keeps_best_text(self):
        def complete(messages, max_tokens=80, json_mode=False):
            if "Grade one draft" in messages[0]["content"]:
                return {
                    "ok": True,
                    "text": '{"pass": false, "answers_ask": false, "reason": "off_topic"}',
                    "reason": "",
                }
            return {"ok": False, "text": "", "reason": "empty"}

        held = dc.apply_check(
            "Can you approve the discount?",
            "no discount is written here",
            "the tags were updated yesterday",
            surface="outlook",
            complete=complete,
            home=Path("/tmp"),
        )
        self.assertFalse(held["ok"])
        self.assertEqual(held["text"], "")
        self.assertIn("tags", held.get("best_text") or "")


    def test_problem_statement_is_not_an_ack(self):
        self.assertFalse(dc._latest_is_ack(ASK_378888))
        self.assertTrue(dc._latest_is_ack(
            "Sounds good operator, I am offsite this week and I will aim to put some detail together around this."
        ))

        def complete(messages, max_tokens=80, json_mode=False):
            return {
                "ok": True,
                "text": '{"pass": false, "answers_ask": false, "reason": "off_topic"}',
                "reason": "",
            }

        import tempfile
        from pathlib import Path as _P
        home = _P(tempfile.mkdtemp(prefix="ack-"))
        (home / "claude-stack/data/learning").mkdir(parents=True)
        held = dc.apply_check(
            ASK_378888,
            "permissions error when trying to edit employee information",
            "the tags were updated yesterday",
            surface="ado",
            complete=complete,
            home=home,
        )
        self.assertFalse(held["ok"])
        self.assertNotEqual(held.get("reason"), "latest_note")
