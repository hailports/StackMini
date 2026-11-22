"""Never-idle local work engine (public-safe mirror).

Bottomless work-lane precompute queue: context packs, draft-only replies,
ADO prep, SF atlas/findings, account health, work products, history
synthesis, and refresh. Interactive traffic wins through the chat-priority
role. Low-watermark refill keeps the queue from draining. Backs off under
memory/swap pressure and interactive chat. Interactive TTFT is measured and
kept under 2s via chat-priority preemption — TTFT spikes alone do not starve
a non-empty queue. Soft mem-governor throttle yields briefly without parking
the loop; git/filesystem scanners are skipped while queue depth is fat so they
cannot hold the DB lock for minutes. An optional second decode slot engages
only when Splash pressure is normal and interactive TTFT is healthy. Drafts
only; Legal/Finance and secret markers are dropped before any prompt is built.
Social content stays off the local model. Generated packs, the SQLite queue,
draft patches, and few-shot banks stay local / gitignored.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

_BLOCKED_PATH = re.compile(r"(?i)(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)")
_SECRET = re.compile(
    r"(?i)\b(?:password|passwd|seed phrase|2fa|otp|totp|recovery code|private key)\b"
)
_SOCIAL = re.compile(r"(?i)\b(?:twitter|x\.com|linkedin|instagram|tiktok)\b")

# Fresh inbound first; backlog / refresh last. Lower = sooner.
PRODUCER_PRIORITY = {
    "pre_draft": 10,
    "context_pack": 20,
    "ado_prep": 30,
    "sf_atlas": 40,
    "sf_finding": 45,
    "account_health": 50,
    "work_product": 55,
    "enrichment": 58,
    "code_review": 60,
    "self_improve": 65,
    "synthesis": 70,
    "history_summary": 75,
    "backfill": 80,
    "refresh": 85,
}

LOW_WATER = 50
ENQUEUE_SKIP_DEPTH = LOW_WATER * 3
DUAL_TTFT_P95_MAX_MS = 1500.0


def blocked_text(text: str) -> bool:
    blob = text or ""
    return bool(
        _BLOCKED_PATH.search(blob) or _SECRET.search(blob) or _SOCIAL.search(blob)
    )


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def read_bg_throttle(path: Path | None = None) -> dict:
    """Read mem-governor throttle file. Fail-open to full rate."""
    p = path or (Path.home() / "claude-stack/data/flags/bg_throttle.json")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(doc, dict):
            return doc
    except Exception:
        pass
    return {"level": "normal", "rate": 1.0}


def should_run_background(
    *,
    interactive: bool,
    memory_pressure: str,
    swap_mb: float,
    ttft_p95_ms: float | None = None,
    ttft_limit_ms: float = 2000.0,
    throttle: dict | None = None,
) -> str | None:
    """Return a backoff reason, or None when a background job may run.

    TTFT is observed for status/preemption health but does not pause the
    queue — a single contended probe used to starve utilization for minutes.
    """
    del ttft_p95_ms, ttft_limit_ms  # retained for API compat; not a run gate
    if interactive:
        return "interactive"
    thr = throttle if isinstance(throttle, dict) else read_bg_throttle()
    try:
        rate = float(thr.get("rate", 1.0))
    except (TypeError, ValueError):
        rate = 1.0
    level = str(thr.get("level") or "")
    if rate <= 0.0 or level == "critical" or memory_pressure == "critical":
        return "memory_critical"
    if rate < 1.0 or level == "warning":
        return f"throttle_warning_rate_{rate}"
    if swap_mb >= 4500:
        return "swap"
    if memory_pressure == "warning" and swap_mb >= 3500:
        return "memory_warning"
    return None


def soft_throttle_reason(reason: str | None) -> bool:
    """True when background work should continue at a reduced duty cycle."""
    if not reason:
        return False
    if reason.startswith("throttle_") and "_rate_" in reason:
        try:
            rate = float(reason.rsplit("_rate_", 1)[1])
        except ValueError:
            rate = 0.25
        return rate > 0.0
    return False


def throttle_sleep_s(reason: str | None, *, depth: int = 0) -> float:
    """Brief yield under soft throttle — keep the slot warm when the queue is deep."""
    if not reason:
        return 0.0
    if reason in {"memory_critical", "throttle_critical"}:
        return 30.0
    if reason.startswith("throttle_") and "_rate_" in reason:
        try:
            rate = float(reason.rsplit("_rate_", 1)[1])
        except ValueError:
            rate = 0.25
        if rate <= 0:
            return 30.0
        gap = max(0.2, min(1.5, (1.0 - rate) * 2.0))
        if depth >= ENQUEUE_SKIP_DEPTH:
            gap = min(gap, 0.45)
        return gap
    if reason.startswith("memory_warning") or reason.startswith("swap"):
        return 8.0 if depth < LOW_WATER else 2.0
    return 10.0


def should_scan_enqueue(depth: int, *, low_water: int = LOW_WATER) -> bool:
    """Skip git/filesystem scanners while the queue is already fat."""
    return int(depth) < max(int(low_water) * 3, ENQUEUE_SKIP_DEPTH)


def dual_slot_allowed(
    *,
    throttle_rate: float,
    memory_pressure: str,
    interactive: bool,
    depth: int,
    ttft_p95_ms: float | None,
    ttft_limit_ms: float = DUAL_TTFT_P95_MAX_MS,
) -> bool:
    """Second decode slot only when Splash is healthy and chat TTFT is fine."""
    if interactive:
        return False
    if float(throttle_rate) < 0.99 or memory_pressure != "normal":
        return False
    if int(depth) < max(10, LOW_WATER // 2):
        return False
    if ttft_p95_ms is not None and float(ttft_p95_ms) > float(ttft_limit_ms):
        return False
    return True


def open_queue(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS queue (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          producer TEXT NOT NULL,
          priority INTEGER NOT NULL,
          content_hash TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',
          created_at REAL NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_queue_hash
          ON queue(producer, content_hash) WHERE status='queued';
        CREATE TABLE IF NOT EXISTS done (
          producer TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          produced_at REAL NOT NULL,
          PRIMARY KEY (producer, content_hash)
        );
        CREATE TABLE IF NOT EXISTS quality (
          producer TEXT PRIMARY KEY,
          ok INTEGER NOT NULL DEFAULT 0,
          bad INTEGER NOT NULL DEFAULT 0,
          paused_until REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS usefulness (
          producer TEXT PRIMARY KEY,
          reads INTEGER NOT NULL DEFAULT 0,
          produced INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )
    con.commit()
    return con


def queue_depth(con: sqlite3.Connection) -> int:
    row = con.execute(
        "SELECT COUNT(*) FROM queue WHERE status='queued'"
    ).fetchone()
    return int(row[0] if row else 0)


def needs_refill(con: sqlite3.Connection, low_water: int = LOW_WATER) -> bool:
    return queue_depth(con) < int(low_water)


def effective_priority(
    producer: str, *, fresh: bool = False, usefulness: float = 1.0
) -> int:
    base = int(PRODUCER_PRIORITY.get(producer, 80))
    if fresh:
        base = max(1, base - 5)
    if usefulness < 0.2:
        base += 15
    elif usefulness < 0.5:
        base += 5
    elif usefulness > 2.0:
        base = max(1, base - 3)
    return base


def enqueue(con: sqlite3.Connection, producer: str, payload: dict) -> bool:
    raw = json.dumps(payload, sort_keys=True)
    if blocked_text(raw):
        return False
    h = content_hash(raw)
    if con.execute(
        "SELECT 1 FROM done WHERE producer=? AND content_hash=?", (producer, h)
    ).fetchone():
        return False
    paused = con.execute(
        "SELECT paused_until FROM quality WHERE producer=?", (producer,)
    ).fetchone()
    if paused and float(paused[0] or 0) > time.time():
        return False
    useful = 1.0
    urow = con.execute(
        "SELECT reads, produced FROM usefulness WHERE producer=?", (producer,)
    ).fetchone()
    if urow:
        produced = max(1, int(urow[1] or 1))
        useful = max(0.05, min(5.0, (int(urow[0] or 0) + 1) / produced))
    prio = effective_priority(
        producer, fresh=bool(payload.get("fresh")), usefulness=useful
    )
    try:
        con.execute(
            "INSERT INTO queue(producer,priority,content_hash,payload_json,status,created_at)"
            " VALUES(?,?,?,?,'queued',?)",
            (producer, prio, h, raw, time.time()),
        )
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def claim_next(con: sqlite3.Connection) -> tuple | None:
    row = con.execute(
        "SELECT id, producer, content_hash, payload_json FROM queue "
        "WHERE status='queued' ORDER BY priority ASC, id ASC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    con.execute(
        "UPDATE queue SET status='running', attempts=attempts+1 WHERE id=?",
        (row[0],),
    )
    con.commit()
    return row


def mark_done(
    con: sqlite3.Connection, job_id: int, producer: str, content_hash_val: str
) -> None:
    con.execute(
        "INSERT OR REPLACE INTO done(producer,content_hash,produced_at) VALUES(?,?,?)",
        (producer, content_hash_val, time.time()),
    )
    con.execute("DELETE FROM queue WHERE id=?", (job_id,))
    con.execute(
        "INSERT INTO usefulness(producer,reads,produced) VALUES(?,0,1) "
        "ON CONFLICT(producer) DO UPDATE SET produced=produced+1",
        (producer,),
    )
    con.commit()


def record_quality(
    con: sqlite3.Connection, producer: str, ok: bool, fail_pause: float = 0.20
) -> None:
    con.execute(
        "INSERT INTO quality(producer,ok,bad,paused_until) VALUES(?,?,?,0) "
        "ON CONFLICT(producer) DO UPDATE SET ok=ok+?, bad=bad+?",
        (producer, 1 if ok else 0, 0 if ok else 1, 1 if ok else 0, 0 if ok else 1),
    )
    row = con.execute(
        "SELECT ok,bad FROM quality WHERE producer=?", (producer,)
    ).fetchone()
    total = int(row[0]) + int(row[1])
    if total >= 10 and (int(row[1]) / total) > fail_pause:
        con.execute(
            "UPDATE quality SET paused_until=? WHERE producer=?",
            (time.time() + 3600, producer),
        )
    con.commit()


def deterministic_gate(producer: str, output: str, source: str) -> tuple[bool, list[str]]:
    """Public-safe subset of the rock-solid gate (no model calls)."""
    reasons: list[str] = []
    text = (output or "").strip()
    if not text:
        return False, ["empty"]
    if blocked_text(text) or blocked_text(source or ""):
        return False, ["data_line"]
    if producer == "pre_draft" and len(text.split()) < 25:
        reasons.append("thin_draft")
    return (len(reasons) == 0), reasons


def pack_prompt(title: str, source: str) -> str:
    return (
        "Build a context pack a paid editor can read instead of raw sources.\n"
        "Sections: Summary, Open asks, People, Related tickets/PRs, Next steps.\n"
        "Cite only the source. Drafts only.\n\n"
        f"TITLE: {title}\nSOURCE:\n{source[:12000]}"
    )
