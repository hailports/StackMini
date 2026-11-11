"""Stale chat badges must not freeze already-queued drafts."""

from __future__ import annotations

from hailports.fair_lap import fair_predrain_plan, fair_run_queue_after_enq


def test_fresh_unread_owns_the_lap():
    rounds, why = fair_predrain_plan(10, 1, stale_untrusted=False, sticky_only=False)
    assert (rounds, why) == (0, "live_zoom")


def test_stale_badge_runs_one_queued_row():
    rounds, why = fair_predrain_plan(10, 1, stale_untrusted=True, sticky_only=False)
    assert (rounds, why) == (1, "unstick")


def test_parked_badge_runs_one_queued_row():
    rounds, why = fair_predrain_plan(10, 1, stale_untrusted=False, sticky_only=True)
    assert (rounds, why) == (1, "unstick")


def test_empty_sidebar_keeps_full_predrain():
    rounds, why = fair_predrain_plan(10, 0, stale_untrusted=False, sticky_only=False)
    assert (rounds, why) == (10, "open")


def test_passive_stale_tip_still_runs_the_queue():
    assert fair_run_queue_after_enq(enqueued=0, skip=None, reason="passive_stale_tip")
    assert fair_run_queue_after_enq(enqueued=0, skip="already_queued", reason=None)
    assert fair_run_queue_after_enq(enqueued=1, skip=None, reason=None)
    assert not fair_run_queue_after_enq(enqueued=0, skip="hold_cooldown", reason=None)
