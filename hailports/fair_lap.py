"""Queued-draft rounds before a live unread loop.

A fresh unread owns the lap. A stale badge, or a badge whose queue row is
already parked, gets one queue round instead of zero. No chat is opened.
"""

from __future__ import annotations


def fair_predrain_plan(
    max_rounds: int,
    unread_n: int,
    *,
    stale_untrusted: bool,
    sticky_only: bool,
) -> tuple[int, str]:
    """Queued-runner rounds before the live-unread loop.

    A fresh unread owns the lap. A stale sidebar badge, or a badge whose queue
    row is already parked, must not freeze other pending rows.
    """
    if unread_n <= 0:
        return max_rounds, "open"
    if stale_untrusted or sticky_only:
        return min(1, max_rounds), "unstick"
    return 0, "live_zoom"


def fair_run_queue_after_enq(*, enqueued: int, skip: str | None, reason: str | None) -> bool:
    """Whether this unread still owes the queue a runner pass.

    already_queued means the row is pending and must be drafted. A self-tip
    on a native unread dot is not a new open, but it must not skip the queue.
    """
    if enqueued > 0:
        return True
    if skip == "already_queued":
        return True
    return reason == "passive_stale_tip"
