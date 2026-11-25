"""SQLite + host guards for never-idle.

- LockedConnection: every call on the shared writer is serialized by one RLock
  and results are fully materialized inside the lock, so no thread ever steps a
  statement while another thread uses the same sqlite3* handle. Unlocked
  cross-thread use on system Python 3.9 (threadsafety=1) segfaulted inside
  sqlite3BtreeCursor/VdbeExec and left the db "malformed".
- quick_check / recover_db: startup integrity probe and safe rebuild
  (sqlite3 .recover -> fresh file -> quick_check -> atomic swap; bad file kept
  aside). Falls back to iterdump, then to an empty db (schema is recreated by
  the caller) so the daemon always keeps going.
- hold_reason: LOCAL_ENGINE_HOLD (3h window) / qwen_maint_pause (4h window)
  flags and severe FREE+purgeable pressure only when the local model is not
  serving (inactive pages are the model working set — never count them).
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

FLAGS = Path(
    os.environ.get("NEVER_IDLE_FLAGS_DIR", str(Path.home() / ".hailports" / "flags"))
)
HOLD_FLAGS = (
    (FLAGS / "LOCAL_ENGINE_HOLD", 3 * 3600),  # local-engine maintenance hold
    (FLAGS / "qwen_maint_pause", 4 * 3600),  # model maintenance pause
)
# Floor on FREE+PURGEABLE only. Counting inactive tripped the hold whenever a
# large local model was resident and parked the engine for 60s loops.
MEM_FLOOR_MB = float(os.environ.get("NEVER_IDLE_MEM_FLOOR_MB", "256"))
_MEM_CACHE: tuple[float, float | None] = (0.0, None)


# ---------------------------------------------------------------- connection
class _Result:
    """Fully materialized cursor result (safe to read outside the lock)."""

    __slots__ = ("_rows", "_i", "rowcount", "lastrowid", "description")

    def __init__(self, cur: sqlite3.Cursor):
        self.description = cur.description
        self._rows = cur.fetchall() if cur.description else []
        self.rowcount = cur.rowcount
        self.lastrowid = cur.lastrowid
        self._i = 0
        cur.close()

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        r = self._rows[self._i]
        self._i += 1
        return r

    def fetchall(self):
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out

    def fetchmany(self, size: int = 1):
        out = self._rows[self._i:self._i + size]
        self._i += len(out)
        return out

    def __iter__(self):
        while True:
            r = self.fetchone()
            if r is None:
                return
            yield r


def make_locked_factory(lock: "threading.RLock"):
    class LockedConnection(sqlite3.Connection):
        def execute(self, sql, params=()):  # type: ignore[override]
            with lock:
                return _Result(super().execute(sql, params))

        def executemany(self, sql, seq):  # type: ignore[override]
            with lock:
                return _Result(super().executemany(sql, seq))

        def executescript(self, script):  # type: ignore[override]
            with lock:
                return super().executescript(script)

        def commit(self):  # type: ignore[override]
            with lock:
                return super().commit()

        def rollback(self):  # type: ignore[override]
            with lock:
                return super().rollback()

        def close(self):  # type: ignore[override]
            with lock:
                return super().close()

    return LockedConnection


# ---------------------------------------------------------------- integrity
def is_corruption(exc: BaseException) -> bool:
    m = str(exc).lower()
    return isinstance(exc, sqlite3.DatabaseError) and (
        "malformed" in m or "not a database" in m or "disk image" in m
    )


def quick_check(path: Path, timeout: float = 30.0) -> str:
    """'ok' or the first problem. Short-lived connection, never raises."""
    if not path.exists():
        return "ok"
    try:
        con = sqlite3.connect(str(path), timeout=timeout)
        try:
            con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            rows = con.execute("PRAGMA quick_check(4)").fetchall()
        finally:
            con.close()
        bad = [str(r[0]) for r in rows if str(r[0]) != "ok"]
        return "ok" if not bad else "; ".join(bad)[:300]
    except sqlite3.Error as exc:
        return f"{type(exc).__name__}: {exc}"[:300]


def checkpoint(con: sqlite3.Connection, mode: str = "PASSIVE") -> None:
    try:
        con.execute(f"PRAGMA wal_checkpoint({mode})")
    except sqlite3.Error:
        pass


def _sidecars(path: Path) -> list[Path]:
    return [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]


def _sqlite3_cli() -> str | None:
    for c in ("/opt/homebrew/opt/sqlite/bin/sqlite3", "/usr/bin/sqlite3"):
        if os.access(c, os.X_OK):
            return c
    return shutil.which("sqlite3")


def _rebuild_into(src: Path, dst: Path) -> str:
    """Write a recovered copy of src into dst. Returns method used or ''."""
    cli = _sqlite3_cli()
    if cli:
        try:
            p = subprocess.run(
                f'"{cli}" "{src}" .recover | "{cli}" "{dst}"',
                shell=True, capture_output=True, timeout=900,
            )
            if p.returncode == 0 and dst.exists() and quick_check(dst) == "ok":
                return "recover"
        except (subprocess.SubprocessError, OSError):
            pass
        for f in _sidecars(dst):
            f.unlink(missing_ok=True)
    try:
        s = sqlite3.connect(str(src))
        d = sqlite3.connect(str(dst))
        try:
            for stmt in s.iterdump():
                try:
                    d.execute(stmt)
                except sqlite3.Error:
                    pass
            d.commit()
        finally:
            s.close()
            d.close()
        if quick_check(dst) == "ok":
            return "iterdump"
    except sqlite3.Error:
        pass
    for f in _sidecars(dst):
        f.unlink(missing_ok=True)
    return ""


def recover_db(path: Path, log=lambda row: None, *, reason: str = "") -> dict:
    """Move the bad db aside, rebuild into a fresh file, atomic swap.

    Caller must have closed its own connections first. Never raises; on total
    failure leaves no db at `path` so the caller recreates an empty schema.
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    aside = path.with_name(f"{path.name}.malformed-{ts}")
    out = {"reason": reason[:160], "aside": str(aside), "method": ""}
    try:
        for f in _sidecars(path):
            if f.exists():
                os.replace(f, Path(str(aside) + f.name[len(path.name):]))
        fresh = path.with_name(f"{path.name}.rebuild-{ts}")
        method = _rebuild_into(aside, fresh) if aside.exists() else ""
        if method:
            os.replace(fresh, path)
        out["method"] = method or "empty"
        out["quick_check"] = quick_check(path)
    except OSError as exc:
        out["error"] = str(exc)[:160]
    log({"event": "db_recovered", **out})
    return out


