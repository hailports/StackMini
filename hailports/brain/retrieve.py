"""BM25 plus vectors, fused with reciprocal rank, and an optional 2-hop graph."""

from __future__ import annotations

import json
import sqlite3

from hailports.notes import cosine, search as lexical_search

RRF_K = 60
DEFAULT_SCOPES = {
    "notes": ("note", "paste", "file"),
    "web": ("url",),
    "all": ("note", "paste", "file", "url"),
}


def ensure_graph(con: sqlite3.Connection) -> None:
    con.execute(
        """create table if not exists brain_edge (
            src integer not null,
            dst integer not null,
            kind text not null,
            primary key (src, dst, kind)
        )"""
    )


def link(con: sqlite3.Connection, src: int, dst: int, kind: str = "related") -> None:
    ensure_graph(con)
    con.execute(
        "insert or ignore into brain_edge (src, dst, kind) values (?, ?, ?)",
        (int(src), int(dst), kind),
    )
    con.commit()


def _kind(con: sqlite3.Connection, doc_id: int) -> str:
    try:
        row = con.execute("select kind from doc_meta where doc_id = ?", (doc_id,)).fetchone()
    except sqlite3.OperationalError:
        return "note"
    if not row or not row[0]:
        return "note"
    return str(row[0])


def _in_scope(con: sqlite3.Connection, doc_id: int, scope: str, scopes: dict | None) -> bool:
    table = scopes or DEFAULT_SCOPES
    allowed = table.get(scope)
    if allowed is None:
        raise ValueError(f"unknown scope {scope!r}")
    return _kind(con, doc_id) in allowed


def bm25_search(con: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    tokens = [token for token in query.replace('"', " ").split() if token.strip()]
    if not tokens:
        return []
    match = " OR ".join('"' + token.replace('"', "") + '"' for token in tokens[:8])
    try:
        rows = con.execute(
            "select rowid, title, body, bm25(docs_fts) as rank from docs_fts "
            "where docs_fts match ? order by rank limit ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return lexical_search(con, query, limit=limit)
    return [{"id": int(row[0]), "title": row[1], "body": row[2]} for row in rows]


def _vectors(con: sqlite3.Connection) -> dict[int, list[float]]:
    try:
        rows = con.execute("select doc_id, vector from vec").fetchall()
    except sqlite3.OperationalError:
        return {}
    found = {}
    for doc_id, blob in rows:
        try:
            found[int(doc_id)] = list(json.loads(blob))
        except (TypeError, json.JSONDecodeError):
            continue
    return found


def vector_search(con: sqlite3.Connection, query_vector: list[float] | None, limit: int = 20) -> list[dict]:
    if not query_vector:
        return []
    ranked = []
    for doc_id, vector in _vectors(con).items():
        score = cosine(query_vector, vector)
        if score <= 0:
            continue
        row = con.execute("select id, title, body from docs where id = ?", (doc_id,)).fetchone()
        if not row:
            continue
        ranked.append((score, {"id": int(row[0]), "title": row[1], "body": row[2]}))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in ranked[:limit]]


def rrf(lists: list[list[dict]], limit: int, rrf_k: int = RRF_K) -> list[dict]:
    """Fuse ranked lists. A row that is high in either list can win. Both lists reinforce it."""
    scores: dict[int, float] = {}
    meta: dict[int, dict] = {}
    for listed in lists:
        for rank, item in enumerate(listed):
            key = int(item["id"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
            current = meta.get(key)
            if current is None or len(str(item.get("body") or "")) > len(str(current.get("body") or "")):
                meta[key] = dict(item)
    ordered = sorted(scores, key=lambda key: -scores[key])
    out = []
    for key in ordered[:limit]:
        row = dict(meta[key])
        row["score"] = round(scores[key], 6)
        row["via"] = "hybrid"
        out.append(row)
    return out


def neighbors(con: sqlite3.Connection, seeds: list[int], hops: int = 2, limit: int = 4) -> list[dict]:
    """Walk undirected edges up to `hops` steps. The seed rows themselves are not returned."""
    ensure_graph(con)
    seen = set(seeds)
    frontier = list(seeds)
    found: list[tuple[int, str]] = []
    for _step in range(max(1, hops)):
        if not frontier:
            break
        nxt: list[int] = []
        for node in frontier:
            rows = con.execute(
                "select dst, kind from brain_edge where src = ? union select src, kind from brain_edge where dst = ?",
                (node, node),
            ).fetchall()
            for other, kind in rows:
                other = int(other)
                if other in seen:
                    continue
                seen.add(other)
                nxt.append(other)
                found.append((other, str(kind)))
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
        frontier = nxt
    out = []
    for doc_id, kind in found[:limit]:
        row = con.execute("select id, title, body from docs where id = ?", (doc_id,)).fetchone()
        if not row:
            continue
        out.append({
            "id": int(row[0]),
            "title": row[1],
            "body": row[2],
            "via": "graph",
            "graph_path": f"{kind}",
            "score": 0.0,
        })
    return out
