"""Recent Paiste drafts. The menu lists them on right-click. Nothing is sent."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

KEEP = 30
MENU = 12


def drafts_path() -> Path:
    raw = os.environ.get("PAISTE_DRAFTS") or ""
    return Path(raw) if raw else Path.home() / ".config" / "paiste" / "drafts.jsonl"


def _preview(text: str, width: int = 80) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= width else line[: width - 1].rstrip() + "…"


def load(path: Path | None = None) -> list[dict]:
    path = path or drafts_path()
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("id") and isinstance(row.get("text"), str):
            rows.append(row)
    return rows


def append(text: str, hop: str = "", app: str = "", path: Path | None = None, keep: int = KEEP) -> dict | None:
    text = str(text or "").strip()
    if not text:
        return None
    path = path or drafts_path()
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    digest = hashlib.sha1((at + "\n" + text).encode("utf-8")).hexdigest()[:10]
    row = {"id": at.replace(":", "").replace("+0000", "Z") + "-" + digest, "at": at, "text": text,
           "hop": str(hop or ""), "app": str(app or ""), "preview": _preview(text)}
    rows = (load(path) + [row])[-keep:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for item in rows:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return row


def recent(limit: int = MENU, path: Path | None = None) -> list[dict]:
    return list(reversed(load(path)))[:limit]


def find(draft_id: str, path: Path | None = None) -> dict | None:
    for row in reversed(load(path)):
        if row.get("id") == draft_id:
            return row
    return None


def cmd(action: str, args) -> int:
    """`drafts` prints newest-first JSON. `redo --id X` reprints that draft."""
    if action == "drafts":
        print(json.dumps(recent(), ensure_ascii=False))
        return 0
    draft_id = str(getattr(args, "id", "") or "").strip()
    row = find(draft_id) if draft_id else (recent(1) or [None])[0]
    if not row:
        print("no draft")
        return 2
    if getattr(args, "copy", False):
        from hailports.clip.scene import copy_text
        copy_text(row["text"])
    print(row["text"])
    return 0
