"""Feature calls that must go through the router."""

from __future__ import annotations

import re

from hailports.drafter import draft
from hailports.providers.base import Reply
from hailports.webtools import http_get, needs_web, public_http_url


def answer(router, prompt: str, system: str = "") -> Reply:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    extra = _web_notes(prompt)
    task = "summarize" if extra else None
    messages.append({"role": "user", "content": prompt + extra})
    return router.complete(messages, task=task)


def _web_notes(prompt: str, fetch=http_get) -> str:
    if not needs_web(prompt):
        return ""
    chunks: list[str] = []
    for url in re.findall(r"https?://[^\s]+", prompt)[:2]:
        if not public_http_url(url):
            continue
        try:
            chunks.append(fetch(url)[:4000])
        except Exception as exc:
            chunks.append("Page could not be read: " + str(exc)[:200])
    if not chunks:
        return ""
    return "\n\nPage text:\n" + "\n".join(chunks)


def draft_reply(router, ask: str, sources: list[str]) -> Reply:
    """The model writes the draft. A line with no citation is held back."""
    from hailports.drafter import NoSource, citations_ok

    clean = [s.strip() for s in sources if s and s.strip()]
    if not clean:
        raise NoSource("no source notes")
    numbered = "\n".join(f"[{i}] {src}" for i, src in enumerate(clean, 1))
    grounded = draft(ask, clean)
    messages = [
        {"role": "system", "content": "Rewrite using only the numbered notes. End every line with a citation like [1]. Do not add facts."},
        {"role": "user", "content": "Notes:\n" + numbered + "\n\nGrounding:\n" + grounded + "\n\nAsk: " + ask},
    ]
    reply = router.complete(messages, task="draft")
    if citations_ok(reply.text, len(clean)):
        reply.raw["held"] = False
        return reply
    repair = router.complete(
        [
            {"role": "system", "content": "Add a citation [n] to every line. Use only the notes. Do not add facts."},
            {"role": "user", "content": "Notes:\n" + numbered + "\n\nDraft:\n" + (reply.text or "")},
        ],
        task="draft",
    )
    if citations_ok(repair.text, len(clean)):
        repair.raw["held"] = False
        return repair
    repair.raw["held"] = True
    repair.raw["reason"] = "citation"
    repair.text = ""
    return repair


def see(router, prompt: str, png: bytes) -> Reply:
    return router.vision(prompt, png)
