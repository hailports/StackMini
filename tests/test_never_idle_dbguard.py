"""Tests for hailports.never_idle_dbguard (locking, recovery, holds)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

from hailports import never_idle_dbguard as g
from hailports.never_idle import open_queue, queue_depth


def test_locked_connection_survives_threads(tmp_path: Path) -> None:
    lock = threading.RLock()
    con = open_queue(tmp_path / "q.db", lock=lock)
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")

    def work(n: int) -> None:
        for i in range(400):
            con.execute("INSERT INTO t(v) VALUES(?)", (f"{n}-{i}",))
            if i % 25 == 0:
                con.commit()
            assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] > 0
            for _ in con.execute("SELECT id FROM t ORDER BY id DESC LIMIT 2"):
                pass

    ts = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1600
    assert g.quick_check(tmp_path / "q.db") == "ok"
    assert queue_depth(con) == 0
    con.close()


def test_malformed_db_is_recovered(tmp_path: Path) -> None:
    p = tmp_path / "q.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t(v) VALUES(?)", [("x" * 200,)] * 3000)
    con.commit()
    con.close()
    b = bytearray(p.read_bytes())
    for off in range(4096 * 5, 4096 * 5 + 3000):
        b[off] = 0xAB
    p.write_bytes(b)
    assert g.quick_check(p) != "ok"
    rows: list[dict] = []
    assert g.startup_check(p, rows.append) == "ok"
    assert any(r.get("event") == "db_recovered" for r in rows)
    assert list(tmp_path.glob("q.db.malformed-*"))
    con = open_queue(p)
    assert queue_depth(con) == 0
    con.close()


def test_hold_flags_and_memory(tmp_path: Path, monkeypatch) -> None:
    hold = tmp_path / "LOCAL_ENGINE_HOLD"
    pause = tmp_path / "qwen_maint_pause"
    monkeypatch.setattr(g, "HOLD_FLAGS", ((hold, 3 * 3600), (pause, 4 * 3600)))
    monkeypatch.setattr(g, "_avail_mb", lambda: 8000.0)
    assert g.hold_reason() is None
    pause.touch()
    assert g.hold_reason() == "hold_qwen_maint_pause"
    old = time.time() - 5 * 3600
    os.utime(pause, (old, old))
    assert g.hold_reason() is None
    monkeypatch.setattr(g, "_avail_mb", lambda: 900.0)
    assert g.hold_reason() == "mem_avail_900mb"
