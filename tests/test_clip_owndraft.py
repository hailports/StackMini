"""Redraft: paisty finds its own placed draft (exact, edited, partial), never answers it,
keeps the user's edits, and replaces it instead of appending."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from hailports.clip import compose, owndraft

ROOT = Path(__file__).resolve().parents[1]

DRAFT = (
    "hey Priya -- yes, the level 3 rule is built in. ag services goes from $300k to $600k at level 3. "
    "16 dealers sit below $300k today; want me to bump those up to $300k so level 3 lands at $600k?"
)
QUOTE = (
    "\n\nFrom: Priya Shah\nSent: Thursday, November 19, 2026 9:02 AM\nTo: Jordan Reyes\nSubject: RE: Goals\n\n"
    "Is the level 3 rule built into the goals file, or do we add it by hand?"
)


def test_exact_placement_is_found_and_replaced():
    field = DRAFT + QUOTE
    spot = owndraft.locate(field, DRAFT)
    assert spot.kind == owndraft.EXACT
    assert field[spot.start:spot.end] == DRAFT
    plan = owndraft.plan(field, DRAFT)
    assert plan["mode"] == "replace" and plan["start"] == 0 and plan["end"] == len(DRAFT)


def test_whitespace_and_case_changes_still_count_as_exact():
    field = DRAFT.replace(". ", ".\n").replace("hey", "Hey") + QUOTE
    assert owndraft.locate(field, DRAFT).kind == owndraft.EXACT


def test_edited_draft_is_found_with_the_users_changes():
    edited = DRAFT.replace("16 dealers", "17 dealers").replace("hey Priya -- ", "Hi Priya, ") + " Thanks!"
    field = edited + QUOTE
    spot = owndraft.locate(field, DRAFT)
    assert spot.kind == owndraft.EDITED
    # The whole edited paragraph, including the user's new last word, is the placed draft.
    assert spot.placed == edited
    assert "17" in spot.added and "16" in spot.removed
    assert "thanks" in spot.added
    # The quoted thread under it is never part of the range.
    assert "From: Priya" not in spot.placed


def test_partial_paste_is_found():
    half = DRAFT[:110]
    field = half + QUOTE
    spot = owndraft.locate(field, DRAFT)
    assert spot.kind in (owndraft.PARTIAL, owndraft.EDITED)
    assert spot.placed.strip() == half.strip()


def test_unrelated_text_is_not_mistaken_for_the_draft():
    field = "Sounds good, I'll send the deck after lunch." + QUOTE
    assert owndraft.locate(field, DRAFT).kind == owndraft.NONE
    assert owndraft.plan(field, DRAFT)["mode"] == "insert"


def test_empty_field_means_insert_not_append():
    assert owndraft.plan("", DRAFT) == {"mode": "insert", "kind": "none"}


def test_replace_range_keeps_text_around_it():
    field = "Draft notes above\n\n" + DRAFT + QUOTE
    plan = owndraft.plan(field, DRAFT)
    new = field[: plan["start"]] + "NEW REPLY" + field[plan["end"]:]
    assert new.startswith("Draft notes above\n\nNEW REPLY")
    assert new.endswith(QUOTE)
    assert DRAFT not in new and new.count("NEW REPLY") == 1


def test_utf16_offsets_for_accessibility():
    field = "👋 " + DRAFT
    plan = owndraft.plan(field, DRAFT)
    assert plan["start"] == 2 and plan["start16"] == 3
    assert plan["length16"] == len(DRAFT)


def test_strip_scene_removes_own_draft_so_it_is_not_answered():
    doc = {"app": "Outlook", "field_text": DRAFT + QUOTE,
           "window_ocr": "Priya Shah\n" + DRAFT[:60] + "\n" + DRAFT[60:120] + "\nIs the level 3 rule built in?"}
    out = owndraft.strip_scene(doc, DRAFT)
    assert DRAFT not in out["field_text"]
    assert out["field_text"].startswith("From: Priya Shah")
    assert DRAFT[:60] not in out["window_ocr"] and "Is the level 3 rule built in?" in out["window_ocr"]
    assert out["prior_draft"]["kind"] == owndraft.EXACT
    ctx = compose.gather(out, {}, code_roots=[])
    # The thread the model sees starts with Priya's message, not paisty's draft.
    assert DRAFT[:40] not in ctx.thread_block
    msgs = compose.messages(ctx, "rules")
    user = msgs[-1]["content"]
    assert "YOUR EARLIER DRAFT" in user and "replaces your earlier draft" in user


def test_prior_note_for_edited_draft_asks_to_keep_edits():
    edited = DRAFT.replace("16 dealers", "17 dealers")
    out = owndraft.strip_scene({"field_text": edited + QUOTE}, DRAFT)
    note = owndraft.prior_note(out["prior_draft"])
    assert "edited" in note and "Keep every change the user made" in note and "17 dealers" in note


def test_check_flags_dropped_user_edit_and_copies():
    edited = DRAFT.replace("16 dealers", "17 dealers")
    out = owndraft.strip_scene({"field_text": edited + QUOTE}, DRAFT)
    ctx = compose.gather(out, {}, code_roots=[])
    issues = compose.check(DRAFT, ctx)
    assert any("keep the user's edits" in i and "17" in i for i in issues)
    assert not any("17" in i for i in compose.check(edited.replace("$300k so", "$300k, so"), ctx)
                   if "keep the user's edits" in i)
    exact = owndraft.strip_scene({"field_text": DRAFT + QUOTE}, DRAFT)
    ctx2 = compose.gather(exact, {}, code_roots=[])
    assert any("copy of your earlier draft" in i for i in compose.check(DRAFT, ctx2))


def test_redraft_goes_through_the_full_path():
    assert compose.wants_full({"field_text": "hi", "prior_draft": {"original": "x"}}, {})


def test_locate_cli(tmp_path):
    f = tmp_path / "field.txt"
    p = tmp_path / "prior.txt"
    f.write_text(DRAFT + QUOTE, encoding="utf-8")
    p.write_text(DRAFT, encoding="utf-8")
    run = subprocess.run([sys.executable, str(ROOT / "contrib/clip/run"), "locate", "--field", str(f), "--prior", str(p)],
                         capture_output=True, text=True, env={"PYTHONPATH": str(ROOT), "HOME": str(tmp_path)})
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["mode"] == "replace"


def test_quote_header_in_same_paragraph_is_not_swallowed():
    edited = DRAFT.replace("16 dealers", "17 dealers")
    field = edited + "\nFrom: Priya Shah\nSent: Thursday\nTo: Jordan\n\nIs the level 3 rule built in?"
    spot = owndraft.locate(field, DRAFT)
    assert spot.found and "From:" not in spot.placed and spot.placed.endswith("$600k?")


def test_heavily_rewritten_text_is_left_alone():
    field = "Priya, I need to check with finance before we change any goals. Back to you Friday." + QUOTE
    assert not owndraft.locate(field, DRAFT).found


def test_chat_box_holding_only_the_edited_draft():
    edited = DRAFT.replace("want me to", "should I")
    plan = owndraft.plan(edited, DRAFT)
    assert plan["mode"] == "replace" and plan["start"] == 0 and plan["end"] == len(edited)
