"""Never-idle local work engine (public-safe mirror).

Keeps a local chat server busy on meaningful precompute: context packs,
draft-only replies, entity enrichment, read-only review notes, few-shot
harvest, and history backfill. Interactive traffic wins through the
chat-priority role. Backs off under memory/swap pressure and when
interactive TTFT exceeds 2s. Drafts only; Legal/Finance and secret markers
are dropped before any prompt is built. Social content stays off the local
model. Generated packs, the SQLite queue, and few-shot banks stay local.
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

PRODUCER_PRIORITY = {
    "context_pack": 10,
    "pre_draft": 20,
    "enrichment": 30,
    "code_review": 40,
    "self_improve": 50,
    "backfill": 90,
}


def blocked_text(text: str) -> bool:
    blob = text or ""
    return bool(_BLOCKED_PATH.search(blob) or _SECRET.search(blob) or _SOCIAL.search(blob))


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
    ttft_p95_ms: float | None,
    ttft_limit_ms: float = 2000.0,
    throttle: dict | None = None,
) -> str | None:
    """Return a backoff reason, or None when a background job may run.

    Throttle levels from mem-governor: normal=1.0, warning=0.25, critical=0.
    cheap ingest/embed lanes do not call this.
    """
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
        return "throttle_warning"
    if swap_mb >= 4500:
        return "swap"
    if memory_pressure == "warning" and swap_mb >= 3500:
        return "memory_warning"
    if ttft_p95_ms is not None and ttft_p95_ms > ttft_limit_ms:
        return "ttft"
    return None


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
        """
    )
    con.commit()
    return con


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
    prio = int(PRODUCER_PRIORITY.get(producer, 80))
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
    con.execute("UPDATE queue SET status='running', attempts=attempts+1 WHERE id=?", (row[0],))
    con.commit()
    return row


def mark_done(con: sqlite3.Connection, job_id: int, producer: str, content_hash_val: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO done(producer,content_hash,produced_at) VALUES(?,?,?)",
        (producer, content_hash_val, time.time()),
    )
    con.execute("DELETE FROM queue WHERE id=?", (job_id,))
    con.commit()


def record_quality(con: sqlite3.Connection, producer: str, ok: bool, fail_pause: float = 0.20) -> None:
    con.execute(
        "INSERT INTO quality(producer,ok,bad,paused_until) VALUES(?,?,?,0) "
        "ON CONFLICT(producer) DO UPDATE SET ok=ok+?, bad=bad+?",
        (producer, 1 if ok else 0, 0 if ok else 1, 1 if ok else 0, 0 if ok else 1),
    )
    row = con.execute("SELECT ok,bad FROM quality WHERE producer=?", (producer,)).fetchone()
    total = int(row[0]) + int(row[1])
    if total >= 10 and (int(row[1]) / total) > fail_pause:
        con.execute(
            "UPDATE quality SET paused_until=? WHERE producer=?",
            (time.time() + 3600, producer),
        )
    con.commit()


def pack_prompt(title: str, source: str) -> str:
    return (
        "Build a context pack a paid editor can read instead of raw sources.\n"
        "Sections: Summary, Open asks, People, Related tickets/PRs, Next steps.\n"
        "Cite only the source. Drafts only.\n\n"
        f"TITLE: {title}\nSOURCE:\n{source[:12000]}"
    )