def startup_check(path: Path, log=lambda row: None) -> str:
    """quick_check at startup; auto-recover when it is not ok."""
    qc = quick_check(path)
    log({"event": "db_quick_check", "result": qc[:160]})
    if qc != "ok":
        recover_db(path, log, reason=qc)
        qc = quick_check(path)
    return qc


# ---------------------------------------------------------------- holds
def _avail_mb() -> float | None:
    """Truly reclaimable pages only: free + purgeable + speculative (MB).

    Inactive is the model working set. Counting it made mem_avail look "low"
    whenever the engine was healthy and loaded. Cached 15s.
    """
    global _MEM_CACHE
    now = time.time()
    if now - _MEM_CACHE[0] < 15.0:
        return _MEM_CACHE[1]
    val = None
    try:
        txt = subprocess.run(
            ["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=5
        ).stdout
        m = re.search(r"page size of (\d+)", txt)
        page = int(m.group(1)) if m else 16384
        pages = 0
        for key in ("free", "purgeable", "speculative"):
            mm = re.search(rf"Pages {key}:\s+(\d+)", txt)
            if mm:
                pages += int(mm.group(1))
        val = pages * page / 1048576.0
    except (subprocess.SubprocessError, OSError, ValueError):
        val = None
    _MEM_CACHE = (now, val)
    return val


def _model_serving() -> bool:
    """True when the configured local health URL answers (model owns the RAM)."""
    url = os.environ.get(
        "NEVER_IDLE_HEALTH_URL", "http://127.0.0.1:8080/health"
    ).strip()
    if not url:
        return False
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=1.0) as r:
            return int(getattr(r, "status", 200) or 200) < 500
    except Exception:
        alt = os.environ.get(
            "NEVER_IDLE_MODELS_URL", "http://127.0.0.1:8080/v1/models"
        ).strip()
        if not alt:
            return False
        try:
            with urllib.request.urlopen(alt, timeout=1.0) as r:
                return int(getattr(r, "status", 200) or 200) < 500
        except Exception:
            return False


def hold_reason(now: float | None = None) -> str | None:
    now = now or time.time()
    for flag, max_age in HOLD_FLAGS:
        try:
            age = now - flag.stat().st_mtime
        except OSError:
            continue
        if age < max_age:
            return f"hold_{flag.name}"
    # Model already serving: do not park on reclaimable-page floor.
    if _model_serving():
        return None
    avail = _avail_mb()
    if avail is not None and avail < MEM_FLOOR_MB:
        return f"mem_avail_{int(avail)}mb"
    return None
