#!/usr/bin/env python3
"""Embed work-brain docs the vector index has never seen.

The half-hour refresh only walks littlebird, zoom, email, and digest, so
Salesforce, ADO, pull requests, chatter, and sessions stay invisible to
meaning search. One batch embeds the lane with the most missing docs.
File diffs are left out on purpose. Stops when memory pressure or swap
is already tight. Does not unload the chat model.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path.home() / "claude-stack"
DOCS = ROOT / "data/work_context.db"
VEC = ROOT / "data/learning/work_rag.sqlite"
PY = ROOT / ".venv/bin/python"
LOG = Path.home() / "Library/Logs/claude-stack/work_rag_catchup.log"
LANES = (
    "salesforce",
    "email",
    "sf_chatter",
    "github_pr_comment",
    "ado",
    "github_pr",
    "session_transcript",
    "littlebird",
    "zoom",
    "digest",
    "monday",
)


def _int(cmd: list[str]) -> int:
    try:
        return int(subprocess.check_output(cmd, text=True).strip())
    except (OSError, ValueError, subprocess.CalledProcessError):
        return -1


def gate() -> str:
    pressure = _int(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
    jetsam = _int(["sysctl", "-n", "kern.memorystatus_level"])
    swap = -1
    try:
        out = subprocess.check_output(["sysctl", "vm.swapusage"], text=True)
        # used = 2130.56M
        used = out.split("used =", 1)[1].split("M", 1)[0].strip()
        swap = int(float(used))
    except (OSError, ValueError, IndexError, subprocess.CalledProcessError):
        swap = -1
    # Jetsam in the low teens is normal while the chat model is resident.
    # Holding there left the catch-up waiting forever. Stop only for a real crunch.
    if pressure >= 4:
        return f"pressure {pressure}"
    if 0 <= jetsam <= 6:
        return f"jetsam {jetsam}"
    # The swap file is 4GB while the model is resident. 2.6GB is normal, not full.
    if swap >= 3600:
        return f"swap {swap}MB"
    return ""


def wait_clear() -> str:
    """Warning pressure gets a short breath. Critical pressure, swap, or jetsam stops the pass."""
    for _ in range(8):
        why = gate()
        if not why:
            return ""
        if why.startswith("pressure 2"):
            log(f"breathe {why}")
            time.sleep(20)
            continue
        return why
    return gate() or "pressure stayed at warning"


def missing() -> dict[str, int]:
    docs = sqlite3.connect(f"file:{DOCS}?mode=ro", uri=True)
    vec = sqlite3.connect(f"file:{VEC}?mode=ro", uri=True)
    out: dict[str, int] = {}
    try:
        for src in LANES:
            have = {r[0] for r in vec.execute("SELECT DISTINCT ref FROM vchunks WHERE source=?", (src,))}
            need = docs.execute("SELECT ref FROM docs WHERE source=?", (src,))
            out[src] = sum(1 for (ref,) in need if ref not in have)
    finally:
        docs.close()
        vec.close()
    return out


def log(line: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    text = f"{stamp} {line}\n"
    with LOG.open("a") as fh:
        fh.write(text)


def one_batch(src: str) -> int:
    proc = subprocess.run(
        [str(PY), "-m", "core.work_rag", "index-sources", src],
        cwd=str(ROOT),
        env=os.environ.copy() | {
            "PYTHONPATH": str(ROOT),
            "WORK_RAG_MAX_DOCS": "30",
            "WORK_RAG_MAX_CHUNKS": "90",
        },
        capture_output=True,
        text=True,
        timeout=900,
    )
    blob = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    last = blob.splitlines()[-1] if blob else f"exit {proc.returncode}"
    log(f"batch {src} exit={proc.returncode} {last[:240]}")
    if "already running" in blob:
        return -2
    if proc.returncode != 0:
        return -1
    marker = "docs_embedded"
    if marker not in blob:
        return 0
    try:
        return int(blob.split(marker, 1)[1].split(",", 1)[0].split(":")[1].strip())
    except (IndexError, ValueError):
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--max-batches", type=int, default=1)
    ap.add_argument("--until-done", action="store_true")
    args = ap.parse_args()
    if args.until_done:
        batches = 10**9
    elif args.once:
        batches = 1
    else:
        batches = max(1, args.max_batches)
    skip: set[str] = set()
    held: str | None = None
    since_count = 999
    for _n in range(batches):
        why = gate()
        if why:
            if args.until_done:
                log(f"wait {why}")
                time.sleep(30)
                continue
            log(f"stop {why}")
            return 0
        # Counting the hole scans both databases. Do it every 8 full batches,
        # and again as soon as a batch comes back short.
        if held is None or since_count >= 8:
            gap = missing()
            choices = [(k, v) for k, v in gap.items() if v > 0 and k not in skip]
            log("gap " + " ".join(f"{k}={v}" for k, v in gap.items() if v))
            if not choices:
                if any(v > 0 for v in gap.values()):
                    log("remaining lanes embedded nothing")
                    return 1
                log("caught up")
                return 0
            held = max(choices, key=lambda item: item[1])[0]
            since_count = 0
        src = held
        got = one_batch(src)
        since_count += 1
        if got < 30:
            held = None
        if args.until_done:
            time.sleep(5)
        if got == 0:
            skip.add(src)
            log(f"skip {src}")
            continue
        if got < 0:
            held = None
            if args.until_done:
                log("retry after a failed batch")
                time.sleep(60)
                continue
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
