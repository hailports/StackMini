"""Full-context paisty drafts: thread reading, attachments, computed facts, the check, the retry.

The tier-rule case is an anonymized copy of a real miss: the draft said the logic was missing,
asked for the file that was already attached, and answered an ask that was settled days before.
"""

import json
from pathlib import Path

from hailports.clip import attach, compose, datafacts, rulenotes, thread

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "paisty_eval"
CASES = {c["id"]: c for c in json.loads((ROOT / "scripts" / "paisty_eval" / "cases.json").read_text())}
ME = "Jordan Reyes, Jordan"

import sys
sys.path.insert(0, str(ROOT / "scripts" / "paisty_eval"))
import run_eval  # noqa: E402

BAD = ("hey morgan --\n\nthe logic does not build in the asterisk for level 3 targets. if you need that adjustment, "
       "please send the updated file and i will reload it.\n\ncasey -- i have the goals from morgan & robin. if you can "
       "send over the tech goals pat sent you, i can get everything loaded once we have the all-clear.")


def _ctx(case_id):
    case = CASES[case_id]
    doc = run_eval.chat_doc(case) if case.get("chat") else run_eval.email_doc(case)
    cfg = {"paiste": {"me": ME}}
    return compose.gather(doc, cfg, extra_dirs=[FIX], code_roots=[FIX / "repo" / "src", FIX / "repo" / "docs"])


def test_thread_newest_first_and_settled():
    ctx = _ctx("tier_rule")
    t = ctx.thread
    assert t.ok and t.newest_other().first == "Morgan"
    assert [m.first for m in t.open_messages()] == ["Morgan"]
    assert "Casey" in ctx.settled_people
    assert ctx.people == ["Morgan"]
    assert "already handled" in ctx.thread_block


def test_outlook_names_flip_and_cc():
    addr = "morgan.hale" + "@" + "example.com"
    name, email = thread.person(f"Hale, Morgan <{addr}>")
    assert name == "Morgan Hale" and email == addr
    t = thread.parse(run_eval.email_doc(CASES["tier_rule"])["field_text"], ME.split(", "))
    assert t.messages[0].cc == ["Casey Brandt", "Robin Vance"]


def test_attachment_found_by_subject_and_read():
    ctx = _ctx("tier_rule")
    assert "Program Goals - North 26-27.xlsx" in ctx.files
    assert "RULE IS IN THE NOTES" in ctx.facts_block
    assert "Red Mesa Water ($75k)" in ctx.facts_block
    assert "Prairie Pump Co at $300k -> $600k" in ctx.facts_block


def test_rule_parse_from_code_and_prose():
    code = "level 3 = ROUND(goal + 300000, 0)\n when 300000 <= goal <= 1000000\n = ROUND(goal * 1.20, 0) otherwise"
    prose = "* Level 3: goals $300K-$1M = goal + $300K; goals over $1M = 120% of goal"
    for text in (code, prose):
        rule = datafacts.parse_rule(text)
        assert rule["offset"] == 300000 and rule["low"] == 300000 and rule["high"] == 1000000 and rule["mult"] == 1.2


def test_rulenotes_prefers_formula():
    hits = rulenotes.search("does your logic build in the asterisk for Level 3 targets?", [FIX / "repo" / "docs"])
    assert "300000" in hits


def test_check_rejects_the_original_bad_draft():
    ctx = _ctx("tier_rule")
    issues = compose.check(BAD, ctx)
    text = " ".join(issues)
    assert "not built in" in text or "missing" in text
    assert "already attached" in text
    assert "Casey" in text
    assert run_eval.score(BAD, CASES["tier_rule"]["rubric"])["score"] < 5


def test_check_passes_the_reference():
    ctx = _ctx("tier_rule")
    assert compose.check(CASES["tier_rule"]["reference"], ctx) == []


def test_check_flags_invented_number():
    ctx = _ctx("tier_rule")
    issues = compose.check("hey Morgan -- yes, it's built in. red mesa water would land at $375k.", ctx)
    assert any("375k" in i for i in issues)


def test_draft_retries_until_clean():
    case = CASES["tier_rule"]
    doc = run_eval.email_doc(case)
    replies = iter(['{"asks": []}', BAD, case["reference"]])
    seen = []

    def fake(messages):
        seen.append(messages)
        return next(replies)

    text, ctx, attempts = compose.draft(doc, {"paiste": {"me": ME}}, "rules", fake, extra_dirs=[FIX],
                                        code_roots=[FIX / "repo" / "docs"])
    from hailports.clip import shape
    assert text == shape.finish(case["reference"], "email", ME.split(", "))
    assert text.startswith("hey Morgan,") and text.endswith("best,\nJordan")
    assert len(attempts) == 2 and attempts[0]["issues"]
    roles = [m["role"] for m in seen[-1]]
    assert roles == ["system", "user", "assistant", "user"]  # strictly alternating for local servers


def test_two_people_both_addressed_and_filtered():
    ctx = _ctx("two_people")
    assert ctx.people == ["Avery", "Taylor"]
    assert "4 of 6 rows have Account Status = Inactive" in ctx.facts_block
    assert "$25,600" in ctx.facts_block
    issues = compose.check("hey Avery -- 4 orders, $25,600 total.", ctx)
    assert any("Taylor" in i for i in issues)


def test_chat_holding_reply_keeps_ask_open():
    ctx = _ctx("chat_dupes")
    assert ctx.people == ["Priya"]
    assert "a.ng (2x)" in ctx.facts_block


def test_readers_cover_each_type():
    assert "SSO" in attach.extract(FIX / "Dealer Portal Spec v2.docx").text
    assert "March 31, 2027" in attach.extract(FIX / "Telematics Services Agreement.pdf").text
    tabs = attach.extract(FIX / "Territory Budgets 2027.xlsx").tables
    assert [t.name for t in tabs] == ["East", "West"]
    facts = datafacts.facts(tabs, "totals for east and west", "", "Territory Budgets")
    assert "total $1.3m" in facts and "total $900k" in facts
    assert attach.extract(FIX / "inactive_orders_2026-11-18.csv").tables[0].rows


def test_image_without_ocr_goes_to_vision(monkeypatch):
    monkeypatch.setattr(attach, "ocr_image", lambda path: "")
    ctx = _ctx("image_table")
    assert ctx.images and ctx.images[0].startswith("data:image/png;base64,")
    msgs = compose.messages(ctx, "rules")
    assert isinstance(msgs[1]["content"], list) and msgs[1]["content"][1]["type"] == "image_url"


def test_wants_full_for_threads_files_and_shots():
    assert compose.wants_full(run_eval.email_doc(CASES["stale_ask"]), {})
    assert compose.wants_full({"images": ["/x.png"]}, {})
    assert compose.wants_full({"field_text": "see report.xlsx"}, {})
    assert not compose.wants_full({"field_text": "lunch at noon?"}, {})


def test_references_score_full_marks():
    for case in CASES.values():
        assert run_eval.score(case["reference"], case["rubric"])["score"] == 10.0, case["id"]


def test_screenshot_scene_uses_transcript(tmp_path, monkeypatch):
    from hailports.clip import screenshot
    monkeypatch.setattr(attach, "ocr_image", lambda path: "ocr text")
    png = FIX / "tier_rule.png"
    doc = screenshot.scene_for([png], lambda m: "From: Hale, Morgan\nSent: Thursday\nTo: Reyes, Jordan\nSubject: RE: x\n\nbody?")
    assert doc["field_text"].startswith("From: Hale, Morgan")
    assert doc["window_ocr"] == "ocr text" and doc["images"] == [str(png)]
