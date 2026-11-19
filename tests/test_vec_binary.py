"""Binary prefilter: bit order matches sqlite-vec, recall vs exact scan, source mask, top-up."""
import sqlite3

import pytest

np = pytest.importorskip("numpy")
sqlite_vec = pytest.importorskip("sqlite_vec")

from hailports.brain import vec_binary as vb  # noqa: E402

DIM = 64


def _db(tmp_path, n=3000, seed=0):
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(20, DIM)).astype(np.float32)
    path = tmp_path / "idx.sqlite"
    con = sqlite3.connect(path)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.execute(f"CREATE VIRTUAL TABLE vchunks_vec USING vec0(embedding float[{DIM}])")
    con.execute("CREATE TABLE vchunks (id INTEGER PRIMARY KEY, source TEXT, emb BLOB)")
    vecs = []
    for i in range(1, n + 1):
        v = centers[i % 20] + 0.6 * rng.normal(size=DIM).astype(np.float32)
        v /= np.linalg.norm(v)
        vecs.append(v)
        con.execute("INSERT INTO vchunks_vec(rowid, embedding) VALUES (?, ?)", (i, v.tobytes()))
        con.execute("INSERT INTO vchunks VALUES (?, ?, ?)", (i, "a" if i % 3 else "b", v.tobytes()))
    con.commit()
    con.close()
    return path, np.array(vecs), centers


def _exact(vecs, q, k, mask=None):
    cos = vecs @ (q / np.linalg.norm(q))
    if mask is not None:
        cos = np.where(mask, cos, -9)
    return [int(i) + 1 for i in np.argsort(-cos)[:k]]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    vb._STATE.clear()
    monkeypatch.setenv("HAILPORTS_VEC_BINARY", "1")
    yield
    vb._STATE.clear()


def test_pack_matches_sqlite_vec_bit_order():
    con = sqlite3.connect(":memory:")
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    v = np.random.default_rng(1).normal(size=DIM).astype(np.float32)
    ref = con.execute("SELECT vec_quantize_binary(?)", (v.tobytes(),)).fetchone()[0]
    assert vb._pack(v).tobytes() == ref


def test_recall_and_mask(tmp_path):
    if not vb.enabled():
        pytest.skip("numpy without bitwise_count")
    path, vecs, centers = _db(tmp_path)
    info = vb.rebuild(path)
    assert info["rows"] == len(vecs) and info["orphans"] == 0
    rng = np.random.default_rng(7)
    rec = []
    for _ in range(10):
        q = centers[rng.integers(20)] + 0.6 * rng.normal(size=DIM).astype(np.float32)
        got = [r for r, _ in vb.search(q, 10, path)]
        rec.append(len(set(got) & set(_exact(vecs, q, 10))) / 10)
    assert sum(rec) / len(rec) >= 0.9
    mask = np.array([(i + 1) % 3 == 0 for i in range(len(vecs))])
    hits = vb.search(q, 10, path, sources=["b"])
    assert hits and all(r % 3 == 0 for r, _ in hits)
    assert len(set(r for r, _ in hits) & set(_exact(vecs, q, 10, mask))) >= 8


def test_top_up_sees_new_rows(tmp_path):
    if not vb.enabled():
        pytest.skip("numpy without bitwise_count")
    path, vecs, _ = _db(tmp_path, n=500)
    vb.rebuild(path)
    v = (np.ones(DIM) / np.sqrt(DIM)).astype(np.float32)
    con = sqlite3.connect(path)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.execute("INSERT INTO vchunks_vec(rowid, embedding) VALUES (?, ?)", (9999, v.tobytes()))
    con.execute("INSERT INTO vchunks VALUES (?, ?, ?)", (9999, "a", v.tobytes()))
    con.commit()
    con.close()
    hits = vb.search(v, 1, path)
    assert hits[0][0] == 9999 and hits[0][1] < 1e-3


def test_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("HAILPORTS_VEC_BINARY", "0")
    assert vb.search(np.ones(DIM), 5, tmp_path / "none.sqlite") is None
