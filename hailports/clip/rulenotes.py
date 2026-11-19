"""Find the rule a message asks about in local code and docs.

"Does your logic build in X for Level 3?" is answered by the formula, not by a guess.
Searches the folders in ~/.config/paiste/code-roots (and a docs/ folder beside them)
for the key phrases of the ask and returns the matching lines with a little context.
Pure Python so it works on a Mac without ripgrep. Read-only.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

EXTS = {".md", ".txt", ".cls", ".trigger", ".js", ".ts", ".py", ".sql", ".yaml", ".yml", ".json", ".xml", ".html"}
_SKIP_DIRS = {".git", "node_modules", "__tests__", ".sfdx", ".sf", "dist", "build", ".venv", "venv", "__pycache__"}
_STOP = {"the", "and", "for", "your", "does", "this", "that", "have", "with", "from", "will", "would", "should",
         "could", "please", "thanks", "check", "logic", "below", "above", "there", "their", "they", "what", "when",
         "which", "about", "into", "only", "also", "time", "haven", "build", "built", "adjust", "reload", "probably"}


def roots_from(path: Path | None = None) -> list[Path]:
    path = path or Path.home() / ".config" / "paiste" / "code-roots"
    out: list[Path] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        root = Path(raw).expanduser()
        if root.is_dir():
            out.append(root)
            # A force-app/... root also gets the repo's docs/ folder.
            for parent in list(root.parents)[:5]:
                docs = parent / "docs"
                if docs.is_dir() and docs not in out:
                    out.append(docs)
                    break
    return out


def phrases(text: str) -> list[str]:
    body = " ".join((text or "").split())
    out: list[str] = []

    def add(p: str) -> None:
        p = p.strip().lower()
        if p and len(p) >= 4 and p not in out:
            out.append(p)

    for m in re.finditer(r"(?i)\b(level|tier|phase|step|grade|band|bucket|stage|class)\s*[-_ ]?\s*(\d+|[ivx]+)\b", body):
        word, num = m.group(1).lower(), m.group(2).lower()
        add(f"{word} {num}")
        add(f"{word}{num}")
        add(f"{word}_{num}")
        add(f"lvl{num}")
    for m in re.finditer(r"[\"“”']([^\"“”']{3,40})[\"“”']", body):
        add(m.group(1))
    for m in re.finditer(r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})\b", body):
        cand = m.group(1)
        if cand.lower() not in _STOP and len(cand) > 3 and not cand.isupper():
            add(cand)
    return out[:10]


def search(text: str, roots: list[Path], budget_s: float = 4.0, limit: int = 14, context: int = 4) -> str:
    keys = phrases(text)
    if not keys or not roots:
        return ""
    strong = [k for k in keys if re.search(r"\d", k)] or keys[:3]
    deadline = time.monotonic() + budget_s
    hits: list[tuple[int, str]] = []
    for root in roots[:6]:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            if time.monotonic() > deadline:
                break
            for fn in filenames:
                path = Path(dirpath) / fn
                if path.suffix.lower() not in EXTS or fn.endswith("Test.cls") or ".test." in fn:
                    continue
                try:
                    if path.stat().st_size > 2_000_000:
                        continue
                    text_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for idx, line in enumerate(text_lines):
                    low = line.lower()
                    if not any(k in low for k in strong):
                        continue
                    score = sum(3 for k in strong if k in low) + sum(1 for k in keys if k in low)
                    window = text_lines[max(0, idx - 1): idx + context + 1]
                    chunk = "\n".join(w.rstrip()[:200] for w in window)
                    formula = re.search(r"(?:[+*×]\s*\$?\d|<=|>=|\d\s*%|\$\d|\d{3,}(?:,\d{3})*\s*(?:–|-|to)\s*\$?\d)", chunk)
                    if formula:
                        score += 6
                    elif path.suffix.lower() not in {".md", ".txt"}:
                        continue  # code that only names the field is not the rule
                    if path.suffix.lower() in {".md", ".txt"}:
                        score += 2
                    hits.append((score, f"{path.name}:{idx + 1}\n{chunk}"))
    hits.sort(key=lambda item: -item[0])
    out, seen = [], set()
    for _score, chunk in hits:
        key = chunk.split("\n", 1)[-1][:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(chunk)
        if len(out) >= limit:
            break
    return "\n---\n".join(out)[:5000]
