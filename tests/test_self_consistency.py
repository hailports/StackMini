"""Self-consistency: agreed facts stay, disagreed facts are flagged, never guessed."""

from __future__ import annotations

import pytest

from hailports.self_consistency import (
    ENV_VAR,
    compare_facts,
    extract_facts,
    gate,
    is_casual,
    is_high_stakes,
    merge_answer,
)

# made-up people and places only
A = "you met Dana Reyes in Lakeport on tue nov 17 at 9:30am. you have 3 pull requests open."


def _kinds(text):
    return {(f.kind, f.key, f.value) for f in extract_facts(text)}


def test_extract_dates_counts_places_names():
    k = _kinds(A)
    assert ("date", "11-17", "11-17") in k
    assert ("weekday", "tue", "tue") in k
    assert ("time", "09:30", "09:30") in k
    assert ("count", "pull request", "3") in k
    assert ("place", "lakeport", "lakeport") in k
    assert ("name", "dana reyes", "dana reyes") in k


def test_extract_normalizes_date_forms_and_lowercase_places():
    k = _kinds("flew out of brightwater on 2026-11-15, back 11/17. you were in the brightwater area.")
    assert ("date", "11-15", "11-15") in k and ("date", "11-17", "11-17") in k
    assert ("place", "brightwater", "brightwater") in k


def test_extract_skips_non_places_and_plain_sat():
    k = _kinds("i sat at least once in the calendar at 3pm with the team.")
    assert not any(kind == "place" for kind, _, _ in k)
    assert not any(kind == "weekday" for kind, _, _ in k)
    assert ("time", "15:00", "15:00") in k


def test_lowercase_with_phrase_is_not_a_name():
    k = _kinds("on-site with on-site events mon nov 16, with ticket updates, with rich vance.")
    names = {key for kind, key, _ in k if kind == "name"}
    assert names == {"rich vance"}


def test_ids_are_not_counts():
    k = _kinds("pr #1422 for ado 382662 mesac items and 1,200 widgets; 3 tickets.")
    counts = {(key, v) for kind, key, v in k if kind == "count"}
    assert counts == {("widget", "1200"), ("ticket", "3")}


def test_agreement_kept_no_flags():
    b = "on tue nov 17 at 9:30am you met Dana Reyes in Lakeport; 3 pull requests are open."
    c = "You had 3 pull requests open and met Dana Reyes in Lakeport, tue nov 17, 9:30am."
    r = merge_answer(A, [A, b, c])
    assert r["flags"] == []
    assert r["answer"] == A
    assert r["n_samples"] == 3


def test_place_vs_no_location_flagged_not_guessed():
    p = "the planning sync was on wed nov 18 in Lakeport."
    o1 = "the planning sync was on wed nov 18. no location was listed."
    o2 = "planning sync: wed nov 18, location not given."
    r = merge_answer(p, [p, o1, o2])
    assert "Lakeport vs (no location)" in r["flags"]
    assert "Lakeport (unsure)" in r["answer"]
    assert r["answer"].rstrip().endswith("unsure: Lakeport vs (no location)")
    # the agreed date is not flagged
    assert not any("nov 18" in f for f in r["flags"])


def test_count_mismatch_flagged_with_both_values():
    p = "this week you opened 12 pull requests and closed 4 tickets."
    o1 = "you opened 3 pull requests and closed 4 tickets this week."
    o2 = "3 pull requests opened, 4 tickets closed."
    r = merge_answer(p, [p, o1, o2])
    assert "12 pull requests vs 3 pull requests" in r["flags"]
    assert "12 pull requests (unsure)" in r["answer"]
    assert not any("tickets" in f for f in r["flags"])


def test_no_majority_vote_primary_text_kept():
    # two samples say 3, primary says 12: still the primary's wording, only qualified
    p = "you opened 12 pull requests."
    r = merge_answer(p, [p, "you opened 3 pull requests.", "you opened 3 pull requests."])
    assert r["answer"].startswith("you opened 12 pull requests (unsure).")
    assert "you opened 3 pull requests." not in r["answer"]


def test_supported_by_one_other_sample_is_agreed():
    p = "you were in Lakeport on mon nov 16."
    res = compare_facts([p, "mon nov 16 you were in Lakeport.", "you traveled on mon nov 16."])
    assert ("place", "lakeport") in {(f.kind, f.key) for f in res["agreed"]}
    assert res["disagreed"] == []


def test_failed_samples_degrade_gracefully():
    r = merge_answer("you met Dana Reyes.", ["you met Dana Reyes.", "", None])
    assert r["answer"] == "you met Dana Reyes."
    assert r["flags"] == []
    assert "skipped" in r["note"]


def test_primary_added_when_missing_from_samples():
    r = merge_answer("met in Lakeport.", ["met somewhere.", "met, place unknown."])
    assert r["n_samples"] == 3
    assert "Lakeport vs (no location)" in r["flags"]


@pytest.mark.parametrize(
    "q",
    [
        "what happened at work last week",
        "give me a recap of the portal work",
        "when is the review meeting",
        "where was the offsite",
        "how many pull requests did i open",
        "draft a reply to Dana about the schedule",
        "what did i do on tuesday",
        "anything due nov 30?",
    ],
)
def test_high_stakes(q):
    assert is_high_stakes(q)


@pytest.mark.parametrize(
    "q",
    ["hello", "hey there", "what's your name", "thanks!", "how are you", "tell me a joke"],
)
def test_casual_not_high_stakes(q):
    assert not is_high_stakes(q)


def test_casual_stays_casual_over_dated_context():
    ctx = "nov 15 trip; 12 pull requests; $300 hotel; 2026-11-17 flight"
    assert is_casual("hello")
    assert not is_high_stakes("hello", ctx)
    assert is_high_stakes("anything new on the portal", ctx)
    assert not is_high_stakes("anything new on the portal", "no dates here")


def test_gate_default_off(tmp_path):
    flag = tmp_path / "flag"
    assert gate("what happened last week", env={}, flag_path=flag) == (False, "off")


def test_gate_flag_file_high_stakes_only(tmp_path):
    flag = tmp_path / "flag"
    flag.write_text("")
    assert gate("what happened last week", env={}, flag_path=flag) == (True, "flag-on")
    assert gate("hello", env={}, flag_path=flag) == (False, "casual")


def test_gate_env_on_and_off(tmp_path):
    flag = tmp_path / "flag"
    assert gate("how many tickets", env={ENV_VAR: "1"}, flag_path=flag) == (True, "env-on")
    assert gate("hello", env={ENV_VAR: "1"}, flag_path=flag) == (False, "casual")
    flag.write_text("")
    assert gate("how many tickets", env={ENV_VAR: "0"}, flag_path=flag) == (False, "env-off")
    assert gate("how many tickets", env={ENV_VAR: "0"}, flag_path=flag, trigger=True) == (False, "env-off")


def test_gate_trigger_runs_even_when_unsure(tmp_path):
    flag = tmp_path / "flag"
    assert gate("tell me about the portal", env={}, flag_path=flag, trigger=True) == (True, "trigger")
    assert gate("hello", env={}, flag_path=flag, trigger=True) == (True, "trigger")
