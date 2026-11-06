"""Cited ask over the local notes file.

Blocked questions never touch retrieval. Hits that fail the data line are
removed and counted. Graph rows, when requested, fill at most two of the
slots and only when k is at least 4.
"""

from __future__ import annotations

import sqlite3
import time

from hailports.brain.dataline import hit_block, query_block
from hailports.brain.retrieve import DEFAULT_SCOPES, bm25_search, neighbors, rrf, vector_search


def ask(
    con: sqlite3.Connection,
    question: str,
    *,
    k: int = 8,
    scope: str = "all",
    graph: bool = False,
    scopes: dict | None = None,
    query_vector: list[float] | None = None,
) -> dict:
    started = time.perf_counter()
    reason = query_block(question)
    if reason:
        return {
            "ok": True,
            "blocked": "data_line",
            "reason": reason,
            "hits": [],
            "question": question,
            "scope": scope,
            "took_ms": int((time.perf_counter() - started) * 1000),
            "filtered": 0,
        }
    table = scopes or DEFAULT_SCOPES
    if scope not in table:
        raise ValueError(f"unknown scope {scope!r}")
    lexical = [row for row in bm25_search(con, question, limit=max(k * 4, 20)) if _scoped(con, row["id"], scope, table)]
    vectors = [row for row in vector_search(con, query_vector, limit=max(k * 4, 20)) if _scoped(con, row["id"], scope, table)]
    fused = rrf([lexical, vectors] if vectors else [lexical], limit=max(k * 2, k))
    filtered = 0
    clean = []
    for row in fused:
        if hit_block({
            "title": row.get("title"),
            "text": row.get("body"),
            "source": _meta_source(con, int(row["id"])),
        }):
            filtered += 1
            continue
        clean.append(row)
    graph_rows: list[dict] = []
    if graph and clean:
        extra = neighbors(con, [int(row["id"]) for row in clean], hops=2, limit=4)
        for row in extra:
            if hit_block({
                "title": row.get("title"),
                "text": row.get("body"),
                "source": _meta_source(con, int(row["id"])),
            }):
                filtered += 1
                continue
            if not _scoped(con, int(row["id"]), scope, table):
                continue
            graph_rows.append(row)
    grant = graph_rows[: min(2, k // 4)] if graph else []
    picked = clean[: max(0, k - len(grant))] + grant
    hits = []
    for index, row in enumerate(picked, 1):
        item = {
            "n": index,
            "source": _source(con, int(row["id"])),
            "title": row.get("title") or "",
            "text": row.get("body") or "",
            "link": f"note:{row['id']}",
            "via": row.get("via") or "hybrid",
            "score": row.get("score") or 0,
        }
        if item["via"] == "graph":
            item["graph_path"] = row.get("graph_path") or ""
        hits.append(item)
    return {
        "ok": True,
        "question": question,
        "scope": scope,
        "took_ms": int((time.perf_counter() - started) * 1000),
        "filtered": filtered,
        "hits": hits,
    }


def _scoped(con, doc_id: int, scope: str, scopes: dict) -> bool:
    from hailports.brain.retrieve import _in_scope
    return _in_scope(con, doc_id, scope, scopes)


def _meta_source(con, doc_id: int) -> str:
    try:
        row = con.execute("select source from doc_meta where doc_id = ?", (doc_id,)).fetchone()
    except Exception:
        return ""
    if not row or not row[0]:
        return ""
    return str(row[0])


def _source(con, doc_id: int) -> str:
    try:
        row = con.execute("select source, kind from doc_meta where doc_id = ?", (doc_id,)).fetchone()
    except Exception:
        return "note"
    if not row:
        return "note"
    return str(row[1] or row[0] or "note")
