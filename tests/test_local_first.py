#!/usr/bin/env python3
"""Local-first route: short work stays local, hard work escalates with a brief."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["LOCAL_FIRST_RECORD"] = "0"

from hailports import local_first as r


def test_policy_matrix():
    assert r.classify_stakes("Summarize this log in one sentence: started then stopped")[0] == "low"
    assert r.classify_stakes("deploy this change to production")[0] == "high"
    assert r.classify_stakes("email the customer a status update")[0] == "high"
    assert r.classify_stakes("draft a reply comment for the thread")[0] == "high"
    assert r.classify_stakes("help me with this")[0] == "unsure"
    foot = "Summarize this log: the router started\n\n## STANDING RULE\ncommit and push"
    assert r.classify_stakes(foot)[0] == "low"
    assert r.classify_stakes("first draft of a checklist for me")[0] == "low"
    assert r.classify_stakes("note this ledger-mutate please", ["ledger-mutate"])[0] == "high"


def test_low_completes_and_high_does_not():
    low = r.policy_decision(
        "Summarize this log in one sentence",
        '{"decision":"complete_local","confidence":0.91,"why":"short log","answer":"The router started and did not open a paid session."}',
    )
    assert low["decision"] == "complete_local"
    assert low["paid_invoked"] is False
    assert low["brief"].startswith(r.BRIEF_HEADER)
    high = r.policy_decision(
        "implement the function and deploy it to production",
        '{"decision":"complete_local","confidence":0.99,"why":"done","answer":"I already changed production and emailed the team."}',
    )
    assert high["decision"] == "need_premium"
    assert high["answer"] == ""


def test_grok_plan_does_not_exec_on_local():
    low = r.policy_decision(
        "Summarize this log in one sentence please",
        '{"decision":"complete_local","confidence":0.8,"why":"ok","answer":"One sentence is enough for this short log line."}',
    )
    action, args = r.plan_grok(
        ["--background-wait-timeout", "31536000", "-p", "Summarize this log in one sentence please"],
        low,
    )
    assert action == "local" and args == []
    high = r.policy_decision("refactor the parser", "A short brief of the parser edges.")
    action, args = r.plan_grok(["--background-wait-timeout", "31536000", "-p", "refactor the parser"], high)
    assert action == "escalate"
    assert args[0] == "--background-wait-timeout"
    assert "[LOCAL-FIRST BRIEF v1]" in " ".join(args)


def test_route_with_fake_fetch_never_marks_paid():
    def fetch(_prompt):
        return {
            "ok": True,
            "text": '{"decision":"complete_local","confidence":0.88,"why":"log","answer":"The three lines say the local model finished alone."}',
            "backend": "fake",
            "usage": {"prompt_tokens": 10},
        }

    os.environ["LOCAL_FIRST_DRY"] = "1"
    result = r.route_task(
        "Summarize this log: a b c d e f g h",
        caller="bot",
        fetch=fetch,
        config={"transport": "inject", "extra_high_substrings": []},
        execute_paid=lambda _p: (_ for _ in ()).throw(AssertionError("paid launched")),
    )
    assert result["decision"] == "complete_local"
    assert result["paid_invoked"] is False
    assert result["backend"] == "fake"


def test_before_paid_stops_when_local_finishes():
    original = r.gate_for_paid

    def fake(_task, caller="paid"):
        assert caller == "paid"
        return {"decision": "complete_local", "answer": "local finished the log summary", "brief": r.BRIEF_HEADER}

    r.gate_for_paid = fake
    try:
        action, text = r.before_paid("Summarize this log in one sentence")
    finally:
        r.gate_for_paid = original
    assert action == "done"
    assert text == "local finished the log summary"


if __name__ == "__main__":
    test_policy_matrix()
    test_low_completes_and_high_does_not()
    test_grok_plan_does_not_exec_on_local()
    test_route_with_fake_fetch_never_marks_paid()
    test_before_paid_stops_when_local_finishes()
    print("ok")
