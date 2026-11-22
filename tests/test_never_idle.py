"""Unit tests for hailports.never_idle (no model calls)."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from hailports.never_idle import (
    LOW_WATER,
    blocked_text,
    claim_next,
    content_hash,
    deterministic_gate,
    effective_priority,
    enqueue,
    mark_done,
    needs_refill,
    open_queue,
    queue_depth,
    record_quality,
    should_run_background,
)


def test_blocked_text():
    assert blocked_text("normal ado card about portal") is False
    assert blocked_text("/Legal/contract.pdf") is True
    assert blocked_text("here is the password: hunter2") is True
    assert blocked_text("draft a linkedin caption") is True


def test_backoff():
    assert (
        should_run_background(
            interactive=False,
            memory_pressure="normal",
            swap_mb=1000,
            ttft_p95_ms=100,
        )
        is None
    )
    assert (
        should_run_background(
            interactive=True,
            memory_pressure="normal",
            swap_mb=1000,
            ttft_p95_ms=100,
        )
        == "interactive"
    )
    assert (
        should_run_background(
            interactive=False,
            memory_pressure="warning",
            swap_mb=4000,
            ttft_p95_ms=100,
        )
        == "memory_warning"
    )
    assert (
        should_run_background(
            interactive=False,
            memory_pressure="normal",
            swap_mb=1000,
            ttft_p95_ms=2500,
        )
        == "ttft"
    )


def test_queue_dedupe_and_priority():
    with tempfile.TemporaryDirectory() as tmp:
        con = open_queue(Path(tmp) / "q.db")
        assert enqueue(con, "backfill", {"id": "a", "text": "hello"})
        assert not enqueue(con, "backfill", {"id": "a", "text": "hello"})
        assert enqueue(con, "pre_draft", {"id": "b", "text": "card", "fresh": True})
        job = claim_next(con)
        assert job is not None
        assert job[1] == "pre_draft"  # higher priority first
        mark_done(con, job[0], job[1], job[2])
        assert content_hash('{"fresh": true, "id": "b", "text": "card"}')


def test_low_watermark_refill():
    with tempfile.TemporaryDirectory() as tmp:
        con = open_queue(Path(tmp) / "q.db")
        assert needs_refill(con, low_water=LOW_WATER) is True
        for i in range(LOW_WATER):
            assert enqueue(con, "sf_atlas", {"id": f"c{i}", "text": f"comp-{i}"})
        assert queue_depth(con) == LOW_WATER
        assert needs_refill(con, low_water=LOW_WATER) is False


def test_fresh_outranks_backlog():
    assert effective_priority("refresh") > effective_priority("pre_draft", fresh=True)
    assert effective_priority("sf_atlas") > effective_priority("ado_prep")
    assert effective_priority("backfill", usefulness=0.1) > effective_priority(
        "backfill", usefulness=1.0
    )


def test_quality_pause():
    with tempfile.TemporaryDirectory() as tmp:
        con = open_queue(Path(tmp) / "q.db")
        for _ in range(9):
            record_quality(con, "pre_draft", False)
        record_quality(con, "pre_draft", False)  # 10th
        row = con.execute(
            "SELECT paused_until FROM quality WHERE producer='pre_draft'"
        ).fetchone()
        assert float(row[0]) > time.time()


def test_deterministic_gate_thin_draft():
    ok, reasons = deterministic_gate("pre_draft", "ok", "source thread about portal")
    assert ok is False
    assert "thin_draft" in reasons
