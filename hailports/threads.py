"""Whole-thread chat memory. Old turns are summarized. reset drops the thread."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """create table if not exists turns (
            id integer primary key,
            role text not null,
            content text not null,
            created real not null
        )"""
    )
    con.execute(
        """create table if not exists meta (
            k text primary key,
            v text not null
        )"""
    )
    return con


def append(con: sqlite3.Connection, role: str, content: str) -> None:
    con.execute(
        "insert into turns (role, content, created) values (?, ?, ?)",
        (role, content, time.time()),
    )
    con.commit()


def reset(con: sqlite3.Connection) -> None:
    con.execute("delete from turns")
    con.execute("delete from meta")
    con.commit()


def _summary(con: sqlite3.Connection) -> str:
    row = con.execute("select v from meta where k = 'summary'").fetchone()
    return row[0] if row else ""


def history(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = con.execute(
        "select role, content from turns order by id desc limit ?",
        (limit,),
    ).fetchall()
    rows.reverse()
    return [{"role": role, "content": content} for role, content in rows]


def context_messages(con: sqlite3.Connection, limit: int = 20) -> tuple[str, list[dict]]:
    return _summary(con), history(con, limit)


def maybe_summarize(con: sqlite3.Connection, router, budget_chars: int = 2400) -> str:
    """Fold old turns into a summary once the thread is past the budget. Recent turns stay."""
    rows = con.execute("select id, role, content from turns order by id").fetchall()
    blob = "\n".join(f"{role}: {content}" for _, role, content in rows)
    if len(blob) <= budget_chars or len(rows) < 3:
        return ""
    old = rows[:-2]
    old_text = "\n".join(f"{role}: {content}" for _, role, content in old)
    prior = _summary(con)
    prompt = old_text if not prior else "Earlier summary:\n" + prior + "\n\nMore turns:\n" + old_text
    reply = router.complete(
        [
            {"role": "system", "content": "Summarize the chat. Keep facts and decisions. Be short."},
            {"role": "user", "content": prompt[:12000]},
        ],
        task="summarize",
    )
    text = (reply.text or "").strip()
    con.execute("insert or replace into meta (k, v) values ('summary', ?)", (text,))
    ids = [row[0] for row in old]
    con.execute("delete from turns where id in (%s)" % ",".join("?" for _ in ids), ids)
    con.commit()
    return text
