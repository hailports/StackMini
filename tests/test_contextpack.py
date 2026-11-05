"""Context packs, edit lessons, and the one rewrite check. No network."""

from __future__ import annotations

import json

from hailports.askdo import gather, respond
from hailports.contextpack import (
    answers_the_ask,
    apply_check,
    extract_ask,
    pack_pages,
    record_pair,
    similar_text,
    skip_unchanged,
    stable_fp,
)
from hailports.draft_runner import run_round
from hailports.idle import skip_unchanged as idle_skip
from hailports.learning import record_pair as learn_record
from hailports.learning import similar as learn_similar
from tests.test_modes import Fake
from hailports.router import Policy, Router


def _page(ref, source, body, **extra):
    return {
        "ref": ref,
        "source": source,
        "title": extra.get("title") or ref,
        "body": body,
        "url": extra.get("url") or "",
    }


def test_pack_keeps_real_pages_and_drops_other_repos():
    pages = [
        _page("card-100", "tracker", "Parent: 900\nDealers cannot edit branch contacts. The account stays read only.\n", title="Branch contacts"),
        _page("card-900", "tracker", "The parent account stays read only for this work.\n", title="Parent account"),
        _page("pull-4", "pull", "AB#100 added a contact mapping.\n", title="contact mapping", url="https://github.com/example/app/pull/4"),
        _page("pull-9", "pull", "AB#100 from somewhere else.\n", title="other", url="https://github.com/other/app/pull/9"),
        _page("rec-200", "record", "card-100 record stays read only.\n", title="rec-200"),
        _page("contact_runbook.md", "runbook", "branch contacts mapping runbook. The account stays read only.\n", title="contact runbook"),
        _page("secret", "page", "password is hunter2 and this file is long enough to clip.\n", title="nope", url="/legal/file"),
    ]
    packed = pack_pages(
        "card-100 dealers cannot edit branch contacts",
        pages,
        ticket_id="100",
        allowed_repos=["example/app"],
    )
    kinds = set(packed["kinds"])
    assert "history" in kinds
    assert "parent" in kinds
    assert "pr" in kinds
    assert "record" in kinds
    assert "runbook" in kinds
    assert "example/app" in packed["text"]
    assert "other/app" not in packed["text"]
    assert "hunter2" not in packed["text"]


