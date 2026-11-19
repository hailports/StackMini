#!/usr/bin/env python3
"""Fail if internal-only terms or paths reach the public tree or commit messages.

Usage: public_terms_check.py [--messages REV_RANGE]
Patterns are written with character classes so this file does not match itself.
"""
import re, subprocess, sys
from pathlib import Path

TERMS = re.compile(
    r"d[a]rk[ _.-]?g[a]ry|w[o]rk[ _-]?br[a]in|s[a]lesf[o]rce|v[a]lm[o]nt|\bp[e]rsonas?\b|\bs[o]cials\b",
    re.I,
)
PATHS = re.compile(r"(^|/)(s[o]cials?[^/]*|stack-sw[e]ep)(/|$)|d[a]rk[_-]?g[a]ry", re.I)
SKIP = {".git", "__pycache__", ".pytest_cache", ".venv", "build", "dist"}


def tree_hits(root: Path):
    out = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
        text=True).splitlines()
    for rel in sorted(set(out)):
        if not (root / rel).exists():
            continue
        if PATHS.search(rel):
            yield f"{rel}: internal path"
            continue
        p = root / rel
        if not p.is_file() or any(part in SKIP for part in Path(rel).parts):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        m = TERMS.search(text)
        if m:
            yield f"{rel}: internal term"


def message_hits(root: Path, rev_range: str):
    log = subprocess.check_output(
        ["git", "-C", str(root), "log", rev_range, "--format=%h%x1f%B%x1e"], text=True)
    for rec in log.split("\x1e"):
        if "\x1f" not in rec:
            continue
        sha, body = rec.strip().split("\x1f", 1)
        if TERMS.search(body):
            yield f"commit {sha}: internal term in message"


def main() -> int:
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    hits = list(tree_hits(root))
    if "--messages" in sys.argv:
        rng = sys.argv[sys.argv.index("--messages") + 1]
        hits += list(message_hits(root, rng))
    for h in hits:
        print(h)
    print(f"public terms check: {len(hits)} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
