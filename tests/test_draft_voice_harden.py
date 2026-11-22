"""Tests for hailports.draft_voice_harden failure-gated paid path."""
from __future__ import annotations

import json
from pathlib import Path

from hailports.draft_voice_harden import gate, post_filter, run_checks


def _fixer(draft, *, fails, inbound, lookups, channel):
    t = post_filter(draft or "")
    for phrase in ("certainly", "leverage", "robust", "i will", "i'll"):
        t = t.replace(phrase, "")
    t = post_filter(t)
    if len(t.split()) < 3:
        return "hey,\n\nneed to check."
    return t


def test_checks_pass_grounded():
    inbound = "From: Pat\nplease confirm noreply@hailports.com stays the login"
    draft = "hey pat,\n\nthe login stays noreply@hailports.com."
    assert run_checks(draft, inbound=inbound) == []


def test_checks_fail_ai_and_promise():
    inbound = "please send numbers today"
    draft = post_filter("Hey Pat,\n\nI will leverage our robust tooling. Certainly!")
    fails = run_checks(draft, inbound=inbound)
    assert fails


def test_gate_pass_local(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFT_SECOND_PASS_FLAG", str(tmp_path / "flag"))
    monkeypatch.setenv("DRAFT_SECOND_PASS_LOG", str(tmp_path / "second.jsonl"))
    monkeypatch.setenv("DRAFT_ROUTER_LOG", str(tmp_path / "router.jsonl"))
    (tmp_path / "flag").write_text("1\n")
    # reload paths
    import importlib
    import hailports.draft_voice_harden as m

    importlib.reload(m)
    inbound = "From: Pat\nplease confirm noreply@hailports.com stays the login"
    final, meta = m.gate(
        "hey pat,\n\nthe login stays noreply@hailports.com.",
        inbound=inbound,
        dry_run=True,
        rewrite_fn=_fixer,
        flag=tmp_path / "flag",
    )
    assert final.startswith("hey pat")
    assert meta["route"] == "local"
    assert meta["paid_would"] is False


def test_gate_paid_dry_after_failed_retry(tmp_path):
    flag = tmp_path / "flag"
    flag.write_text("1\n")

    def bad(draft, **kwargs):
        return "hey pat,\n\ni will certainly leverage https://invented.example/x"

    final, meta = gate(
        "hey pat,\n\ni will certainly leverage https://invented.example/x",
        inbound="please confirm the login",
        dry_run=True,
        rewrite_fn=bad,
        flag=flag,
    )
    assert meta["retry_used"] is True
    assert meta["paid_would"] is True
    assert meta["route"] == "paid_dry"
    assert meta["paid_used"] is False
