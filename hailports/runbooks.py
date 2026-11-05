"""Index local markdown runbooks and search them by words."""

from __future__ import annotations

import re
from pathlib import Path


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z0-9']+", text) if len(w) > 2}


def index_dir(root: Path) -> list[dict]:
    if not root or not Path(root).exists():
        return []
    rows = []
    for path in sorted(Path(root).rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        title = path.stem.replace("-", " ").replace("_", " ")
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        rows.append({"title": title, "path": str(path), "body": text})
    return rows


def search(root: Path, query: str, limit: int = 5) -> list[dict]:
    q = _words(query)
    scored = []
    for row in index_dir(root):
        overlap = q & _words(row["title"] + " " + row["body"])
        if overlap:
            scored.append((len(overlap), row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in scored[:limit]]
