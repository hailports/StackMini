"""Unit tests for hailports.never_idle (no model calls)."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from hailports.never_idle import (
    blocked_text,
    claim_next,
    content_hash,
    enqueue,
    mark_done,
    open_queue,
    record_quality,
    should_run_background,
)


def test_blocked_text():
    assert blocked_text("normal ado card about portal") is False
    assert blocked_text("/Legal/contract.pdf") is True
    assert blocked_text("here is the password: hunter2") is True
    assert blocked_text("draft a linkedin caption") is True


def test_backoff():
    assert should_run_background(
        interactive=False, memory_pressure="normal", swap_mb=1000, ttft_p95_ms=100
    ) is None
    assert (
        should_run_background(
            interactive=True, memory_pressure="normal", swap_mb=1000, ttft_p95_ms=100
        )
        == "interactive"
    )
    assert (
        should_run_background(
            interactive=False, memory_pressure="warning", swap_mb=4000, ttft_p95_ms=100
        )
        == "memory_warning"
    )
    assert (
        should_run_background(
            interactive=False, memory_pressure="normal", swap_mb=1000, ttft_p95_ms=2500
        )
        == "ttft"
    )


def test_queue_dedupe_and_priority():
    with tempfile.TemporaryDirectory() as tmp:
        con = open_queue(Path(tmp) / "q.db")
        assert enqueue(con, "backfill", {"id": "a", "text": "hello"})
        assert not enqueue(con, "backfill", {"id": "a", "text": "hello"})
        assert enqueue(con, "context_pack", {"id": "b", "text": "card"})
        job = claim_next(con)
        assert job is not None
        assert job[1] == "context_pack"  # higher priority first
        mark_done(con, job[0], job[1], job[2])
        assert content_hash('{"id": "a", "text": "hello"}')


def test_quality_pause():
    with tempfile.TemporaryDirectory() as tmp:
        con = open_queue(Path(tmp) / "q.db")
        for _ in range(9):
            record_quality(con, "pre_draft", False)
        record_quality(con, "pre_draft", False)  # 10th
        row = con.execute("SELECT paused_until FROM quality WHERE producer='pre_draft'").fetchone()
        assert float(row[0]) > time.time()
