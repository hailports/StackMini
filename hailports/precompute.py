"""Shrink a long prompt on the local model before a paid call."""

from __future__ import annotations


def build_packet(router, full_text: str) -> tuple[list[dict], int]:
    """Return messages safe to send, and how many tokens the shrink saved.

    Short text is passed through. Longer text is compressed on the local model
    when one is configured. The cloud call then sees the short packet only.
    """
    budget = int(getattr(getattr(router, "policy", None), "compact_char_budget", 1600) or 1600)
    if len(full_text) <= budget:
        return [{"role": "user", "content": full_text}], 0
    compact, saved = router.compress(full_text)
    packet = [
        {"role": "system", "content": "Answer from this compact context only."},
        {"role": "user", "content": "Context:\n" + compact + "\n\nAnswer the request using only that context."},
    ]
    return packet, saved
