"""Per-thread backoff when an Outlook compose writes nothing. No sends.

A hold is not a silent multi-hour park. The same note is regenerated a few
times, with a short wait between tries. After that it is marked for review
once. A waiting note does not get opened again on every lap.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

PATH = Path.home() / "llm-appliance/runtime/local-cycle-browser/owa_compose_backoff.json"
_COOLDOWN_S = (4 * 3600, 12 * 3600)
# Fresh generation, not a re-grade of the same text. Three tries, then review.
HOLD_MAX = 3
HOLD_WAIT_S = (120, 300, 600)


def _load() -> dict:
    try:
        data = json.loads(PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    if len(data) > 400:
        items = sorted(data.items(), key=lambda kv: float((kv[1] or {}).get("at") or 0))
        data = dict(items[-300:])
    PATH.write_text(json.dumps(data))


def _key(mid: str) -> str:
    return str(mid or "").strip().strip("<>")


def _row(mid: str) -> dict:
    return dict(_load().get(_key(mid)) or {})


def is_hold(reason: str) -> bool:
    text = str(reason or "").lower()
    return (
        text.startswith("hold:")
        or text.startswith("need_dig")
        or "off_topic" in text
    )


def _same_hash(row: dict, content_hash: str) -> bool:
    ch = str(content_hash or "")
    stored = str(row.get("content_hash") or "")
    if ch and stored and ch != stored:
        return False
    return True


def peek(mid: str, content_hash: str = "") -> str:
    """'' when a compose may run. wait, review, or settled otherwise."""
    row = _row(mid)
    if not row or not _same_hash(row, content_hash):
        return ""
    if row.get("settled") or row.get("reviewed"):
        return "settled"
    reason = str(row.get("reason") or "")
    fails = int(row.get("fails") or 0)
    if is_hold(reason) and (row.get("review_due") or fails >= HOLD_MAX):
        return "review"
    next_ok = float(row.get("next_ok") or 0)
    if next_ok and time.time() < next_ok:
        return "wait"
    return ""


def check(mid: str, content_hash: str) -> str:
    """Skip token when this same message must not be composed again yet."""
    state = peek(mid, content_hash)
    if state == "wait":
        return "owa_compose_backoff"
    if state == "review":
        return "owa_hold_review"
    if state == "settled":
        return "owa_hold_settled"
    return ""


def reason_for(mid: str) -> str:
    return str(_row(mid).get("reason") or "")[:160]


def best_text(mid: str) -> str:
    return str(_row(mid).get("best_text") or "")


def record_empty(mid: str, content_hash: str, reason: str, best_text: str = "") -> dict:
    key = _key(mid)
    if not key:
        return {}
    data = _load()
    prev = dict(data.get(key) or {})
    ch = str(content_hash or "")
    if ch and prev.get("content_hash") and ch != str(prev.get("content_hash")):
        prev = {}
    fails = int(prev.get("fails") or 0) + 1
    now = time.time()
    held = is_hold(reason)
    if held:
        blocked = False
        review_due = fails >= HOLD_MAX
        if review_due:
            next_ok = now
        else:
            next_ok = now + HOLD_WAIT_S[min(fails, len(HOLD_WAIT_S)) - 1]
    else:
        blocked = fails >= 3
        review_due = False
        if blocked:
            next_ok = now + 365 * 24 * 3600
        else:
            next_ok = now + _COOLDOWN_S[min(fails, len(_COOLDOWN_S)) - 1]
    kept = str(best_text or "").strip()
    prior_best = str(prev.get("best_text") or "")
    if len(kept) < len(prior_best):
        kept = prior_best
    row = {
        "content_hash": ch or prev.get("content_hash") or "",
        "fails": fails,
        "blocked": blocked,
        "reason": str(reason or "")[:160],
        "next_ok": next_ok,
        "at": now,
        "best_text": kept[:2000],
        "review_due": review_due,
        "reviewed": False,
        "settled": False,
    }
    data[key] = row
    _save(data)
    return row


def mark_reviewed(mid: str, how: str = "") -> None:
    key = _key(mid)
    if not key:
        return
    data = _load()
    row = dict(data.get(key) or {})
    if not row:
        row = {"content_hash": "", "fails": HOLD_MAX, "reason": "hold:review"}
    row["reviewed"] = True
    row["settled"] = True
    row["review_due"] = False
    row["blocked"] = False
    row["how"] = str(how or "")[:40]
    row["next_ok"] = time.time() + 7 * 24 * 3600
    row["at"] = time.time()
    data[key] = row
    _save(data)


def heal_permanent_holds() -> list:
    """Turn a year-long hold into one fresh try, or one review. Does not send."""
    data = _load()
    now = time.time()
    changed = []
    for key, raw in list(data.items()):
        row = dict(raw or {})
        if row.get("settled") or row.get("reviewed"):
            continue
        if not is_hold(row.get("reason") or ""):
            continue
        nxt = float(row.get("next_ok") or 0)
        long = bool(row.get("blocked")) or nxt > now + 900
        if not long:
            continue
        fails = int(row.get("fails") or 0)
        row["blocked"] = False
        if fails >= HOLD_MAX:
            row["review_due"] = True
            row["next_ok"] = now
            row["how_heal"] = "review"
        else:
            row["review_due"] = False
            row["next_ok"] = now
            row["how_heal"] = "retry"
        row["at"] = now
        data[key] = row
        changed.append(key)
    if changed:
        _save(data)
    return changed
