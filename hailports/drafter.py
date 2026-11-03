"""Draft a reply using only the source notes you pass in."""

from __future__ import annotations

import re


class NoSource(ValueError):
    pass


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z0-9']+", text) if len(w) > 2}


_CITE = re.compile(r"\[(\d+)\]")


def citations_ok(text: str, source_count: int) -> bool:
    """Every non-empty line must cite a source number that exists."""
    if source_count < 1 or not (text or "").strip():
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()] or [text.strip()]
    marks: list[int] = []
    for line in lines:
        found = [int(n) for n in _CITE.findall(line)]
        if not found:
            return False
        marks.extend(found)
    return all(1 <= n <= source_count for n in marks)


def draft(ask: str, sources: list[str]) -> str:
    clean = [s.strip() for s in sources if s and s.strip()]
    if not clean:
        raise NoSource("no source notes")
    ask_words = _words(ask)
    picked: list[str] = []
    for src in clean:
        for sent in _sentences(src):
            if ask_words and _words(sent) & ask_words:
                picked.append(sent)
    if not picked:
        picked = _sentences(clean[0])[:2] or [clean[0]]
    # De-dupe, keep order.
    seen: set[str] = set()
    lines: list[str] = []
    for line in picked:
        if line not in seen:
            seen.add(line)
            lines.append(line)
    body = " ".join(lines[:6])
    return body
