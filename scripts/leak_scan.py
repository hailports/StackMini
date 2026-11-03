#!/usr/bin/env python3
"""Pattern scan used by scripts/leak_scan.sh. Public git revisions are ignored."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOW_HEX = {
    "f174553e1621c6a22a64e7bfcbb58d3a5c49e6fb",
    "d81235049384534c167caea52b85a694f6103d14",
}
ALLOW_EMAIL = {"noreply@hailports.com"}
ALLOW_IP = {"127.0.0.1", "0.0.0.0"}

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
B64 = re.compile(r"\b(?:[A-Za-z0-9+/]{40,}={0,2})\b")

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".local", "models", "build", "dist", ".venv"}


def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.suffix.lower() in {".gguf", ".png", ".zip", ".tar", ".gz"}:
            continue
        yield path


def git_shas(root: Path) -> set[str]:
    if not (root / ".git").exists():
        return set()
    out = subprocess.check_output(["git", "-C", str(root), "rev-list", "--all"], text=True, errors="replace")
    return {line.strip().lower() for line in out.splitlines() if line.strip()}


def scan_text(label: str, text: str, extra_hex: set[str] | None = None) -> list[str]:
    hits = []
    allow_hex = set(ALLOW_HEX)
    if extra_hex:
        allow_hex |= {item.lower() for item in extra_hex}
    for rx, kind, allow in (
        (EMAIL, "email", ALLOW_EMAIL),
        (IP, "ip", ALLOW_IP),
        (PHONE, "phone", set()),
        (UUID, "uuid", set()),
        (HEX, "hex", allow_hex),
    ):
        for match in rx.findall(text):
            if match in allow:
                continue
            if kind == "ip":
                parts = [int(p) for p in match.split(".")]
                if any(p > 255 for p in parts):
                    continue
            hits.append(f"{label}: {kind} {match}")
    for match in B64.findall(text):
        if match.lower() in allow_hex:
            continue
        # Skip ordinary words and urls. Require mixed case or digits plus letters.
        if match.startswith("http") or "/" in match:
            continue
        if match.isalpha():
            continue
        hits.append(f"{label}: b64 {match[:12]}...")
    return hits


def scan_tree(root: Path) -> list[str]:
    hits = []
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = str(path.relative_to(root))
        hits.extend(scan_text(rel, text))
    return hits


def scan_history(root: Path) -> list[str]:
    if not (root / ".git").exists():
        return []
    shas = git_shas(root)
    log = subprocess.check_output(["git", "-C", str(root), "log", "--all", "--format=fuller"], text=True, errors="replace")
    hits = scan_text("git-log", log, shas)
    try:
        patch = subprocess.check_output(["git", "-C", str(root), "log", "--all", "-p"], text=True, errors="replace")
    except subprocess.CalledProcessError:
        return hits
    hits.extend(scan_text("git-patch", patch, shas))
    return hits


def main() -> int:
    hits = scan_tree(ROOT) + scan_history(ROOT)
    if hits:
        print("\n".join(hits))
        print(f"pattern hits: {len(hits)}")
        return 1
    print("pattern hits: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
