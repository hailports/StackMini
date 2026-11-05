"""Stop a second open card with the same requester and title."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def _tokens(title: str) -> set[str]:
    return {part for part in title.lower().split() if len(part) > 2}


def jaccard(left: str, right: str) -> float:
    a = _tokens(left)
    b = _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def key_for(requester: str, title: str) -> str:
    raw = (requester.strip().lower() + "\n" + " ".join(title.lower().split())).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """create table if not exists open_cards (
            k text primary key,
            requester text,
            title text,
            card_id text
        )"""
    )
    return con


def acquire(con: sqlite3.Connection, requester: str, title: str, threshold: float = 0.72) -> dict:
    k = key_for(requester, title)
    row = con.execute("select card_id from open_cards where k = ?", (k,)).fetchone()
    if row:
        return {"ok": False, "reason": "duplicate", "key": k, "card_id": row[0]}
    wanted = requester.strip().lower()
    for existing_title, card_id, who in con.execute("select title, card_id, requester from open_cards"):
        if (who or "").strip().lower() != wanted:
            continue
        if jaccard(existing_title or "", title) >= threshold:
            return {"ok": False, "reason": "duplicate", "key": k, "card_id": card_id}
    return {"ok": True, "reason": "new", "key": k, "card_id": None}


def remember(con: sqlite3.Connection, requester: str, title: str, card_id: str) -> None:
    k = key_for(requester, title)
    con.execute(
        "insert or replace into open_cards (k, requester, title, card_id) values (?, ?, ?, ?)",
        (k, requester, title, card_id),
    )
    con.commit()


def lookup_json(blob: str, requester: str, title: str) -> dict | None:
    """Helper for tests that keep the ledger as JSON text instead of sqlite."""
    data = json.loads(blob or "{}")
    return data.get(key_for(requester, title))
