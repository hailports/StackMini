"""BM25 plus vectors, fused with reciprocal rank, and an optional 2-hop graph."""

from __future__ import annotations

import json
import re
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


_EXACT_ID = re.compile(r"(?<![\w.])#?(\d{5,8})(?![\w.])")


def exact_ids(query: str) -> set[str]:
    """Ticket-style numbers named in the question (5-8 digits, optional #)."""
    return set(_EXACT_ID.findall(query or ""))


def flag_exact(query: str, rows: list[dict]) -> list[dict]:
    """Mark rows whose title carries an ID from the question. Returns the same list."""
    ids = exact_ids(query)
    if not ids:
        return rows
    for row in rows:
        title = str(row.get("title") or "")
        if any(re.search(rf"(?<!\d){i}(?!\d)", title) for i in ids):
            row["exact"] = True
    return rows


_MATCH_STOP = frozenset("""about after also and any are been before but can did does for from had has have how into
its just more not now our out over that the their them then there they this those was were what when where which
who why will with would you your week month year today yesterday please show tell give find""".split())
_MATCH_TOK = re.compile(r"[a-z0-9][a-z0-9_\-]{2,}")


def content_match(query: str, row: dict) -> bool:
    """True when at least half of the question's distinctive words (and at least two) are in the row."""
    toks = {t for t in _MATCH_TOK.findall((query or "").lower()) if t not in _MATCH_STOP and not t.isdigit()}
    if not toks:
        return False
    blob = f"{row.get('title') or ''} {row.get('body') or ''}".lower()
    return sum(1 for t in toks if t in blob) >= max(2, (len(toks) + 1) // 2)


def rrf(
    lists: list[list[dict]],
    limit: int,
    rrf_k: int = RRF_K,
    *,
    caps: dict[str, int] | None = None,
    query: str = "",
    summary_kinds: set[str] | frozenset[str] | None = None,
    summary_top3: int = 1,
) -> list[dict]:
    """Fuse ranked lists. A row that is high in either list can win. Both lists reinforce it.

    A row flagged ``exact`` (see flag_exact) gets one extra rank-1 vote and sorts ahead of the
    rest, so a question naming a ticket number returns that ticket first. ``caps`` limits how
    many rows of a given ``kind`` (for example summary docs) can fill one result set; exact
    rows are never capped.

    Rank rule as of 2026-11-20: the user's own notes still rank first, and an exact ticket-ID
    hit shares that top tier. A caller that keeps own notes in a top tier gives exact rows the
    same tier, not one above it.

    ``summary_kinds`` (2026-11-21) names row kinds that summarize other sources, such as master
    docs. Those rows sort below rows that match ``query`` on content (see content_match), and at most
    ``summary_top3`` of them may sit in the top 3, so summaries inform an answer without crowding out
    the direct hits. Exact rows keep the first tier either way.
    """
    scores: dict[int, float] = {}
    meta: dict[int, dict] = {}
    exact: set[int] = set()
    for listed in lists:
        for rank, item in enumerate(listed):
            key = int(item["id"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
            if item.get("exact"):
                exact.add(key)
            current = meta.get(key)
            if current is None or len(str(item.get("body") or "")) > len(str(current.get("body") or "")):
                meta[key] = dict(item)
    for key in exact:
        scores[key] += 1.0 / (rrf_k + 1)
    summary = set(summary_kinds or ())

    def is_summary(key: int) -> bool:
        return key not in exact and str(meta[key].get("kind") or "") in summary

    def tier(key: int) -> int:
        if key in exact:
            return 0
        if not summary:
            return 1
        if is_summary(key):
            return 2
        return 1 if content_match(query, meta[key]) else 3

    ordered = sorted(scores, key=lambda key: (tier(key), -scores[key]))
    used: dict[str, int] = {}
    out: list[dict] = []
    deferred: list[dict] = []
    for key in ordered:
        if len(out) >= limit:
            break
        row = dict(meta[key])
        kind = str(row.get("kind") or "")
        if caps and kind in caps and key not in exact:
            if used.get(kind, 0) >= caps[kind]:
                continue
            used[kind] = used.get(kind, 0) + 1
        row["score"] = round(scores[key], 6)
        row["via"] = "hybrid"
        if summary and is_summary(key) and len(out) < 3 and \
                sum(1 for r in out if r.get("_summary")) >= summary_top3:
            deferred.append(row)
            continue
        row["_summary"] = bool(summary) and is_summary(key)
        out.append(row)
        while deferred and 3 <= len(out) < limit:
            out.append(deferred.pop(0))
    while deferred and len(out) < limit:
        out.append(deferred.pop(0))
    for row in out:
        row.pop("_summary", None)
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
