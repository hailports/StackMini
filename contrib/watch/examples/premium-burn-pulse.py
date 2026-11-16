#!/usr/bin/env python3
"""Cheap pulse from local-first decisions. Alerts only on a sharp paid spike."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def decisions_path() -> Path:
    env = os.environ.get("LOCAL_FIRST_DECISIONS", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "local-first" / "router-decisions.jsonl"


def main() -> int:
    path = decisions_path()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    local_n = paid_n = 0
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not str(row.get("ts") or "").startswith(today):
                continue
            if row.get("route") == "local":
                local_n += 1
            else:
                paid_n += 1
    total = local_n + paid_n
    paid_ratio = (paid_n / total) if total else 0.0
    need = total >= 20 and paid_ratio >= 0.75
    out = {
        "need": need,
        "summary": f"today local={local_n} paid={paid_n} paid_ratio={paid_ratio:.2f}",
        "bot": "stack" if need else None,
        "dedupe": f"burn-{today}-{paid_n}",
        "force": False,
    }
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
