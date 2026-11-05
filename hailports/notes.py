"""Local notes: ingest, embed, and search.

Connectors read files and public pages into the note file. Search mixes the
full-text index with vectors from a bge-small (or any OpenAI-compatible)
embeddings server. A missing server does not invent vectors.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import urllib.request
from pathlib import Path

from hailports.webtools import html_to_text

_TEXT = {".md", ".txt", ".text", ".rst", ".markdown"}
_HTML = {".html", ".htm"}


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """create table if not exists docs (
            id integer primary key,
            title text,
            body text not null,
            created real not null
        )"""
    )
    con.execute(
        """create virtual table if not exists docs_fts using fts5(title, body)"""
    )
    return con


def add(con: sqlite3.Connection, title: str, body: str) -> int:
    cur = con.execute(
        "insert into docs (title, body, created) values (?, ?, ?)",
        (title, body, time.time()),
    )
    doc_id = int(cur.lastrowid)
    con.execute("insert into docs_fts (rowid, title, body) values (?, ?, ?)", (doc_id, title, body))
    con.commit()
    return doc_id


def store_vector(con: sqlite3.Connection, doc_id: int, backend: str, vector: list[float]) -> None:
    con.execute(
        """create table if not exists vec (
            doc_id integer primary key,
            backend text,
            vector text
        )"""
    )
    con.execute(
        "insert or replace into vec (doc_id, backend, vector) values (?, ?, ?)",
        (doc_id, backend, json.dumps(vector)),
    )
    con.commit()


def brain_status(con: sqlite3.Connection) -> dict:
    docs = int(con.execute("select count(*) from docs").fetchone()[0])
    try:
        rows = con.execute("select backend, count(*) from vec group by backend").fetchall()
    except sqlite3.OperationalError:
        rows = []
    return {"docs": docs, "embeddings": [{"backend": row[0], "count": row[1]} for row in rows]}


def search(con: sqlite3.Connection, query: str, limit: int = 5) -> list[dict]:
    tokens = [t for t in query.replace('"', " ").split() if t.strip()]
    if not tokens:
        return []
    match = " OR ".join('"' + t.replace('"', "") + '"' for t in tokens[:8])
    try:
        rows = con.execute(
            "select rowid, title, body from docs_fts where docs_fts match ? limit ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        like = f"%{tokens[0]}%"
        rows = con.execute(
            "select id, title, body from docs where body like ? or title like ? limit ?",
            (like, like, limit),
        ).fetchall()
    return [{"id": r[0], "title": r[1], "body": r[2]} for r in rows]


def _meta(con: sqlite3.Connection) -> None:
    con.execute(
        """create table if not exists doc_meta (
            doc_id integer primary key,
            source text,
            kind text,
            content_hash text,
            updated real,
            embedded_at real
        )"""
    )


def note_meta(con: sqlite3.Connection, doc_id: int, source: str, kind: str, body: str, updated: float | None = None) -> None:
    _meta(con)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    con.execute(
        "insert or replace into doc_meta (doc_id, source, kind, content_hash, updated, embedded_at) values (?, ?, ?, ?, ?, ?)",
        (doc_id, source, kind, digest, updated if updated is not None else time.time(), None),
    )
    con.commit()


def _ingest_one(con: sqlite3.Connection, title: str, body: str, source: str, kind: str) -> int:
    _meta(con)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    row = con.execute("select doc_id from doc_meta where content_hash = ?", (digest,)).fetchone()
    if row:
        return int(row[0])
    doc_id = add(con, title, body)
    note_meta(con, doc_id, source, kind, body)
    return doc_id


def ingest_path(con: sqlite3.Connection, path: Path) -> list[int]:
    """Read a file or a directory of text files. Nothing is deleted."""
    path = Path(path)
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = [item for item in sorted(path.rglob("*")) if item.is_file() and item.suffix.lower() in _TEXT | _HTML]
    else:
        return []
    ids = []
    for item in files:
        raw = item.read_text(encoding="utf-8", errors="replace")
        if item.suffix.lower() in _HTML:
            raw = html_to_text(raw)
        if not raw.strip():
            continue
        ids.append(_ingest_one(con, item.stem, raw, str(item), "file"))
    return ids


def ingest_url(con: sqlite3.Connection, url: str, fetch) -> int:
    body = fetch(url)
    title = url.rsplit("/", 1)[-1] or url
    return _ingest_one(con, title[:80], body, url, "url")


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / ((left_norm ** 0.5) * (right_norm ** 0.5))


def _vectors(con: sqlite3.Connection) -> dict[int, list[float]]:
    try:
        rows = con.execute("select doc_id, vector from vec").fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for doc_id, blob in rows:
        try:
            out[int(doc_id)] = list(json.loads(blob))
        except (TypeError, json.JSONDecodeError):
            continue
    return out


def hybrid_search(con: sqlite3.Connection, query: str, query_vector: list[float] | None = None, limit: int = 5) -> list[dict]:
    """Full-text hits mixed with cosine similarity when a query vector is present."""
    lexical = search(con, query, limit=20)
    if not query_vector:
        return lexical[:limit]
    vecs = _vectors(con)
    pool = {row["id"]: row for row in lexical}
    for doc_id in vecs:
        if doc_id in pool:
            continue
        row = con.execute("select id, title, body from docs where id = ?", (doc_id,)).fetchone()
        if row:
            pool[int(row[0])] = {"id": row[0], "title": row[1], "body": row[2]}
    lex_rank = {row["id"]: index for index, row in enumerate(lexical)}
    ranked = []
    for doc_id, row in pool.items():
        lexical_score = 1.0 / (1 + lex_rank[doc_id]) if doc_id in lex_rank else 0.0
        vector_score = cosine(query_vector, vecs.get(doc_id) or [])
        if lexical_score == 0 and vector_score <= 0:
            continue
        score = (0.45 * lexical_score) + (0.55 * max(0.0, vector_score))
        item = dict(row)
        item["score"] = round(score, 4)
        ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in ranked[:limit]]


def stale_ids(con: sqlite3.Connection, now: float, max_age_s: float) -> list[int]:
    _meta(con)
    rows = con.execute("select doc_id, updated from doc_meta").fetchall()
    stale = []
    for doc_id, updated in rows:
        try:
            age = now - float(updated)
        except (TypeError, ValueError):
            age = max_age_s + 1
        if age > max_age_s:
            stale.append(int(doc_id))
    return stale


def mark_embedded(con: sqlite3.Connection, doc_id: int, when: float) -> None:
    _meta(con)
    con.execute("update doc_meta set embedded_at = ?, updated = ? where doc_id = ?", (when, when, doc_id))
    con.commit()


def reembed_stale(con: sqlite3.Connection, embed_fn, now: float, max_age_s: float) -> int:
    """Recompute vectors for notes older than max_age_s. The note text stays."""
    count = 0
    for doc_id in stale_ids(con, now, max_age_s):
        row = con.execute("select body from docs where id = ?", (doc_id,)).fetchone()
        if not row:
            continue
        vectors = embed_fn([row[0]])
        if not vectors:
            continue
        store_vector(con, doc_id, "refresh", list(vectors[0]))
        mark_embedded(con, doc_id, now)
        count += 1
    return count


def embed(embed_url: str, text: str, timeout: float = 10.0) -> list[float] | None:
    """Ask an embeddings server. Return None when it is down. Never invent a vector."""
    url = embed_url.rstrip("/") + "/v1/embeddings"
    payload = json.dumps({"input": text, "model": "local"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
        return list(doc["data"][0]["embedding"])
    except Exception:
        return None
