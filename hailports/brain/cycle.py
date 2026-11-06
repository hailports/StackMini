"""Ingest, embed, probe, publish, drop, lookup, and parity.

Each step is isolated. One failure is recorded and the cycle continues.
Publish refuses a manifest that contains a restricted path or a secret.
Nothing here sends mail or posts to a remote service.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hailports.brain.dataline import hit_block
from hailports.notes import reembed_stale

STEP_NAMES = ("ingest", "embed", "probe", "publish", "drop", "lookup", "parity")


def run_cycle(steps: dict) -> dict:
    results = {}
    for name in STEP_NAMES:
        fn = steps.get(name)
        if fn is None:
            results[name] = {"ok": True, "skipped": True}
            continue
        try:
            results[name] = {"ok": True, "result": fn(), "skipped": False}
        except Exception as exc:
            results[name] = {"ok": False, "error": type(exc).__name__, "skipped": False}
    results["ok"] = all(item.get("ok") for item in results.values())
    return results


def nightly_embed(con: sqlite3.Connection, embed_fn, now: float, max_age_s: float) -> int:
    return reembed_stale(con, embed_fn, now, max_age_s)


def publish_manifest(docs: list[dict], dest: Path) -> dict:
    refused = []
    clean = []
    for doc in docs:
        reason = hit_block(doc)
        if reason:
            refused.append({"reason": reason, "title": doc.get("title") or ""})
        else:
            clean.append({"title": doc.get("title") or "", "text": doc.get("text") or doc.get("body") or ""})
    if refused:
        return {"ok": False, "refused": refused, "wrote": False}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"docs": clean}), encoding="utf-8")
    return {"ok": True, "count": len(clean), "wrote": True, "refused": []}


def drop_doc(con: sqlite3.Connection, doc_id: int) -> bool:
    """Remove one note from the local index. This does not touch a remote system."""
    cur = con.execute("delete from docs where id = ?", (int(doc_id),))
    try:
        con.execute("delete from docs_fts where rowid = ?", (int(doc_id),))
    except sqlite3.OperationalError:
        pass
    for table, column in (("vec", "doc_id"), ("doc_meta", "doc_id"), ("brain_edge", "src")):
        try:
            con.execute(f"delete from {table} where {column} = ?", (int(doc_id),))
        except sqlite3.OperationalError:
            pass
    try:
        con.execute("delete from brain_edge where dst = ?", (int(doc_id),))
    except sqlite3.OperationalError:
        pass
    con.commit()
    return cur.rowcount > 0


def parity(left: int, right: int) -> dict:
    return {"ok": left == right, "left": left, "right": right}


def probe_health(fetch) -> dict:
    """`fetch` returns a status code or raises. The probe does not restart anything by itself."""
    try:
        status = int(fetch())
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": status == 200, "status": status}
