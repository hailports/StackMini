#!/usr/bin/env python3
"""Coop pattern tests. No network."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["LOCAL_FIRST_RECORD"] = "0"
os.environ["JEV_DISABLE"] = "1"
os.environ["LOCAL_FIRST_DRY"] = "1"
os.environ.pop("COOP_MODE", None)

from hailports import local_first as r  # noqa: E402
from hailports import coop_route as c  # noqa: E402


def _no_fetch(_prompt):
    raise AssertionError("route fetch should stay idle")


def _no_paid(_prompt):
    raise AssertionError("paid launched")


def test_short_unsure_and_high_stay_exact():
    cfg = {"transport": "inject", "extra_high_substrings": [], "coop_fetch": _no_fetch}
    short = r.route_task("help me with this", caller="bot", fetch=_no_fetch, config=cfg, execute_paid=_no_paid)
    assert short["decision"] == "need_premium"
    assert short["backend"] == "skipped_stakes"
    assert short["answer"] == ""
    assert "Local model skipped; stakes are not low." in short["brief"]
    assert "COMPRESSED CONTEXT" not in short["brief"]
    high = r.route_task(
        "Draft an email to the customer about the outage.",
        caller="bot",
        fetch=_no_fetch,
        config=cfg,
        execute_paid=_no_paid,
    )
    assert high["decision"] == "need_premium" and high["answer"] == "" and high["stakes"] == "high"


def _pin_unsure(monkeypatch):
    # 9a18b8f made the classifier send simple count digs straight to local (stakes low); pin
    # stakes to "unsure" so these tests keep exercising the coop jev_gate path they are about.
    import re as _re
    monkeypatch.setattr(r, "classify_stakes", lambda *_a, **_k: ("unsure", "stakes_unsure"))
    monkeypatch.setattr(r, "task_type", lambda *_a, **_k: "other")
    monkeypatch.setattr(r, "_SOFT_DIG", _re.compile(r"(?!x)x"))


def test_number_heavy_dig_can_finish_locally(monkeypatch):
    _pin_unsure(monkeypatch)
    calls = {"n": 0}

    def hook(_prompt):
        calls["n"] += 1
        return {
            "ok": True,
            "text": "Skipped 295, completed 22, unsure 235, high 60.",
            "backend": "fake",
            "usage": {"completion_tokens": 9},
        }

    task = "What is the current state of these counts? skipped 295, complete 22, unsure 235, high 60."
    result = r.route_task(
        task,
        caller="bot",
        fetch=_no_fetch,
        config={"transport": "inject", "extra_high_substrings": [], "coop_fetch": hook},
        execute_paid=_no_paid,
    )
    assert calls["n"] == 1
    assert result["decision"] == "complete_local"
    assert result["coop_pattern"] == "jev_gate"
    assert result["premium_in_saved"] > 0
    assert "295" in result["answer"]


def test_bad_score_falls_back(monkeypatch):
    _pin_unsure(monkeypatch)
    os.environ.pop("JEV_DISABLE", None)
    gate_dir = str(Path(__file__).resolve().parents[1] / "hailports")
    if gate_dir not in sys.path:
        sys.path.insert(0, gate_dir)
    import jev_gates

    old = jev_gates.score_local
    jev_gates.score_local = lambda *_a, **_k: "escalate"
    try:
        result = r.route_task(
            "What is the current state of these counts? skipped 295, complete 22, unsure 235, high 60.",
            caller="bot",
            fetch=_no_fetch,
            config={
                "transport": "inject",
                "extra_high_substrings": [],
                "coop_fetch": lambda _p: {
                    "ok": True,
                    "text": "Skipped 295, completed 22, unsure 235, high 60.",
                    "backend": "fake",
                },
            },
            execute_paid=_no_paid,
        )
    finally:
        jev_gates.score_local = old
        os.environ["JEV_DISABLE"] = "1"
    assert result["decision"] == "need_premium"
    assert result["answer"] == ""
    assert result["backend"] == "skipped_stakes"
    assert "Local model skipped; stakes are not low." in result["brief"]
    assert "COMPRESSED CONTEXT" not in result["brief"]


def test_precompress_only_when_shorter_and_numbers_survive():
    calls = {"n": 0}
    body = "log rows 11 22 33 44. " + ("note " * 500)

    def hook(prompt):
        calls["n"] += 1
        assert "Compress" in prompt
        return {"ok": True, "text": "Rows: 11, 22, 33, 44.", "backend": "fake"}

    cfg = {"transport": "inject", "extra_high_substrings": [], "coop_fetch": hook, "coop_long_chars": 200}
    result = r.route_task(
        "implement the function and deploy it to production\n" + body,
        caller="bot",
        fetch=_no_fetch,
        config=cfg,
        execute_paid=_no_paid,
    )
    assert calls["n"] == 1
    assert result["decision"] == "need_premium" and result["answer"] == ""
    assert result["backend"] == "skipped_stakes"
    assert result["coop_pattern"] == "precompress"
    assert "11, 22, 33, 44" in result["brief"]
    assert result["premium_in_saved"] > 0

    os.environ["COOP_MODE"] = "0"
    try:
        calls["n"] = 0
        off = r.route_task(
            "implement the function and deploy it to production\n" + body,
            caller="bot",
            fetch=_no_fetch,
            config=cfg,
            execute_paid=_no_paid,
        )
    finally:
        os.environ.pop("COOP_MODE", None)
    assert calls["n"] == 0
    assert off["decision"] == "need_premium"
    assert "COMPRESSED CONTEXT" not in off["brief"]


def test_cache_hit_skips_the_model():
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="coop-cache-"))
    path = folder / "answer_cache.json"
    cfg = {
        "transport": "inject",
        "extra_high_substrings": [],
        "answer_cache_path": str(path),
        "coop_fetch": _no_fetch,
    }
    task = "Summarize this log: the router started and stopped cleanly today."
    first = r.route_task(
        task,
        caller="bot",
        fetch=lambda _p: {
            "ok": True,
            "text": '{"decision":"complete_local","confidence":0.9,"why":"log","answer":"The router started and stopped cleanly today."}',
            "backend": "fake",
            "usage": {"completion_tokens": 8},
        },
        config=cfg,
        execute_paid=_no_paid,
    )
    assert first["decision"] == "complete_local"
    assert path.is_file()
    second = r.route_task(task, caller="bot", fetch=_no_fetch, config=cfg, execute_paid=_no_paid)
    assert second["decision"] == "complete_local"
    assert second["backend"] == "answer_cache"
    assert second["coop_pattern"] == "cache"
    assert "stopped cleanly" in second["answer"]


def test_self_check_rejects_action_claims():
    assert c.answer_ok("status 12 20 30", "Dear customer, status 12 20 30") is False
    assert c.allow_local_finish("status 12 20 30", "counts 12, 20, and 30", "escalate") is False
    assert c.allow_local_finish("status 12 20 30", "counts 12, 20, and 30", None) is True
    assert c.compression_ok("x", "y") is False
