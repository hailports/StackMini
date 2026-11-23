"""Redraft keeps your edits: sentence split, diff, locks, prompt, verbatim check and splice."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hailports.clip import compose, locks, owndraft
from hailports.clip.run import _keep_locks, redraft_doc

ROOT = Path(__file__).resolve().parents[1]

DRAFT = (
    "hey Priya -- yes, the level 3 rule is built in. "
    "ag services goes from $300k to $600k at level 3. "
    "16 dealers sit below $300k today. "
    "want me to bump those up to $300k?"
)
QUOTE = (
    "\n\nFrom: Priya Shah\nSent: Thursday, November 19, 2026 9:02 AM\nTo: Jordan Reyes\nSubject: RE: Goals\n\n"
    "Is the level 3 rule built into the goals file, or do we add it by hand?"
)
S = [p.text for p in locks.split(DRAFT)]


# ---- splitter ---------------------------------------------------------------------------

def test_split_basic_sentences():
    assert S == [
        "hey Priya -- yes, the level 3 rule is built in.",
        "ag services goes from $300k to $600k at level 3.",
        "16 dealers sit below $300k today.",
        "want me to bump those up to $300k?",
    ]


def test_split_keeps_abbreviations_decimals_money_emails_and_urls_whole():
    text = ("the cost is $1.5k, up 3.2% vs. last year. e.g. the north file. "
            "email jordan@acme.example or see https://acme.example/a.b. Dr. Park agreed at 3 p.m. today. J. Lee too!")
    assert [p.text for p in locks.split(text)] == [
        "the cost is $1.5k, up 3.2% vs. last year.",
        "e.g. the north file.",
        "email jordan@acme.example or see https://acme.example/a.b.",
        "Dr. Park agreed at 3 p.m. today.",
        "J. Lee too!",
    ]


def test_split_bullets_and_line_breaks():
    text = "two things:\n- the goals file. it is loaded\n1. first item\n2. second item\n\nthanks"
    pieces = locks.split(text)
    assert [p.text for p in pieces] == ["two things:", "- the goals file.", "it is loaded", "1. first item",
                                        "2. second item", "thanks"]
    # The gaps survive a round trip.
    assert locks.join(pieces) == text


def test_split_ellipsis_carries_on_in_lowercase():
    assert [p.text for p in locks.split("so... yeah. ok")] == ["so... yeah.", "ok"]


# ---- diff -------------------------------------------------------------------------------

def _kinds(placed: str) -> list[tuple[str, str]]:
    return [(r["text"], r["kind"]) for r in locks.diff(placed, DRAFT)]


def test_untouched_draft_has_no_locks():
    rows = locks.diff(DRAFT, DRAFT)
    assert all(r["kind"] == locks.PAISTY and not r["locked"] for r in rows)


def test_edited_and_user_written_sentences_are_locked():
    placed = DRAFT.replace("16 dealers", "17 dealers") + " I can do it Friday."
    kinds = _kinds(placed)
    assert kinds[2] == ("17 dealers sit below $300k today.", locks.EDITED)
    assert kinds[-1] == ("I can do it Friday.", locks.USER)
    assert [k for _, k in kinds[:2]] == [locks.PAISTY, locks.PAISTY]


@pytest.mark.parametrize("edit", [
    lambda s: s.replace("hey Priya -- yes,", "Hey Priya -- yes,"),           # case
    lambda s: s.replace("built in.", "built in!"),                            # punctuation
    lambda s: s.replace("yes, the", "yes,  the"),                             # spacing inside
])
def test_cosmetic_edits_count_as_kept(edit):
    placed = edit(DRAFT)
    first = locks.diff(placed, DRAFT)[0]
    assert first["kind"] == locks.COSMETIC and first["locked"]
    # The user's version is the one locked.
    assert first["text"] == locks.split(placed)[0].text


def test_line_wrapping_between_sentences_is_not_an_edit():
    placed = DRAFT.replace(". ", ".\n")
    assert not any(r["locked"] for r in locks.diff(placed, DRAFT))


def test_reordered_sentences_lock_by_content_in_the_users_order():
    placed = " ".join([S[3], S[0], "Hi again, 17 dealers sit below $300k today.", S[1]])
    rows = locks.diff(placed, DRAFT)
    assert [r["kind"] for r in rows] == [locks.PAISTY, locks.PAISTY, locks.EDITED, locks.PAISTY]
    assert rows[0]["text"] == S[3]


# ---- lock plan --------------------------------------------------------------------------

def test_plan_with_quoted_thread_only_looks_at_the_draft():
    placed = DRAFT.replace("16 dealers", "17 dealers")
    plan = locks.lock_plan(placed + QUOTE, DRAFT)
    assert plan["status"] == "ok"
    assert plan["locks"] == ["17 dealers sit below $300k today."]
    assert not any("From:" in r["text"] for r in plan["sentences"])


def test_deleted_draft_means_fresh_with_no_locks():
    plan = locks.lock_plan("Is the level 3 rule built in?" + QUOTE, DRAFT)
    assert plan["status"] == "gone" and plan["locks"] == []


def test_partial_edit_keeps_only_whats_left():
    placed = S[0] + " " + S[1] + " Friday works for me."
    plan = locks.lock_plan(placed + QUOTE, DRAFT)
    assert plan["status"] == "ok"
    assert plan["locks"] == ["Friday works for me."]


def test_explicit_pins_override_auto_locks():
    placed = DRAFT.replace("16 dealers", "17 dealers")
    # The user unpinned their edit and pinned an untouched sentence.
    plan = locks.lock_plan(placed + QUOTE, DRAFT, pins=[S[1]])
    assert plan["locks"] == [S[1]] and plan["explicit"]
    # An empty pin list is a choice too: nothing kept.
    assert locks.lock_plan(placed, DRAFT, pins=[])["locks"] == []
    # A pin that is no longer in the box is dropped.
    assert locks.lock_plan(placed, DRAFT, pins=["not here at all."])["locks"] == []


def test_fresh_ignores_every_lock():
    placed = DRAFT.replace("16 dealers", "17 dealers") + " Thanks!"
    plan = locks.lock_plan(placed, DRAFT, pins=["Thanks!"], fresh=True)
    assert plan["status"] == "fresh" and plan["locks"] == []


def test_popup_rows_prepin_edits():
    rows = locks.popup_rows(locks.lock_plan(DRAFT.replace("16 dealers", "17 dealers"), DRAFT))
    assert [r["pinned"] for r in rows] == [False, False, True, False]


# ---- prompt, check, splice --------------------------------------------------------------

def _plan():
    return locks.lock_plan(DRAFT.replace("16 dealers", "17 dealers") + " I can do it Friday." + QUOTE, DRAFT)


def test_prompt_block_lists_locks_in_position():
    block = locks.prompt_block(_plan())
    assert "WORD FOR WORD" in block
    assert "KEEP: 17 dealers sit below $300k today." in block
    assert "REWRITE: " + S[0] in block
    assert block.index("KEEP: 17 dealers") < block.index("KEEP: I can do it Friday.")
    assert locks.prompt_block({"locks": []}) == ""


def test_missing_is_verbatim_but_ignores_line_wrapping():
    plan = _plan()
    ok = "hey Priya -- yes.\n17 dealers sit below\n$300k today. I can do it Friday."
    assert locks.missing(ok, plan["locks"]) == []
    assert locks.missing("17 Dealers sit below $300k today. I can do it Friday.", plan["locks"]) == [plan["locks"][0]]


def test_splice_replaces_a_paraphrase_and_appends_a_dropped_lock():
    plan = _plan()
    reply = ("hey Priya -- yes, level 3 is built in. ag services moves to $600k at level 3. "
             "17 dealers are below $300k today. want me to raise them?")
    out = locks.splice(reply, plan)
    assert locks.missing(out, plan["locks"]) == []
    assert "17 dealers are below" not in out           # the paraphrase was swapped, not duplicated
    assert out.index("17 dealers sit below") < out.index("I can do it Friday.")


def test_splice_keeps_original_order_and_position():
    placed = "Hi Priya, thanks for checking. " + " ".join(S[1:3]) + " " + S[3]
    plan = locks.lock_plan(placed, DRAFT)
    assert plan["locks"][0] == "Hi Priya, thanks for checking."
    out = locks.splice("yes, it is built in. want me to bump them?", plan)
    assert out.startswith("Hi Priya, thanks for checking.")


def test_enforce_leaves_a_good_reply_alone():
    plan = _plan()
    good = "hey Priya -- yes. 17 dealers sit below $300k today. I can do it Friday."
    assert locks.enforce(good, plan) == good
    assert locks.enforce("", plan) == ""


# ---- wiring -----------------------------------------------------------------------------

def _scene(field: str) -> dict:
    return {"app": "Mail", "field_text": field, "prior_text": DRAFT, "blocks": []}


def test_redraft_prompt_contains_the_locked_sentences():
    field = DRAFT.replace("16 dealers", "17 dealers") + " I can do it Friday." + QUOTE
    doc = redraft_doc(_scene(field))
    assert doc["prior_draft"]["locks"]["locks"] == ["17 dealers sit below $300k today.", "I can do it Friday."]
    ctx = compose.gather(doc, {"paiste": {"me": "Jordan Reyes"}}, extra_dirs=[], code_roots=[])
    text = compose.messages(ctx, "rules")[-1]["content"]
    assert "KEEP: 17 dealers sit below $300k today." in text
    assert '"I can do it Friday."' in text


def test_popup_pins_and_fresh_reach_the_prompt():
    field = DRAFT.replace("16 dealers", "17 dealers") + QUOTE
    pinned = redraft_doc(dict(_scene(field), redraft_locks={"pins": [S[0]]}))
    note = owndraft.prior_note(pinned["prior_draft"])
    assert "KEEP: " + S[0] in note and "REWRITE: 17 dealers" in note
    fresh = redraft_doc(dict(_scene(field), redraft_locks={"fresh": True}))
    note = owndraft.prior_note(fresh["prior_draft"])
    assert "KEEP:" not in note and "fresh redraft" in note
    # Fresh drops the "keep the user's edits" check too.
    ctx = compose.Context(prior=fresh["prior_draft"])
    assert not compose._prior_issues("hey Priya -- yes. sixteen dealers.", ctx)


def test_unreadable_field_falls_back_to_todays_behaviour():
    doc = redraft_doc({"app": "Mail", "field_text": "", "prior_text": DRAFT, "blocks": []})
    assert "locks" not in doc["prior_draft"]
    assert "It is not in the reply box" in owndraft.prior_note(doc["prior_draft"])


def test_compose_retries_then_splices():
    field = DRAFT.replace("16 dealers", "17 dealers") + " I can do it Friday." + QUOTE
    doc = redraft_doc(_scene(field))
    calls = []

    def model(msgs):
        calls.append(msgs)
        if "Return only JSON" in str(msgs[-1]["content"]):
            return "{}"
        return "hey Priya -- yes, level 3 is built in. want me to bump them up?"

    text, ctx, attempts = compose.draft(doc, {"paiste": {"me": "Jordan Reyes"}}, "rules", model, tries=2,
                                        extra_dirs=[], code_roots=[])
    assert any("word for word" in i for i in attempts[0]["issues"])
    retry_ask = calls[-1][-1]["content"]
    assert "17 dealers sit below $300k today." in retry_ask
    assert locks.missing(text, ctx.locks()) == []


def test_legacy_path_retries_once_then_splices():
    field = DRAFT.replace("16 dealers", "17 dealers") + QUOTE
    doc = redraft_doc(_scene(field))
    asks = []

    def retry(prev, ask):
        asks.append(ask)
        return "hey Priya -- still no luck keeping it."

    out = _keep_locks("hey Priya -- yes, it is built in.", doc, retry)
    assert len(asks) == 1 and "17 dealers sit below $300k today." in asks[0]
    assert "17 dealers sit below $300k today." in out
    # A reply that already keeps the locks costs no retry.
    asks.clear()
    _keep_locks("ok. 17 dealers sit below $300k today.", doc, retry)
    assert asks == []


def test_locks_cli(tmp_path):
    f = tmp_path / "field.txt"
    p = tmp_path / "prior.txt"
    f.write_text(DRAFT.replace("16 dealers", "17 dealers") + QUOTE, encoding="utf-8")
    p.write_text(DRAFT, encoding="utf-8")
    run = subprocess.run([sys.executable, str(ROOT / "contrib/clip/run"), "locks", "--field", str(f), "--prior", str(p)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    doc = json.loads(run.stdout)
    assert doc["status"] == "ok"
    assert [r["pinned"] for r in doc["sentences"]] == [False, False, True, False]


# ---- eval scorecard ---------------------------------------------------------------------

def test_eval_redraft_cases_lock_what_the_rubric_expects():
    sys.path.insert(0, str(ROOT / "scripts" / "paisty_eval"))
    import run_eval
    cases = [c for c in json.loads((ROOT / "scripts/paisty_eval/cases.json").read_text(encoding="utf-8")) if c.get("redraft")]
    assert len(cases) >= 2
    for case in cases:
        doc = run_eval.redraft_doc(case, run_eval.email_doc(case))
        plan = doc["prior_draft"]["locks"]
        assert sorted(plan["locks"]) == sorted(case["rubric"]["must_keep_verbatim"]), case["id"]
        assert case["redraft"]["in_box"] not in doc["field_text"]      # the old draft is not their message
        ref = run_eval.score(case["reference"], case["rubric"])
        assert ref["not_kept"] == [] and ref["score"] == 10.0, (case["id"], ref)
        bad = run_eval.score(case["redraft"]["original"], case["rubric"])
        assert bad["not_kept"], case["id"]


# ---- Swift pin state --------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("swiftc") is None, reason="needs swiftc")
def test_swift_pin_state(tmp_path):
    exe = tmp_path / "redraft-tests"
    build = subprocess.run(["swiftc", "-O", "-o", str(exe), str(ROOT / "contrib/clip/Pulse.swift"),
                            str(ROOT / "contrib/clip/tests/redraft/main.swift")], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr[-2000:]
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "passed" in run.stdout


def test_popup_offers_pins_and_fresh_redraft():
    src = (ROOT / "contrib/clip/PaisteMenu.swift").read_text(encoding="utf-8")
    assert '"Fresh redraft"' in src and "RedraftPins" in src and '"--locks"' in src
    assert "Reply box read from the screen" in (ROOT / "contrib/clip/Pulse.swift").read_text(encoding="utf-8")
    flow = (ROOT / ".github/workflows/paisty-release.yml").read_text(encoding="utf-8")
    assert "tests/test_clip_locks.py" in flow
