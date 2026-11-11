"""Write normalized items into the notes brain and remember sync cursors."""

from __future__ import annotations

import hashlib
import sqlite3
import time

from hailports.integrations.items import Item, apply_mask, needle_for
from hailports.notes import add, connect, search

SOURCE_PREFIX = "integration:"


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
    con.execute(
        """create table if not exists integration_doc (
            surface text not null,
            provider text not null,
            external_id text not null,
            doc_id integer not null,
            content_hash text,
            updated real,
            primary key (surface, provider, external_id)
        )"""
    )
    con.execute(
        """create table if not exists integration_cursor (
            surface text not null,
            provider text not null,
            cursor text,
            synced_at real,
            primary key (surface, provider)
        )"""
    )


def _source(item: Item) -> str:
    external = str(item.external_id or "").replace("\n", " ").strip()
    return f"{SOURCE_PREFIX}{item.surface}:{item.provider}:{external}"


def _hash(title: str, body: str) -> str:
    return hashlib.sha256((title + "\n" + body).encode("utf-8")).hexdigest()


def _replace_doc(con: sqlite3.Connection, doc_id: int, title: str, body: str) -> None:
    con.execute("update docs set title = ?, body = ? where id = ?", (title, body, doc_id))
    con.execute("delete from docs_fts where rowid = ?", (doc_id,))
    con.execute("insert into docs_fts (rowid, title, body) values (?, ?, ?)", (doc_id, title, body))


def index_items(con: sqlite3.Connection, items: list[Item], terms: list[str] | None = None, now: float | None = None) -> dict:
    """Insert or update one row per external id. Unchanged text is left alone."""
    _meta(con)
    stamp = time.time() if now is None else float(now)
    added = updated = skipped = dropped = 0
    needles: list[str] = []
    for item in items:
        source = _source(item)
        masked = apply_mask(item.title, item.body, source, list(terms or []))
        if masked is None:
            dropped += 1
            continue
        title, body = masked
        digest = _hash(title, body)
        row = con.execute(
            "select doc_id, content_hash from integration_doc where surface = ? and provider = ? and external_id = ?",
            (item.surface, item.provider, item.external_id),
        ).fetchone()
        when = float(item.when or stamp)
        if row and str(row[1] or "") == digest:
            skipped += 1
            continue
        if row:
            doc_id = int(row[0])
            _replace_doc(con, doc_id, title, body)
            updated += 1
        else:
            doc_id = add(con, title, body)
            added += 1
        con.execute(
            "insert or replace into doc_meta (doc_id, source, kind, content_hash, updated, embedded_at) values (?, ?, ?, ?, ?, ?)",
            (doc_id, source, item.kind_name(), digest, when, None),
        )
        con.execute(
            """insert or replace into integration_doc
               (surface, provider, external_id, doc_id, content_hash, updated)
               values (?, ?, ?, ?, ?, ?)""",
            (item.surface, item.provider, item.external_id, doc_id, digest, when),
        )
        word = needle_for(title, body)
        if word:
            needles.append(word)
    con.commit()
    return {"added": added, "updated": updated, "skipped": skipped, "dropped": dropped, "needles": needles}


def get_cursor(con: sqlite3.Connection, surface: str, provider: str) -> str:
    _meta(con)
    row = con.execute(
        "select cursor from integration_cursor where surface = ? and provider = ?",
        (surface, provider),
    ).fetchone()
    return str(row[0] or "") if row else ""


def set_cursor(con: sqlite3.Connection, surface: str, provider: str, cursor: str, now: float | None = None) -> None:
    _meta(con)
    con.execute(
        """insert or replace into integration_cursor (surface, provider, cursor, synced_at)
           values (?, ?, ?, ?)""",
        (surface, provider, cursor or "", time.time() if now is None else float(now)),
    )
    con.commit()


def prove(con: sqlite3.Connection, surface: str, needle: str) -> dict:
    if not needle:
        return {"surface": surface, "ok": False, "query": "", "reason": "nothing ingested"}
    hits = search(con, needle, limit=5)
    ok = any(needle.lower() in f"{hit.get('title') or ''} {hit.get('body') or ''}".lower() for hit in hits)
    return {"surface": surface, "ok": ok, "query": needle, "hits": len(hits), "reason": "" if ok else "not in the brain"}


def purge_surface(con: sqlite3.Connection, surface: str) -> int:
    """Delete documents this surface ingested. Other notes stay."""
    _meta(con)
    rows = con.execute("select doc_id from integration_doc where surface = ?", (surface,)).fetchall()
    count = 0
    for (doc_id,) in rows:
        doc_id = int(doc_id)
        con.execute("delete from docs_fts where rowid = ?", (doc_id,))
        con.execute("delete from docs where id = ?", (doc_id,))
        con.execute("delete from doc_meta where doc_id = ?", (doc_id,))
        try:
            con.execute("delete from vec where doc_id = ?", (doc_id,))
        except sqlite3.OperationalError:
            pass
        count += 1
    con.execute("delete from integration_doc where surface = ?", (surface,))
    con.execute("delete from integration_cursor where surface = ?", (surface,))
    con.commit()
    return count


def latest_text(con: sqlite3.Connection, surface: str) -> tuple[str, str]:
    _meta(con)
    row = con.execute(
        "select doc_id from integration_doc where surface = ? order by updated desc limit 1",
        (surface,),
    ).fetchone()
    if not row:
        return "", ""
    doc = con.execute("select title, body from docs where id = ?", (int(row[0]),)).fetchone()
    if not doc:
        return "", ""
    return str(doc[0] or ""), str(doc[1] or "")


def open_brain(path) -> sqlite3.Connection:
    return connect(path)
