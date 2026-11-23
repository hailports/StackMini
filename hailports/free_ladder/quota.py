"""SQLite per-provider quota with daily reset + RPM + Retry-After backoff."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import QUOTA_DB, QUOTAS, ensure_dirs

_LOCK = threading.RLock()


def _day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")


class QuotaStore:
    def __init__(self, path: Path | None = None) -> None:
        ensure_dirs()
        self.path = path or QUOTA_DB
        self._init()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        with _LOCK, self._conn() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage (
                  provider TEXT NOT NULL,
                  day TEXT NOT NULL,
                  req INTEGER NOT NULL DEFAULT 0,
                  tokens INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (provider, day)
                );
                CREATE TABLE IF NOT EXISTS rpm (
                  provider TEXT NOT NULL,
                  minute TEXT NOT NULL,
                  req INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (provider, minute)
                );
                CREATE TABLE IF NOT EXISTS backoff (
                  provider TEXT PRIMARY KEY,
                  until_ts REAL NOT NULL,
                  reason TEXT
                );
                """
            )

    def _limits(self, provider: str) -> dict[str, int]:
        return dict(QUOTAS.get(provider) or QUOTAS.get("openrouter") or {"req_day": 100, "tok_day": 100000, "rpm": 10})

    def backoff_until(self, provider: str) -> float:
        with _LOCK, self._conn() as con:
            row = con.execute("SELECT until_ts FROM backoff WHERE provider=?", (provider,)).fetchone()
            return float(row["until_ts"]) if row else 0.0

    def set_backoff(self, provider: str, seconds: float, reason: str = "429") -> None:
        until = time.time() + max(1.0, float(seconds))
        with _LOCK, self._conn() as con:
            con.execute(
                "INSERT INTO backoff(provider, until_ts, reason) VALUES(?,?,?) "
                "ON CONFLICT(provider) DO UPDATE SET until_ts=excluded.until_ts, reason=excluded.reason",
                (provider, until, reason[:120]),
            )

    def clear_backoff(self, provider: str) -> None:
        with _LOCK, self._conn() as con:
            con.execute("DELETE FROM backoff WHERE provider=?", (provider,))

    def status(self, provider: str) -> dict[str, Any]:
        day = _day()
        minute = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")
        lim = self._limits(provider)
        with _LOCK, self._conn() as con:
            u = con.execute(
                "SELECT req, tokens FROM usage WHERE provider=? AND day=?", (provider, day)
            ).fetchone()
            r = con.execute(
                "SELECT req FROM rpm WHERE provider=? AND minute=?", (provider, minute)
            ).fetchone()
            bo = con.execute("SELECT until_ts, reason FROM backoff WHERE provider=?", (provider,)).fetchone()
        req = int(u["req"]) if u else 0
        tok = int(u["tokens"]) if u else 0
        rpm = int(r["req"]) if r else 0
        until = float(bo["until_ts"]) if bo else 0.0
        ok = True
        why = ""
        now = time.time()
        if until > now:
            ok = False
            why = f"backoff:{bo['reason'] if bo else '429'}"
        elif req >= lim["req_day"]:
            ok = False
            why = "req_day"
        elif tok >= lim["tok_day"]:
            ok = False
            why = "tok_day"
        elif rpm >= lim["rpm"]:
            ok = False
            why = "rpm"
        return {
            "provider": provider,
            "ok": ok,
            "why": why,
            "req": req,
            "tokens": tok,
            "rpm": rpm,
            "limits": lim,
            "backoff_until": until,
        }

    def allow(self, provider: str) -> tuple[bool, str]:
        st = self.status(provider)
        return bool(st["ok"]), st.get("why") or ""

    def record(self, provider: str, tokens: int = 0) -> None:
        day = _day()
        minute = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")
        tokens = max(0, int(tokens or 0))
        with _LOCK, self._conn() as con:
            con.execute(
                "INSERT INTO usage(provider, day, req, tokens) VALUES(?,?,1,?) "
                "ON CONFLICT(provider, day) DO UPDATE SET req=req+1, tokens=tokens+excluded.tokens",
                (provider, day, tokens),
            )
            con.execute(
                "INSERT INTO rpm(provider, minute, req) VALUES(?,?,1) "
                "ON CONFLICT(provider, minute) DO UPDATE SET req=req+1",
                (provider, minute),
            )
