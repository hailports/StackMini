"""Per-source freshness with an age limit for every source, and lanes that must exist.

A source with no explicit limit gets ``default_h``. A required lane with no rows at all is
RED, not "fresh": an index that never had Teams cannot report Teams as healthy.
"""

from __future__ import annotations

import time

DEFAULT_LIMIT_H = 168.0


def lane_status(
    sources: dict[str, tuple[int, float]],
    limits: dict[str, float | None] | None = None,
    *,
    required: dict[str, str] | None = None,
    default_h: float = DEFAULT_LIMIT_H,
    factor: float = 1.0,
    now: float | None = None,
) -> dict:
    """``sources`` maps name -> (row_count, newest_epoch). Returns stale, missing, and a verdict."""
    now = time.time() if now is None else now
    limits = limits or {}
    stale = {}
    for name, (_count, newest) in sorted(sources.items()):
        limit = limits.get(name, default_h)
        if limit is None:
            limit = default_h
        age_h = round((now - float(newest or 0)) / 3600, 1)
        if age_h > float(limit) * factor:
            stale[name] = {"age_h": age_h, "limit_h": float(limit) * factor}
    missing = {
        lane: desc
        for lane, desc in sorted((required or {}).items())
        if not any(lane in name and sources[name][0] > 0 for name in sources)
    }
    verdict = "RED" if missing else "YELLOW" if stale else "GREEN"
    return {"verdict": verdict, "stale": stale, "missing": missing}


def probe_hit(hits: list[dict], expect_any: list[str], top: int = 3) -> bool:
    """A probe passes only when an expected string is in one of the first ``top`` hits."""
    needles = [e.lower() for e in expect_any if e]
    for hit in hits[: max(1, top)]:
        blob = f"{hit.get('title') or ''} {hit.get('text') or hit.get('body') or ''}".lower()
        if any(n in blob for n in needles):
            return True
    return False
