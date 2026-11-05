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


def record_pair(folder: Path, surface: str, key: str, staged: str, sent: str) -> str:
    """Remember an edit. A matching send is a gold example. A changed send is a lesson."""
    from hailports.contextpack import record_pair as remember
    return remember(Path(folder), surface, key, staged, sent)


def similar(folder: Path, query: str, k: int = 3) -> str:
    """The two or three closest lessons and examples for this ask."""
    from hailports.contextpack import similar_text
    return similar_text(Path(folder), query, k=k)


def pref_split(key: str) -> str:
    """One fifth of keys are held out. The split is stable."""
    import hashlib
    digest = hashlib.sha1(b"pref|" + str(key).encode("utf-8", "replace")).hexdigest()
    if int(digest[:8], 16) % 5 == 0:
        return "heldout"
    return "train"


def refuse_train_path(path) -> None:
    """Training must not read the held-out preference file."""
    if Path(path).name == "pref_pairs_heldout.jsonl":
        raise SystemExit("refusing pref_pairs_heldout.jsonl")


def capture_pref_rows(folder: Path, rows: list) -> dict:
    """Store rewritten and used-as-is pairs. Drop sensitive and frozen keys. No training."""
    import hashlib
    import json
    import os

    from hailports.contextpack import blocked

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    exclude = set()
    frozen = folder / "eval_frozen" / "exclude.json"
    try:
        doc = json.loads(frozen.read_text(encoding="utf-8"))
        for raw in list(doc.get("keys") or []) + list(doc.get("event_ids") or []) + list(doc.get("source_keys") or []):
            exclude.add(str(raw))
            exclude.add(hashlib.sha1(str(raw).encode("utf-8", "replace")).hexdigest()[:10])
    except (OSError, json.JSONDecodeError):
        pass
    train_path = folder / "pref_pairs.jsonl"
    hold_path = folder / "pref_pairs_heldout.jsonl"
    seen = set()
    for path in (train_path, hold_path):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                seen.add(str(json.loads(line).get("key") or ""))
            except json.JSONDecodeError:
                continue
    counts = {"train": 0, "heldout": 0, "skipped": 0}
    for row in rows or []:
        cls = str(row.get("class") or "")
        if cls not in {"rewritten", "used_as_is"}:
            counts["skipped"] += 1
            continue
        key = str(row.get("key") or "")
        draft = str(row.get("draft") or "")
        sent = str(row.get("sent") or "")
        context = str(row.get("context") or "")
        if not key or not draft.strip() or not sent.strip():
            counts["skipped"] += 1
            continue
        if key in exclude or blocked(draft) or blocked(sent) or blocked(context) or row.get("sensitive"):
            counts["skipped"] += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        split = pref_split(key)
        rec = {"key": key, "class": cls, "split": split, "context": context[:1500], "draft": draft[:2000], "sent": sent[:2000], "score": row.get("score")}
        path = hold_path if split == "heldout" else train_path
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)
        counts[split] += 1
    return counts


def prompt_with_lessons(con: sqlite3.Connection, runbook_root: Path | None = None, limit: int = 8) -> str:
    parts = [system_addon(con, limit)]
    if runbook_root:
        path = Path(runbook_root) / "corrections.md"
        if path.exists():
            parts.append(path.read_text(encoding="utf-8")[:2000])
    return "\n".join(part for part in parts if part)
