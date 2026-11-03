"""Remember a correction and show it next time as a short system note."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """create table if not exists lessons (
            id integer primary key,
            prompt text not null,
            correction text not null,
            created real not null
        )"""
    )
    return con


def teach(con: sqlite3.Connection, prompt: str, correction: str) -> int:
    cur = con.execute(
        "insert into lessons (prompt, correction, created) values (?, ?, ?)",
        (prompt.strip(), correction.strip(), time.time()),
    )
    con.commit()
    return int(cur.lastrowid)


def recent(con: sqlite3.Connection, limit: int = 8) -> list[dict]:
    rows = con.execute(
        "select id, prompt, correction from lessons order by id desc limit ?",
        (limit,),
    ).fetchall()
    return [{"id": r[0], "prompt": r[1], "correction": r[2]} for r in rows]


def system_addon(con: sqlite3.Connection, limit: int = 8) -> str:
    rows = recent(con, limit)
    if not rows:
        return ""
    lines = ["Corrections the user already made:"]
    for row in reversed(rows):
        lines.append(f"- When the topic is '{row['prompt']}', prefer: {row['correction']}")
    return "\n".join(lines)


def write_runbook(con: sqlite3.Connection, root: Path) -> Path:
    """Write the corrections into a runbook the ask/do search can find."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "corrections.md"
    rows = recent(con, limit=50)
    lines = ["# Corrections", ""]
    if not rows:
        lines.append("No corrections yet.")
    else:
        for row in reversed(rows):
            lines.append(f"- When the topic is {row['prompt']}, prefer: {row['correction']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def prompt_with_lessons(con: sqlite3.Connection, runbook_root: Path | None = None, limit: int = 8) -> str:
    parts = [system_addon(con, limit)]
    if runbook_root:
        path = Path(runbook_root) / "corrections.md"
        if path.exists():
            parts.append(path.read_text(encoding="utf-8")[:2000])
    return "\n".join(part for part in parts if part)
