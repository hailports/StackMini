"""Unit tests for hailports.never_idle (no model calls)."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from hailports.never_idle import (
    ENQUEUE_SKIP_DEPTH,
    LOW_WATER,
    blocked_text,
    claim_next,
    content_hash,
    deterministic_gate,
    dual_slot_allowed,
    effective_priority,
    enqueue,
    mark_done,
    needs_refill,
    open_queue,
    queue_depth,
    record_quality,
    should_run_background,
    should_scan_enqueue,
    soft_throttle_reason,
    throttle_sleep_s,
)


def test_blocked_text():
    assert blocked_text("normal ado card about portal") is False
    assert blocked_text("/Legal/contract.pdf") is True
    assert blocked_text("here is the password: hunter2") is True
    assert blocked_text("draft a linkedin caption") is True


def test_backoff():
    normal = {"level": "normal", "rate": 1.0}
    assert (
        should_run_background(
            interactive=False,
            memory_pressure="normal",
            swap_mb=1000,
            ttft_p95_ms=100,
            throttle=normal,
        )
        is None
    )
    assert (
        should_run_background(
            interactive=True,
            memory_pressure="normal",
            swap_mb=1000,
            ttft_p95_ms=100,
            throttle=normal,
        )
        == "interactive"
    )
    assert (
        should_run_background(
            interactive=False,
            memory_pressure="warning",
            swap_mb=4000,
            ttft_p95_ms=100,
            throttle=normal,
        )
        == "memory_warning"
    )
    # Contended TTFT must not starve a non-empty queue (preemption owns TTFT).
    assert (
        should_run_background(
            interactive=False,
            memory_pressure="normal",
            swap_mb=1000,
            ttft_p95_ms=2500,
            throttle=normal,
        )
        is None
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


def test_soft_throttle_keeps_producing():
    reason = should_run_background(
        interactive=False,
        memory_pressure="normal",
        swap_mb=1000,
        throttle={"level": "warning", "rate": 0.25},
    )
    assert reason == "throttle_warning_rate_0.25"
    assert soft_throttle_reason(reason) is True
    assert throttle_sleep_s(reason, depth=ENQUEUE_SKIP_DEPTH) <= 0.45
    assert should_scan_enqueue(ENQUEUE_SKIP_DEPTH) is False
    assert should_scan_enqueue(LOW_WATER - 1) is True


def test_dual_slot_gates():
    assert (
        dual_slot_allowed(
            throttle_rate=1.0,
            memory_pressure="normal",
            interactive=False,
            depth=100,
            ttft_p95_ms=200,
        )
        is True
    )
    assert (
        dual_slot_allowed(
            throttle_rate=0.25,
            memory_pressure="warning",
            interactive=False,
            depth=100,
            ttft_p95_ms=200,
        )
        is False
    )
    assert (
        dual_slot_allowed(
            throttle_rate=1.0,
            memory_pressure="normal",
            interactive=False,
            depth=100,
            ttft_p95_ms=5000,
        )
        is False
    )
