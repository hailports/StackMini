"""Hash a service file so a later edit is visible."""

from __future__ import annotations

import hashlib
from pathlib import Path


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot(path: Path) -> str:
    return fingerprint(path.read_text(encoding="utf-8"))


def drifted(path: Path, expected: str) -> bool:
    if not path.exists():
        return True
    return snapshot(path) != expected
