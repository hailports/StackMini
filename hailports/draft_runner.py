"""A small queue of draft jobs. The runner does not send anything by itself."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from hailports.drafter import NoSource, draft


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """create table if not exists jobs (
            id integer primary key,
            ask text not null,
            sources text not null,
            status text not null,
            result text,
            created real not null
        )"""
    )
    return con


def enqueue(con: sqlite3.Connection, ask: str, sources: list[str]) -> int:
    cur = con.execute(
        "insert into jobs (ask, sources, status, created) values (?, ?, 'queued', ?)",
        (ask, "\n---\n".join(sources), time.time()),
    )
    con.commit()
    return int(cur.lastrowid)


def run_next(con: sqlite3.Connection, router=None) -> dict | None:
    row = con.execute(
        "select id, ask, sources from jobs where status = 'queued' order by id limit 1"
    ).fetchone()
    if not row:
        return None
    job_id, ask, blob = row
    sources = [p for p in blob.split("\n---\n") if p.strip()]
    try:
        if router is None:
            text = draft(ask, sources)
            status = "done"
        else:
            from hailports.features import draft_reply
            reply = draft_reply(router, ask, sources)
            if reply.raw.get("held"):
                text = ""
                status = "held"
            else:
                text = reply.render()
                status = "done"
    except NoSource as exc:
        text = str(exc)
        status = "error"
    con.execute("update jobs set status = ?, result = ? where id = ?", (status, text, job_id))
    con.commit()
    return {"id": job_id, "status": status, "result": text}


def pending(con: sqlite3.Connection) -> int:
    return int(con.execute("select count(*) from jobs where status = 'queued'").fetchone()[0])
