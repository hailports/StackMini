"""Three freshness states. A missing or old verdict is not a pass."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

MAX_VERDICT_AGE_S = 1800


def snapshot(path: Path | None = None, *, blob: dict | None = None, now: float | None = None, max_age_s: int = MAX_VERDICT_AGE_S) -> dict[str, Any]:
    now = time.time() if now is None else now
    if blob is None:
        if path is None:
            return {"state": "undecidable", "problems": [], "age_s": None, "why": "no verdict"}
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"state": "undecidable", "problems": [], "age_s": None, "why": f"verdict unreadable: {type(exc).__name__}"}
    try:
        age = now - float(blob.get("at") or 0)
    except (TypeError, ValueError):
        age = None
    problems = [str(item) for item in (blob.get("problems") or ())]
    if age is None or age > max_age_s or age < -5:
        shown = "unknown" if age is None else f"{int(age / 60)}m"
        return {"state": "undecidable", "problems": problems, "age_s": age, "why": f"sentinel has not reported in {shown}"}
    if blob.get("degraded") or str(blob.get("verdict") or "").upper() == "RED":
        return {"state": "degraded", "problems": problems, "age_s": age, "why": "; ".join(problems) or "sentinel reported degraded"}
    checked, total = blob.get("checked_n"), blob.get("total_n")
    if isinstance(checked, int) and isinstance(total, int) and checked < total:
        return {"state": "undecidable", "problems": problems, "age_s": age, "why": f"only {checked} of {total} checks could run"}
    return {"state": "green", "problems": [], "age_s": age, "why": ""}


def write_verdict(path: Path, *, degraded: bool, problems: list[str], checked_n: int, total_n: int, now: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": time.time() if now is None else now,
        "degraded": degraded,
        "verdict": "RED" if degraded else "GREEN",
        "problems": problems,
        "checked_n": checked_n,
        "total_n": total_n,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def is_trustworthy(snap: dict) -> bool:
    return snap.get("state") == "green"
