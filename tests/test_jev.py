"""Fail-open tests for the optional Jev classifier. No live API calls."""
from __future__ import annotations

import json
from pathlib import Path

from hailports import jev


def test_disable_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_DISABLE", "1")
    monkeypatch.setattr(jev, "LOG", tmp_path / "jev.jsonl")
    out = jev.classify("hello", {"q": {"type": "noul", "instructions": "yes"}}, caller="t")
    assert out is None
    assert not (tmp_path / "jev.jsonl").exists()


def test_empty_questions_returns_none(monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_DISABLE", raising=False)
    monkeypatch.setattr(jev, "LOG", tmp_path / "jev.jsonl")
    assert jev.classify("hello", {}, caller="t") is None


def test_missing_key_returns_none_and_logs_no_state(monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_DISABLE", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.setattr(jev, "_key_files", lambda: [tmp_path / "missing-key"])
    monkeypatch.setattr(jev, "LOG", tmp_path / "jev.jsonl")
    secret_state = "password=demo-not-real"
    out = jev.classify(secret_state, {"q": {"type": "noul", "instructions": "yes"}}, caller="t")
    assert out is None
    rec = json.loads((tmp_path / "jev.jsonl").read_text().strip().splitlines()[-1])
    blob = json.dumps(rec)
    assert rec["ok"] is False
    assert rec["err"] == "no_key"
    assert "state" not in rec
    assert "key" not in rec
    assert "password" not in blob
    assert secret_state not in blob


def test_noul_and_choice_helpers():
    answers = {
        "n": {"type": "noul", "noul": 0.8},
        "c": {"type": "choice", "choice": "needs_reply", "confidence": 0.9},
        "s": {"type": "score", "score": 2, "confidence": 0.5},
    }
    assert jev.noul(answers, "n") == 0.8
    assert jev.noul(answers, "missing") is None
    pick, conf = jev.choice(answers, "c")
    assert pick == "needs_reply" and conf == 0.9
    sc, sc_conf = jev.score(answers, "s")
    assert sc == 2 and sc_conf == 0.5


def test_mask_state_redacts_secrets():
    masked = jev.mask_state("password=demo-not-real and token=abc")
    assert "demo-not-real" not in masked
    assert "[redacted]" in masked


def test_gates_fail_open_and_confident(monkeypatch):
    monkeypatch.setenv("JEV_DISABLE", "1")
    from hailports import jev_gates as gates
    assert gates.score_local("Summarize this log", "a finished line") is None
    monkeypatch.delenv("JEV_DISABLE", raising=False)

    def fake_low(_state, _questions, caller, timeout=4.0):
        return {"quality": {"noul": 0.05}, "complete": {"noul": 0.95}}

    def fake_commit(_state, _questions, caller, timeout=4.0):
        return {"commits": {"noul": 0.95}}

    monkeypatch.setattr(gates, "_ask", fake_low)
    assert gates.score_local("Summarize this log", "unrelated paragraph") == "escalate"
    monkeypatch.setattr(gates, "_ask", fake_commit)
    ok, reason, _scores = gates.draft_gate("Please send the notes.", "I will deliver the report by Friday.")
    assert ok is False and reason == "jev_commits"
    monkeypatch.setattr(gates, "_ask", lambda *_a, **_k: None)
    assert gates.risk_gate("delete the table") is None


def test_cli_fail_open_on_garbage(capsys):
    rc = jev.main(["--state", "x"])  # no questions
    assert rc == 0
    assert capsys.readouterr().out.strip() == "null"
