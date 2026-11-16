#!/usr/bin/env python3
"""Classifier + fail-open tests for the local-first offload door."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["LOCAL_FIRST_RECORD"] = "0"
os.environ["JEV_DISABLE"] = "1"
os.environ.pop("LOCAL_FIRST_ROUTER", None)
os.environ.pop("LOCAL_FIRST_DONE", None)

from hailports import local_first as r  # noqa: E402


def test_simple_classes():
    cases = [
        "Summarize this log in one sentence",
        "classify these inbox items",
        "triage the open loops",
        "extract the error codes",
        "short rewrite of this note",
        "first draft of a checklist",
        "write a commit message for this diff",
        "grep-explain why this regex matched",
    ]
    for task in cases:
        assert r.classify_stakes(task)[0] == "low", task
        assert r.route_class(task) == "simple", task


def test_build_never_local():
    task = "implement the multi-file feature and scaffold the module"
    assert r.route_class(task) == "build"
    assert r.classify_stakes(task)[0] == "high"
    result = r.route_task(
        task,
        caller="test",
        fetch=lambda _p: {
            "ok": True,
            "text": '{"decision":"complete_local","confidence":0.99,"why":"done","answer":"I built the whole feature already."}',
            "backend": "fake",
        },
        config={"transport": "inject", "extra_high_substrings": []},
    )
    assert result["decision"] == "need_premium"
    assert result.get("class") == "build"


def test_social_off_local():
    stakes, why = r.classify_stakes("draft a tweet for the social bot content calendar")
    assert stakes == "high"
    assert why == "social_off_local"


def test_fail_open_when_local_down(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_FIRST_RECORD", "1")
    decisions = tmp_path / "router-decisions.jsonl"
    rollup = tmp_path / "offload-today.md"
    cfg = {
        "transport": "inject",
        "extra_high_substrings": [],
        "decisions_path": str(decisions),
        "rollup_path": str(rollup),
        "receipt_path": str(tmp_path / "receipts.jsonl"),
    }
    result = r.route_task(
        "Summarize this log in one sentence please",
        caller="claude",
        fetch=lambda _p: {"ok": False, "error": "down", "backend": "unavailable"},
        config=cfg,
    )
    assert result["decision"] == "need_premium"
    assert result["why"] == "local_unavailable"
    assert decisions.is_file()
    row = json.loads(decisions.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert row["route"] == "paid"
    assert row["tool"] == "claude"
    assert rollup.is_file()
    assert "estimated premium tokens saved" in rollup.read_text(encoding="utf-8")


def test_local_route_logs_simple(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_FIRST_RECORD", "1")
    decisions = tmp_path / "router-decisions.jsonl"
    cfg = {
        "transport": "inject",
        "extra_high_substrings": [],
        "decisions_path": str(decisions),
        "rollup_path": str(tmp_path / "offload-today.md"),
        "receipt_path": str(tmp_path / "receipts.jsonl"),
    }
    result = r.route_task(
        "Summarize this log in one sentence please",
        caller="codex",
        fetch=lambda _p: {
            "ok": True,
            "text": '{"decision":"complete_local","confidence":0.9,"why":"short","answer":"The router started and wrote a one-line summary."}',
            "backend": "fake",
            "elapsed_s": 0.05,
        },
        config=cfg,
    )
    assert result["decision"] == "complete_local"
    row = json.loads(decisions.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert row["route"] == "local"
    assert row["class"] == "simple"
    assert row["tool"] == "codex"
    assert isinstance(row.get("tokens"), int)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
def test_watch_runner_dedupe(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_ALERT_DIR", str(tmp_path / "alerts"))
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WATCH_DEDUPE_TTL", "3600")
    monkeypatch.setenv("WATCH_MODEL_URL", "http://127.0.0.1:9/nope")
    check = tmp_path / "check.sh"
    check.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"need\":true,\"summary\":\"probe-down\",\"bot\":\"stack\",\"force\":true,\"dedupe\":\"k1\"}'\n"
    )
    check.chmod(0o755)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contrib" / "watch"))
    import runner as watch_runner  # noqa: E402

    assert watch_runner.main(["demo", str(check)]) == 0
    alerts = list((tmp_path / "alerts").glob("demo.jsonl"))
    assert alerts
    # second tick dedupes
    assert watch_runner.main(["demo", str(check)]) == 0
    lines = alerts[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
