"""Binary-quantized prefilter for a brute-force sqlite-vec index.

A vec0 table with ~900k x 384 float32 rows is ~1.4 GB per query scan. Under memory pressure
that scan pages from disk and costs 2-6 s per lookup.

Schema assumed (names are parameters): a vec0 table `<vec>` with column `embedding`, its
shadow `<vec>_rowids`, and a rows table `<rows>` with `id` (= vec rowid), `source` and `emb`
(the same float32 vector as a blob).

This keeps a 1-bit sign sketch of every vector (48 bytes each, ~43 MB) in process memory and
in an .npz cache next to the DB. A query takes the top `over` rows by Hamming distance, then
rescores them exactly (cosine on <rows>.emb, the same float32 vectors vec0 holds). Measured
on 30 real queries over a 900k-row index: recall@10 0.94 vs the exact scan at ~8x lower latency.

Safety: read-only on the DB. New rows are picked up by rowid from <vec>_rowids on every
call (cheap); a full rebuild runs in a background thread when the cache is older than
HAILPORTS_VEC_BITS_MAX_AGE_S (default 6 h) so rows rewritten in place get fresh bits. Any error
returns None and the caller falls back to the exact vec0 scan. Kill switch: HAILPORTS_VEC_BINARY=0.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

VEC_TABLE = os.environ.get("HAILPORTS_VEC_TABLE", "vchunks_vec")
ROWS_TABLE = os.environ.get("HAILPORTS_VEC_ROWS", "vchunks")

_LOCK = threading.Lock()
_STATE: dict = {}
_REBUILDING = threading.Event()


def enabled() -> bool:
    return np is not None and os.environ.get("HAILPORTS_VEC_BINARY", "1") != "0" and hasattr(np, "bitwise_count")


def _cache_path(db_path: Path) -> Path:
    return Path(str(db_path) + ".bits.npz")


def _connect(db_path: Path):
    import sqlite_vec
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    return con


def _pack(vec) -> "np.ndarray":
    v = np.asarray(vec, dtype=np.float32)
    # sqlite-vec vec_quantize_binary packs bit i of byte j as dim 8j+i (LSB first).
    return np.packbits((v > 0).astype(np.uint8), bitorder="little")


def _scan(con, where: str = "", args: tuple = ()):
    rows = con.execute(
        f"SELECT rowid, vec_quantize_binary(embedding) FROM {VEC_TABLE} {where}", args
    ).fetchall()
    if not rows:
        return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.uint8)
    rid = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    bits = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.uint8).reshape(len(rows), -1)
    return rid, bits.copy()


def _sources_for(con, rid: "np.ndarray", names: list) -> "np.ndarray":
    """int16 source code per rowid (-1 = orphan vector with no rows-table entry)."""
    codes = np.full(rid.size, -1, dtype=np.int16)
    if rid.size == 0:
        return codes
    lo, hi = int(rid.min()), int(rid.max())
    rows = con.execute(f"SELECT id, source FROM {ROWS_TABLE} WHERE id BETWEEN ? AND ?", (lo, hi)).fetchall()
    if not rows:
        return codes
    idx = {n: i for i, n in enumerate(names)}
    ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    src = np.empty(len(rows), dtype=np.int16)
    for i, r in enumerate(rows):
        name = r[1] or ""
        if name not in idx:
            idx[name] = len(names)
            names.append(name)
        src[i] = idx[name]
    order = np.argsort(ids)
    ids, src = ids[order], src[order]
    pos = np.searchsorted(ids, rid)
    pos[pos >= ids.size] = 0
    hit = ids[pos] == rid
    codes[hit] = src[pos[hit]]
    return codes


def _max_rowid(con) -> int:
    row = con.execute(f"SELECT max(rowid) FROM {VEC_TABLE}_rowids").fetchone()
    return int(row[0] or 0)


def rebuild(db_path: Path) -> dict:
    """Full rebuild from vec0 (~8 s on 900k rows). Writes the .npz atomically."""
    t0 = time.time()
    con = _connect(db_path)
    try:
        top = _max_rowid(con)
        rid, bits = _scan(con)
        names: list = []
        src = _sources_for(con, rid, names)
    finally:
        con.close()
    cache = _cache_path(db_path)
    tmp = cache.with_name(cache.name + ".tmp.npz")
    np.savez(tmp, rid=rid, bits=bits, src=src, names=np.array(names, dtype=object),
             top=np.array([top], dtype=np.int64), built=np.array([t0], dtype=np.float64))
    os.replace(tmp, cache)
    with _LOCK:
        _STATE[str(db_path)] = {"rid": rid, "bits": bits, "src": src, "names": names, "top": top, "built": t0}
    return {"rows": int(rid.size), "orphans": int((src < 0).sum()), "top": top,
            "secs": round(time.time() - t0, 2)}


def _load(db_path: Path):
    key = str(db_path)
    st = _STATE.get(key)
    if st is not None:
        return st
    cache = _cache_path(db_path)
    if not cache.is_file():
        return None
    z = np.load(cache, allow_pickle=True)
    if "src" not in z.files:
        return None
    st = {"rid": z["rid"], "bits": z["bits"], "src": z["src"], "names": list(z["names"]),
          "top": int(z["top"][0]), "built": float(z["built"][0])}
    _STATE[key] = st
    return st


def _bg_rebuild(db_path: Path) -> None:
    if _REBUILDING.is_set():
        return
    _REBUILDING.set()

    def run():
        try:
            rebuild(db_path)
        except Exception as e:  # noqa: BLE001
            print(f"[vec_binary] rebuild failed ({type(e).__name__}: {e})", file=sys.stderr)
        finally:
            _REBUILDING.clear()

    threading.Thread(target=run, name="vec-binary-rebuild", daemon=True).start()


def _top_up(con, db_path: Path, st: dict) -> dict:
    top = _max_rowid(con)
    if top <= st["top"]:
        return st
    ids = [r[0] for r in con.execute(
        f"SELECT rowid FROM {VEC_TABLE}_rowids WHERE rowid > ? ORDER BY rowid LIMIT 20000", (st["top"],)
    ).fetchall()]
    if not ids:
        return st
    parts_r, parts_b = [], []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        r, b = _scan(con, "WHERE rowid IN (%s)" % ",".join("?" * len(chunk)), tuple(chunk))
        if r.size:
            parts_r.append(r)
            parts_b.append(b)
    new_r = np.concatenate(parts_r) if parts_r else np.zeros(0, dtype=np.int64)
    new_b = np.concatenate(parts_b) if parts_b else np.zeros((0, st["bits"].shape[1]), dtype=np.uint8)
    names = list(st["names"])
    new_s = _sources_for(con, new_r, names)
    st = {"rid": np.concatenate([st["rid"], new_r]), "bits": np.concatenate([st["bits"], new_b]),
          "src": np.concatenate([st["src"], new_s]), "names": names,
          "top": max(top if len(ids) < 20000 else int(ids[-1]), st["top"]), "built": st["built"]}
    with _LOCK:
        _STATE[str(db_path)] = st
    return st


def search(qv, want: int, db_path: Path, over: int | None = None, sources=None):
    """Return [(rowid, cosine_distance_l2_on_unit_vectors)] best-first, or None to fall back."""
    if not enabled():
        return None
    try:
        st = _load(db_path)
        if st is None:
            _bg_rebuild(db_path)  # first call builds the cache; this query uses the exact scan
            return None
        max_age = float(os.environ.get("HAILPORTS_VEC_BITS_MAX_AGE_S", "21600"))
        if time.time() - st["built"] > max_age:
            _bg_rebuild(db_path)
        con = _connect(db_path)
        try:
            st = _top_up(con, db_path, st)
            q = np.asarray(qv, dtype=np.float32)
            n = float(np.linalg.norm(q)) or 1.0
            q = q / n
            qb = _pack(q)
            d = np.bitwise_count(np.bitwise_xor(st["bits"], qb)).sum(axis=1, dtype=np.int32)
            if sources:
                # Mask BEFORE picking candidates. Filtering after (as the exact path does) let one
                # dense out-of-scope cluster (near-identical PR file chunks) eat every slot.
                names = st["names"]
                codes = [i for i, n in enumerate(names) if n in set(sources)]
                keep = np.isin(st["src"], np.array(codes, dtype=np.int16))
                d = np.where(keep, d, np.int32(10_000))
                in_scope = int(keep.sum())
            else:
                in_scope = d.size
            over = int(over or min(8000, max(800, want if sources else want * 2)))
            over = min(over, in_scope)
            over = min(over, d.size)
            if over <= 0:
                return []
            cand = st["rid"][np.argpartition(d, over - 1)[:over]].tolist()
            rows = []
            for i in range(0, len(cand), 900):
                chunk = cand[i:i + 900]
                rows.extend(con.execute(
                    f"SELECT id, emb FROM {ROWS_TABLE} WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk
                ).fetchall())
        finally:
            con.close()
        rows = [r for r in rows if r[1] and len(r[1]) == q.size * 4]
        if not rows:
            return []
        M = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), q.size)
        norms = np.linalg.norm(M, axis=1)
        norms[norms == 0] = 1.0
        cos = (M @ q) / norms
        order = np.argsort(-cos)[:want]
        # vec0 reports L2 on unit vectors; callers turn it back into 1 - d^2/2.
        return [(int(rows[i][0]), float(np.sqrt(max(0.0, 2.0 - 2.0 * cos[i])))) for i in order]
    except Exception as e:  # noqa: BLE001
        print(f"[vec_binary] fallback to exact ({type(e).__name__}: {e})", file=sys.stderr)
        return None


if __name__ == "__main__":
    import json
    if len(sys.argv) < 2:
        sys.exit("usage: python -m hailports.brain.vec_binary DB_PATH")
    db = Path(sys.argv[1])
    print(json.dumps(rebuild(db)))