def test_lessons_dedupe_and_retrieve(tmp_path):
    folder = tmp_path / "learning"
    first = record_pair(folder, "mail", "e1", "I will look into the contact edit.", "I added the contact mapping. The account stays read only.")
    again = record_pair(folder, "mail", "e1", "I will look into the contact edit.", "I added the contact mapping. The account stays read only.")
    gold = record_pair(folder, "chat", "e2", "the mapping is in and the account stays read only.", "the mapping is in and the account stays read only.")
    assert first == "lesson"
    assert again == "dup"
    assert gold == "gold"
    assert learn_record(folder, "mail", "e3", "I will look into the contact edit tomorrow.", "I added the contact mapping for the branch.") == "lesson"
    block = similar_text(folder, "contact mapping account read only", k=3)
    assert "lesson" in block
    assert "example" in block
    assert learn_similar(folder, "contact mapping account", k=3)
    for line in (folder / "draft_edits.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        assert "draft" not in row
        assert "final" not in row


def test_invented_claim_is_not_staged():
    def complete(messages, max_tokens=80):
        if "Grade one draft" in messages[0]["content"]:
            return {"ok": True, "text": '{"pass": true, "reason": "looks fine"}', "reason": ""}
        return {"ok": True, "text": "I deployed fourteen lambdas last night.", "reason": ""}

    good = apply_check(
        "can they edit contacts?",
        "I added a contact mapping. The account stays read only.",
        "I added the contact mapping. The account stays read only.",
        complete=complete,
    )
    assert good["ok"] is True
    assert good["status"] == "pass"
    held = apply_check(
        "can they edit contacts?",
        "the account stays read only",
        "I deployed fourteen lambdas last night.",
        complete=complete,
    )
    assert held["ok"] is False
    assert held["text"] == ""
    assert "not_in_context" in held["reason"]
    assert held["attempts"] == 2


def test_tag_change_misses_the_ask_and_the_edit_fix_passes():
    ask = (
        "Tags: board-cleanup:2026-08-12\nParent: 900\n"
        "Description:\nInventory the approval groups and design the migration waves."
    )
    tag = 'on 2026-08-12 the tags were updated to include "board-cleanup:2026-08-12".'
    assert extract_ask(ask).lower().startswith("inventory")
    ok, why = answers_the_ask(ask, tag)
    assert ok is False
    assert why.startswith("off_topic")

    def complete(messages, max_tokens=80):
        if "Grade one draft" in messages[0]["content"]:
            return {"ok": True, "text": '{"pass": true, "answers_ask": true, "reason": "mentions it"}', "reason": ""}
        return {"ok": True, "text": tag, "reason": ""}

    held = apply_check(ask, "Tags: board-cleanup:2026-08-12\nParent: 900", tag, complete=complete)
    assert held["ok"] is False
    assert held["text"] == ""
    assert "off_topic" in held["reason"]

    edit_ask = "Description:\nPermissions error when trying to edit employee records."
    edit = "the earlier fix did not allow dealers to edit branch contacts. the account stays read only."
    assert answers_the_ask(edit_ask, edit)[0] is True
    good = apply_check(
        edit_ask,
        "dealers can edit branch contacts. the account stays read only.",
        edit,
        complete=lambda messages, max_tokens=80: {"ok": True, "text": '{"pass": true, "reason": "answers the edit"}', "reason": ""},
    )
    assert good["ok"] is True
    assert good["status"] == "pass"


def test_unparsed_grade_retries_once_and_then_holds():
    calls = {"n": 0}

    def messy(messages, max_tokens=80):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": True, "text": "looks fine", "reason": ""}
        return {"ok": True, "text": '{"pass": true, "reason": "answers the edit"}', "reason": ""}

    kept = apply_check(
        "can they edit contacts?",
        "they can edit contacts",
        "they can edit the contacts now",
        complete=messy,
    )
    assert kept["ok"] is True
    assert calls["n"] == 2

    held = apply_check(
        "can they edit contacts?",
        "they can edit contacts",
        "they can edit the contacts now",
        complete=lambda messages, max_tokens=80: {"ok": True, "text": "still prose", "reason": ""},
    )
    assert held["ok"] is False
    assert held["text"] == ""
    assert held["reason"] == "grade_unparsed"
    assert held["attempts"] == 2


def test_skip_unchanged_avoids_a_repeat():
    text = "The account stays read only after the contact mapping."
    fp = stable_fp(text + "\n2026-11-04")
    assert fp == stable_fp(text + "\n2026-11-05")
    assert skip_unchanged({"hash": "abc", "fp": fp}, {"text": text}) is True
    assert skip_unchanged({"hash": "abc"}, {"text": text, "fp": "ffff"}) is True
    assert skip_unchanged({"hash": "abc", "fp": "other"}, {"text": text, "fp": fp}) is False
    assert skip_unchanged({}, {"text": text}) is False
    assert idle_skip({"hash": "abc"}, {"text": text, "fp": "ffff"}) is True


def test_gather_adds_pages_without_an_extra_draft(tmp_path):
    book = tmp_path / "runbooks"
    book.mkdir()
    (book / "backup.md").write_text("# Backup\n\nCopy the notes file to a second disk.\n", encoding="utf-8")
    pages = [
        _page("card-100", "tracker", "Parent: 900\nCopy the notes when the check passes.\n", title="Notes"),
        _page("card-900", "tracker", "Parent item for the notes check.\n", title="Parent"),
    ]
    bundle = gather("copy the notes for card-100", book, pages=pages, allowed_repos=["example/app"])
    assert bundle["changed"] is False
    assert "read-context" in bundle["actions"]
    assert "history" in bundle["links"]
    local = Fake("local", "Draft only. No file was changed.")
    router = Router("local", local, None, Policy())
    reply = respond(router, "copy the notes for card-100", bundle)
    assert reply.text == "Draft only. No file was changed."
    assert len(local.calls) == 1


def test_check_flag_holds_a_second_failure():
    class Scripted:
        def __init__(self):
            self.calls = []
            self.provider = "local"
            self.model = "local-model"

        def chat(self, messages, max_tokens=512, temperature=0.2):
            from hailports.providers.base import Reply
            self.calls.append(messages)
            if "Grade one draft" in messages[0]["content"]:
                return Reply('{"pass": false, "reason": "no"}', "local", "m", 1, 1, {})
            if "Rewrite" in messages[0]["content"]:
                return Reply("a model rewrote this", "local", "m", 1, 1, {})
            return Reply("hello there friend", "local", "m", 1, 1, {})

    local = Scripted()
    router = Router("local", local, None, Policy())
    bundle = {"kind": "ask", "notes": ["the account stays read only"], "actions": ["read-context"], "check": True, "changed": False}
    reply = respond(router, "edit the contacts?", bundle)
    assert reply.text == ""
    assert reply.raw.get("held") is True
    assert len(local.calls) == 3


def test_runner_logs_links_and_does_not_send():
    pages = [
        _page("card-100", "tracker", "Parent: 900\nThe account stays read only for branch contacts.\n", title="Branch contacts"),
        _page("card-900", "tracker", "Parent stays read only.\n", title="Parent"),
    ]
    result = run_round([{
        "message_id": "m1",
        "subject": "branch contacts",
        "from": "sam",
        "body": "Can they edit branch contacts on card-100?",
        "ask": "edit branch contacts",
        "sources": ["The account stays read only for branch contacts."],
        "pages": pages,
        "ticket_id": "100",
        "may_open": True,
    }])
    assert result["drafted"] == 1
    assert result["drafts"][0]["sent"] is False
    assert "history" in result["drafts"][0]["links"]
    assert "parent" in result["drafts"][0]["links"]


def test_quoted_history_is_not_the_ask_and_a_miss_keeps_the_best_text():
    thread = (
        "Sounds good. I will put the detail together.\n"
        "From: Old Note\n"
        "Following up on this?\n"
        "Is this a setting that you changed?\n"
    )
    ask = extract_ask(thread)
    assert "setting" not in ask.lower()
    assert "detail" in ask.lower()
    ok, why = answers_the_ask(thread, "sounds good. the stages listed are what is on file today.")
    assert ok is True, why

    def complete(messages, max_tokens=80, json_mode=False):
        if "Grade one draft" in messages[0]["content"]:
            return {"ok": True, "text": '{"pass": false, "answers_ask": false, "reason": "off_topic"}', "reason": ""}
        return {"ok": False, "text": "", "reason": "empty"}

    held = apply_check(
        "Can you approve the discount?",
        "no discount is written here",
        "the tags were updated yesterday",
        complete=complete,
    )
    assert held["ok"] is False
    assert held["text"] == ""
    assert held["best_text"]
    problem = "Description:\nPermissions error when trying to edit employee records."
    assert _latest_is_not_ack(problem)


def _latest_is_not_ack(text: str) -> bool:
    from hailports.contextpack import _latest_is_ack
    return _latest_is_ack(text) is False


def test_off_topic_retries_then_reviews_without_blocking_other_mail():
    from hailports.providers.base import Reply

    calls = {"n": 0, "fetch": 0}

    class Router:
        def complete(self, messages, task=None, max_tokens=512):
            calls["n"] += 1
            blob = messages[0]["content"]
            if "Grade one draft" in blob:
                return Reply('{"pass": false, "answers_ask": false, "reason": "off_topic"}', "l", "m", 1, 1, {})
            if "Rewrite" in blob:
                return Reply("hello there friend", "l", "m", 1, 1, {})
            return Reply("hello there friend [1]", "l", "m", 1, 1, {})

    def fetch():
        calls["fetch"] += 1
        return ""

    held = {
        "message_id": "m-hold",
        "subject": "note",
        "from": "pat",
        "body": "",
        "ask": "Can you approve the discount?",
        "sources": ["The discount is not approved."],
        "check": True,
        "fetch_body": fetch,
    }
    other = {
        "message_id": "m-other",
        "subject": "notes",
        "from": "sam",
        "body": "hello",
        "ask": "notes",
        "sources": ["The notes are on the desk."],
    }
    router = Router()
    first = run_round([held, other], router=router)
    assert first["drafted"] == 1
    assert first["drafts"][0]["message_id"] == "m-other"
    assert first["drafts"][0]["sent"] is False
    assert first["prior"]["m-hold"]["grade_fails"] == 1
    assert any(row["skip"] == "hold_wait" for row in first["skipped"])
    opened = calls["fetch"]
    used = calls["n"]
    second = run_round([held, other], router=router, prior=first["prior"])
    assert calls["fetch"] == opened
    assert calls["n"] > used
    assert second["drafted"] == 1
    assert second["drafts"][0]["sent"] is False
    prior = dict(second["prior"])
    prior["m-hold"] = dict(prior["m-hold"])
    prior["m-hold"]["next_ok"] = 0
    third = run_round([held], router=router, prior=prior)
    assert third["prior"]["m-hold"]["grade_fails"] == 2
    prior = dict(third["prior"])
    prior["m-hold"] = dict(prior["m-hold"])
    prior["m-hold"]["next_ok"] = 0
    fourth = run_round([held], router=router, prior=prior)
    assert fourth["prior"]["m-hold"]["grade_fails"] == 3
    assert fourth["prior"]["m-hold"]["settled"] is True
    assert fourth["drafted"] == 1
    assert fourth["drafts"][0]["review"] is True
    assert fourth["drafts"][0]["sent"] is False
    fifth = run_round([held], router=router, prior=fourth["prior"])
    assert fifth["drafted"] == 0
    assert fifth["skipped"][0]["skip"] == "hold_settled"
    sixth = run_round([held], router=router, prior=fifth["prior"])
    assert sixth["skipped"] == []
    assert sixth["drafted"] == 0
