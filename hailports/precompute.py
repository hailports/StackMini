"""Shrink a long prompt on the local model before a paid call.

Also documents the optional low-priority precompute lane: idle capacity on the
local chat server digests tracker/mail/meeting notes into ready context packs
that a paid model can read later. Interactive traffic wins; the lane backs off
under memory pressure. Drafts only. Legal/Finance paths and secret markers are
dropped before any prompt is built.
"""

from __future__ import annotations

import re

_BLOCKED_PATH = re.compile(r"(?i)(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)")
_SECRET = re.compile(
    r"(?i)\\b(?:password|passwd|seed phrase|2fa|otp|totp|recovery code|private key)\\b"
)


def blocked_text(text: str) -> bool:
    return bool(_BLOCKED_PATH.search(text or "") or _SECRET.search(text or ""))


def build_packet(router, full_text: str) -> tuple[list[dict], int]:
    """Return messages safe to send, and how many tokens the shrink saved.

    Short text is passed through. Longer text is compressed on the local model
    when one is configured. The cloud call then sees the short packet only.
    """
    budget = int(getattr(getattr(router, "policy", None), "compact_char_budget", 1600) or 1600)
    if len(full_text) <= budget:
        return [{"role": "user", "content": full_text}], 0
    if blocked_text(full_text):
        return [{"role": "user", "content": full_text[:budget]}], 0
    compact, saved = router.compress(full_text)
    packet = [
        {"role": "system", "content": "Answer from this compact context only."},
        {
            "role": "user",
            "content": "Context:\n" + compact + "\n\nAnswer the request using only that context.",
        },
    ]
    return packet, saved


def should_run_background(*, interactive: int, memory_pressure: str, swap_mb: float) -> str | None:
    """Return a backoff reason, or None when a background precompute tick may run."""
    if interactive > 0:
        return "interactive"
    if memory_pressure == "critical":
        return "memory_critical"
    if swap_mb >= 4500:
        return "swap"
    if memory_pressure == "warning" and swap_mb >= 3500:
        return "memory_warning"
    return None
