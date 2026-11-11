#!/usr/bin/env python3
"""work_rag.py — WORK-LANE semantic + hybrid RAG so the box/GPT/clone stop being keyword-only.

The problem this closes: work-lane retrieval today is pure FTS (tools/work_context_index.py's
BM25 over email+meetings+SF+Monday+Zoom). Keyword-only means a reworded question ("am i free
friday") misses the chunk that answers it ("Friday afternoon is open, no meetings"), memory recall
is thin, and drafts get shallow context. This adds a SEMANTIC index (Ollama nomic-embed-text, local
$0) and HYBRID retrieval (FTS + vector, fused) so keyword-precise AND reworded hits both surface.

Pipeline (chunk -> embed -> sqlite -> cosine -> rerank) runs on the lane-neutral primitives in
core/text_index.py — embed()/generate()/pack()/chunk()/rerank() — rather than reimplement them.

HARD LANE FIREWALL: the HUSTLE lane has its own indexer and its own index
(data/hustle/rag/index.db). THIS is the WORK lane, a SEPARATE index at
data/learning/work_rag.sqlite (the path core/clone_health.py checks). The two indexes are NEVER
shared or mixed, and this module imports NOTHING from the hustle indexer — importing it here
welded the two lanes together and blocked cloning either one. No AI/stack trace in anything work-facing.

CORPUS = the already-aggregated work corpus in tools/work_context_index.py (SF tickets, Outlook
email, LittleBird/Zoom notes, Monday, digests) + core/valmont_memory memories. We pull text from
those stores; we do NOT re-scrape any source.

    core/../.venv/bin/python -m core.work_rag              # run the smoke proof (default)
    core/../.venv/bin/python -m core.work_rag index        # embed the real work corpus (incremental)
    core/../.venv/bin/python -m core.work_rag search "am i free friday"

LIVE — wired into the work gateway fallback, context assembler, meeting prep, anticipation,
and the lane-aware all-brain router. Callers fail soft to lexical retrieval if embeddings are
temporarily unavailable.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Lane-neutral pipeline primitives — do NOT reimplement, and do NOT reach into the hustle
# lane's indexer for them: importing it from the work lane breaks the lane firewall.
from core import text_index as _txt  # noqa: E402
from tools import work_context_index as wci  # noqa: E402
from tools import work_topic_digests as _topic_digests  # noqa: E402

WORK_RAG_DB = BASE_DIR / "data" / "learning" / "work_rag.sqlite"  # the path clone_health.py checks
INDEX_LOCK = BASE_DIR / "data" / "runtime" / "work_rag_refresh.lock"
EMBED_MODEL = _txt.EMBED_MODEL  # nomic-embed-text (local, $0)
TOPIC_DIGEST_DIR = _topic_digests.OUT_DIR
TOPIC_DIGEST_FILES = tuple(f"{topic}.md" for topic in _topic_digests.TOPICS)
TOPIC_DIGEST_REFRESH_MARKER = _topic_digests.REFRESH_MARKER

# Live callers include apps/valmont_brain/fallback.py, core/work_context_assembler.py,
# core/meeting_prep.py, core/anticipation_engine.py, and core/all_brain_rag.py.


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) if minimum is not None else value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) if minimum is not None else value


def _try_index_lock():
    """Acquire the one local writer slot without blocking another healthy refresh.

    The lock file carries owner metadata only for diagnostics/recovery; the kernel flock remains
    authoritative. If metadata names a dead owner, retry briefly so a just-dying descendant can
    release its inherited descriptor. Never unlink/replace a locked inode or steal from a live PID.
    """
    INDEX_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = INDEX_LOCK.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        owner = _read_lock_owner(handle)
        try:
            owner_pid = int(owner.get("pid") or 0)
        except (TypeError, ValueError):
            owner_pid = 0
        if owner_pid and not _pid_alive(owner_pid):
            attempts = _env_int("WORK_RAG_STALE_LOCK_RETRIES", 4, minimum=1)
            delay = _env_float("WORK_RAG_STALE_LOCK_RETRY_S", 0.25, minimum=0.0)
            for _ in range(attempts):
                if delay:
                    time.sleep(delay)
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    continue
                except OSError:
                    handle.close()
                    raise
            else:
                handle.close()
                return None
        else:
            handle.close()
            return None
    except OSError:
        handle.close()
        raise
    try:
        _write_lock_owner(handle)
    except Exception:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        raise
    return handle


def _read_lock_owner(handle) -> dict:
    try:
        handle.seek(0)
        data = json.loads(handle.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_lock_owner(handle) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump({"pid": os.getpid(), "started_at": time.time()}, handle)
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(proc, *, grace: float = 2.0) -> None:
    """Terminate a timed-out refresh and every descendant in its isolated session."""
    pgid = int(getattr(proc, "pid", 0) or 0)
    if pgid <= 0:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            proc.communicate(timeout=0.1)
        except Exception:
            pass
        return
    except OSError:
        pass
    # Do not reap the leader before the final group kill. Keeping its PID reserved prevents a
    # recycled process-group ID from receiving the SIGKILL intended for this isolated session.
    if grace > 0:
        time.sleep(grace)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass
    try:
        proc.communicate(timeout=max(1.0, grace))
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.communicate(timeout=1.0)
        except Exception:
            pass


def _run_refresh_command(command: list[str], *, timeout: float = 240.0):
    """Run one local refresh step in an isolated process group.

    `subprocess.run(timeout=...)` kills only the direct child. These steps can spawn OAuth/query
    grandchildren, so a timeout must tear down the whole group before the index lock is released.
    """
    proc = subprocess.Popen(
        command,
        cwd=str(BASE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(
            proc,
            grace=_env_float("WORK_RAG_REFRESH_TERM_GRACE_S", 2.0, minimum=0.0),
        )
        raise TimeoutError(
            f"refresh command timed out after {timeout:g}s; process group {proc.pid} terminated"
        ) from exc
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


# ─────────────────────────────────────────────────────────────────────── ollama availability
def _ollama_bin() -> str:
    """Resolve the ollama binary absolutely. Under launchd the job PATH is minimal (no
    /opt/homebrew/bin), so a bare `ollama` raises FileNotFoundError and the model reads as
    'missing' even though it's pulled — that false negative silently disabled the whole work RAG."""
    import shutil
    return shutil.which("ollama") or next(
        (p for p in ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama") if os.path.exists(p)),
        "ollama")


_LAST_EMBED_REASON = ""


def _embed_probe() -> tuple[bool, str]:
    """(available, reason). `ollama list` talks to the ollama server, so a server that is
    down/502ing returns nonzero exactly like a model that was never pulled. Reporting that as
    'NOT found ... ollama pull' sent an operator chasing a pull that had already happened
    (observed 8/20, mid 502 storm, with both models present). Absent and unreachable are
    different states and must print differently."""
    global _LAST_EMBED_REASON
    try:
        from core.local_llm_policy import embed_server_up
        if embed_server_up():
            _LAST_EMBED_REASON = ""
            return True, ""
    except Exception:
        pass
    attempts = _env_int("WORK_RAG_OLLAMA_CHECK_ATTEMPTS", 3, minimum=1)
    wanted = EMBED_MODEL.split(":")[0]
    reason = "ollama unreachable"
    for attempt in range(attempts):
        try:
            proc = subprocess.run(
                [_ollama_bin(), "list"], capture_output=True, text=True, timeout=10
            )
            if proc.returncode == 0:
                if wanted in proc.stdout:
                    _LAST_EMBED_REASON = ""
                    return True, ""
                reason = "model not pulled"
            else:
                reason = f"ollama list exit {proc.returncode}: {(proc.stderr or '').strip()[:120]}"
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:120]}"
        if attempt + 1 < attempts:
            time.sleep(0.75)
    _LAST_EMBED_REASON = reason
    return False, reason


def _embed_block_reason() -> str:
    """Machine label for the skip. 'embed_model_missing' is a claim about the model and must
    not be printed when the probe never reached ollama -- that reading cost an operator a
    wild-goose pull on 8/20."""
    try:
        from core.local_llm_policy import embed_server_up
        if embed_server_up():
            return ""
    except Exception:
        pass
    try:
        from core import local_llm_policy as _llp
        if not _llp.local_llm_enabled():
            return "local_llm_purged"
    except Exception:
        pass
    if _LAST_EMBED_REASON and _LAST_EMBED_REASON != "model not pulled":
        return "embed_probe_unreachable"
    return "embed_model_missing"


def embed_available() -> bool:
    """True iff local embeds are armed and the model is reachable. Default OFF (2026-08-27)."""
    try:
        from core.local_llm_policy import embed_server_up
        if embed_server_up():
            return True
    except Exception:
        pass
    try:
        from core import local_llm_policy as _llp
        if not _llp.local_llm_enabled():
            return False
    except Exception:
        return False
    return _embed_probe()[0]


def _require_embed() -> bool:
    try:
        from core.local_llm_policy import embed_server_up
        if embed_server_up():
            return True
    except Exception:
        pass
    try:
        from core import local_llm_policy as _llp
        if not _llp.local_llm_enabled():
            print("[work_rag] local LLM purged — semantic index skipped; lexical WCI still works.\n"
                  "           (cloud embeds for Valmont corpus are blocked by firewall)",
                  file=sys.stderr)
            return False
    except Exception:
        return False
    ok, reason = _embed_probe()
    if ok:
        return True
    if reason == "model not pulled":
        print(f"[work_rag] embedding model '{EMBED_MODEL}' NOT found (local LLM lane).\n"
              f"           local models were deleted 2026-08-27 — lexical retrieval only.",
              file=sys.stderr)
    else:
        print(f"[work_rag] cannot confirm embedding model '{EMBED_MODEL}': {reason}.",
              file=sys.stderr)
    return False


# ─────────────────────────────────────────────────────────────────────── store
_WAL_REPORTED = False


def _db(path: Path = WORK_RAG_DB):
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=15.0)
    con.execute("PRAGMA busy_timeout=15000")
    # WAL, same as work_context.db and work_graph.db (this store was the odd one out at
    # journal_mode=delete). The refresh job writes every 300s against a 2.2 GB file, and under a
    # rollback journal that writer and a live query connection lock each other out outright.
    # Two measured traps, hence the shape: (1) the switch needs a momentary EXCLUSIVE lock and
    # raises SQLITE_BUSY IMMEDIATELY (busy_timeout is not honored for it) whenever the refresh
    # writer holds a transaction -- and _vector_search calls _db() outside its own try, so an
    # unguarded pragma here would propagate out of search() to every caller; (2) once the switch
    # lands it is persistent in the file header, so the read below makes it a one-time cost.
    # A refused switch is reported once per process rather than silently leaving delete mode.
    global _WAL_REPORTED
    if str((con.execute("PRAGMA journal_mode").fetchone() or ("",))[0]).lower() != "wal":
        try:
            mode = (con.execute("PRAGMA journal_mode=WAL").fetchone() or ("",))[0]
        except Exception as e:
            mode = f"unchanged ({type(e).__name__}: {e})"
        if str(mode).lower() != "wal" and not _WAL_REPORTED:
            _WAL_REPORTED = True
            print(f"[work_rag] journal_mode still {mode!r} for {path}: WAL switch refused "
                  f"(writer holds the lock); retrying on the next open", file=sys.stderr)
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("CREATE TABLE IF NOT EXISTS vchunks (id INTEGER PRIMARY KEY, source TEXT, ref TEXT, "
                "sub INT, title TEXT, url TEXT, ts REAL, text TEXT, emb BLOB)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_vchunks_ref ON vchunks(source, ref)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_vchunks_source_ts ON vchunks(source, ts DESC)")
    # incremental watermark: content hash per (source, ref) parent doc
    con.execute("CREATE TABLE IF NOT EXISTS seen (source TEXT, ref TEXT, hash TEXT, "
                "PRIMARY KEY(source, ref))")
    return con


# ─────────────────────────────────────────────────────────────────────── corpus (pull, never scrape)
def _iter_corpus():
    """Yield (source, ref, title, url, ts, text) from the already-aggregated work stores:
    tools.work_context_index's `docs` FTS table + core.valmont_memory memories. No re-scraping."""
    import sqlite3
    # 1) work_context_index docs (email/meetings/SF/Monday/Zoom/digests)
    if wci.DB_PATH.exists():
        c = sqlite3.connect(str(wci.DB_PATH))
        c.row_factory = sqlite3.Row
        try:
            for r in c.execute("SELECT source, ref, ts, url, title, participants, body FROM docs ORDER BY ts DESC"):
                if r["source"] == "digest" and not wci.is_work_digest_ref(r["ref"]):
                    continue
                # 2026-10-19 spfix: SharePoint rows are mirrored INTO FTS from vchunks for keyword
                # search; their vectors are owned by llm-appliance/cli/sp_corpus.py (+vchunks_vec).
                # Re-embedding them here would orphan the ANN index rows.
                if r["source"] in SP_BACKING_SOURCES:
                    continue
                body = (r["body"] or "").strip()
                # title + participants give the embedder useful context beyond the raw chunk body.
                # A mail row with an empty body still has a subject and people; that is enough to find it.
                head = " · ".join(x for x in (r["title"], r["participants"]) if x)
                if not body and not head:
                    continue
                text = f"{head}\n{body}" if head and body else (head or body)
                yield (r["source"], r["ref"], r["title"] or r["ref"], r["url"] or "",
                       float(r["ts"] or 0), text)
        except Exception as e:
            print(f"[work_rag] work_context_index read failed: {e}", file=sys.stderr)
        finally:
            c.close()
    # 2) valmont_memory durable memories
    try:
        from core import valmont_memory
        for m in valmont_memory.recent(limit=10000):
            body = (m.get("body") or "").strip()
            if not body:
                continue
            text = f"{m.get('title','')}\n{body}".strip()
            yield ("memory", f"mem_{m['id']}", m.get("title") or f"memory {m['id']}",
                   f"memory:{m['id']}", 0.0, text)
    except Exception as e:
        print(f"[work_rag] valmont_memory read failed: {e}", file=sys.stderr)


def _prune_isolated_vectors(con) -> dict:
    """Remove vectors that no longer belong to the fenced work corpus.

    The semantic store has its own incremental watermark, so deleting an unsafe
    source from FTS is not enough: its old vectors otherwise remain searchable.
    Pruning is source-scoped and fail-soft; a temporarily unreadable authority
    never causes broad deletion.
    """
    digest_refs: set[str] | None = None
    memory_refs: set[str] | None = None

    try:
        import sqlite3
        if wci.DB_PATH.exists():
            c = sqlite3.connect(wci.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                digest_refs = {
                    str(row[0]) for row in c.execute(
                        "SELECT ref FROM seen WHERE source='digest'"
                    ).fetchall()
                    if wci.is_work_digest_ref(str(row[0]))
                }
            finally:
                c.close()
    except Exception:
        digest_refs = None

    try:
        from core import valmont_memory
        memory_refs = {
            f"mem_{m['id']}" for m in valmont_memory.recent(limit=10000)
        }
    except Exception:
        memory_refs = None

    removed = {"digest": 0, "memory": 0}
    for source, allowed in (("digest", digest_refs), ("memory", memory_refs)):
        rows = con.execute("SELECT ref FROM seen WHERE source=?", (source,)).fetchall()
        for (ref,) in rows:
            # The filename allowlist remains authoritative even when the FTS store
            # is unavailable. Other stale cleanup waits for a complete authority read.
            disallowed_digest = source == "digest" and not wci.is_work_digest_ref(ref)
            if not disallowed_digest and (allowed is None or ref in allowed):
                continue
            con.execute("DELETE FROM vchunks WHERE source=? AND ref=?", (source, ref))
            con.execute("DELETE FROM seen WHERE source=? AND ref=?", (source, ref))
            removed[source] += 1

    # GitHub feeds are exact-source managed. Once a commit, PR subrecord, source
    # chunk, or transient staged item is pruned from lexical authority it must not
    # survive as a stale semantic hit. Read failure remains fail-closed against
    # deletion: only a successfully opened WCI database can authorize this pass.
    github_refs: dict[str, set[str]] | None = None
    try:
        import sqlite3
        if wci.DB_PATH.exists():
            wc = sqlite3.connect(wci.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                github_refs = {source: set() for source in wci.GITHUB_WORK_SOURCES}
                for source, ref in wc.execute(
                    "SELECT source, ref FROM seen WHERE source IN (%s)"
                    % ",".join("?" for _ in wci.GITHUB_WORK_SOURCES),
                    tuple(sorted(wci.GITHUB_WORK_SOURCES)),
                ).fetchall():
                    github_refs[str(source)].add(str(ref))
            finally:
                wc.close()
    except Exception:
        github_refs = None
    if github_refs is not None:
        for source in sorted(wci.GITHUB_WORK_SOURCES):
            rows = con.execute("SELECT ref FROM seen WHERE source=?", (source,)).fetchall()
            if not rows:
                continue
            count = 0
            allowed = github_refs[source]
            for (ref,) in rows:
                if ref in allowed:
                    continue
                con.execute("DELETE FROM vchunks WHERE source=? AND ref=?", (source, ref))
                con.execute("DELETE FROM seen WHERE source=? AND ref=?", (source, ref))
                count += 1
            removed[source] = count
    con.commit()
    return removed


# ─────────────────────────────────────────────────────────────────────── indexing
def _vec_writer(con) -> bool:
    """2026-10-20 graph: vectors embedded here were never added to vchunks_vec (only sp_corpus and
    reembed_vchunks wrote it), so the ANN path -- which wins whenever it returns anything -- could not
    see any chunk embedded after the 10/18 re-embed (sf_env, new mail, digests). Load sqlite-vec on this
    connection when the interpreter has it; otherwise behave exactly as before (vchunks only)."""
    try:
        import sqlite_vec
        con.enable_load_extension(True); sqlite_vec.load(con); con.enable_load_extension(False)
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='vchunks_vec'").fetchone())
    except Exception:
        return False


def _index_docs(docs, con, *, progress_every: int = 200) -> dict:
    """Embed an iterable of (source, ref, title, url, ts, text) into `con` incrementally.
    Incremental via content hash watermark: unchanged (source,ref) is skipped, changed is re-embedded."""
    added = skipped = chunks = 0
    vec = _vec_writer(con)  # 2026-10-20 graph: keep the sqlite-vec ANN index in step with vchunks
    max_docs = int(os.environ.get("WORK_RAG_MAX_DOCS", "80"))
    max_chunks = int(os.environ.get("WORK_RAG_MAX_CHUNKS", "240"))
    for i, (source, ref, title, url, ts, text) in enumerate(docs):
        h = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
        row = con.execute("SELECT hash FROM seen WHERE source=? AND ref=?", (source, ref)).fetchone()
        if row and row[0] == h:
            skipped += 1
            continue
        if vec:
            for (rid,) in con.execute("SELECT id FROM vchunks WHERE source=? AND ref=?", (source, ref)).fetchall():
                con.execute("DELETE FROM vchunks_vec WHERE rowid=?", (rid,))
        con.execute("DELETE FROM vchunks WHERE source=? AND ref=?", (source, ref))
        pieces = _txt.chunk(text)
        n = 0
        for sub, piece in enumerate(pieces):
            emb = _txt.embed(f"{title}\n{piece}")  # shared Ollama embedder
            if not emb:
                continue
            cur = con.execute("INSERT INTO vchunks(source, ref, sub, title, url, ts, text, emb) "
                              "VALUES (?,?,?,?,?,?,?,?)",
                              (source, ref, sub, title, url, ts, piece, _txt.pack(emb)))
            if vec and len(emb) == 384:
                con.execute("INSERT INTO vchunks_vec(rowid, embedding) VALUES (?,?)", (cur.lastrowid, _txt.pack(emb)))
            n += 1
        if n > 0:
            # only watermark a doc once at least one chunk actually embedded — else a transient
            # per-doc embed failure would orphan it from the index forever (retry next run instead)
            con.execute("INSERT OR REPLACE INTO seen(source, ref, hash) VALUES (?,?,?)", (source, ref, h))
        added += 1
        chunks += n
        if added >= max_docs or chunks >= max_chunks:
            con.commit()
            print(f"  [work_rag] cap docs={max_docs} chunks={max_chunks} added={added} chunks_done={chunks}")
            break
        if progress_every and added % progress_every == 0:
            con.commit()
            print(f"  [work_rag] {added} docs embedded ({chunks} chunks), {skipped} unchanged...")
    con.commit()
    return {"docs_embedded": added, "unchanged": skipped, "chunks_added": chunks}


def _refresh_project_rags() -> dict:
    try:
        from core import work_project_rag
        if not WORK_RAG_DB.exists():
            _db().close()
        # Corporate OneDrive is never an implicit side effect of a local index refresh.
        # Export remains an explicit, separately approved opt-in for the existing publisher.
        export_enabled = os.environ.get("WORK_PROJECT_RAG_ONEDRIVE", "0").strip().lower() \
            not in {"0", "false", "no", "off"}
        return work_project_rag.refresh(
            index_db=WORK_RAG_DB,
            onedrive_root=None if export_enabled else False,
        )
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:160]}


def _refresh_topic_digests() -> dict:
    """Refresh local topic digests and ingest them into lexical context before embedding.

    This is read-only against corporate sources and writes only local workbrain stores. A
    failed source refresh leaves the previous semantic index usable and is reported plainly.
    """
    digest_script = BASE_DIR / "tools" / "work_topic_digests.py"
    figma_script = BASE_DIR / "tools" / "figma_workbrain_sync.py"
    index_script = BASE_DIR / "tools" / "work_context_index.py"
    results: dict[str, dict] = {}
    if _topic_digests_fresh():
        results["figma"] = {"ok": True, "skipped": True, "reason": "topic digests fresh"}
        results["generate"] = {"ok": True, "skipped": True, "reason": "topic digests fresh"}
        refresh_steps = ()
    else:
        refresh_steps = (
            ("figma", [sys.executable, str(figma_script), "sync", "--if-configured"]),
            ("generate", [sys.executable, str(digest_script)]),
        )

    for name, command in refresh_steps:
        try:
            proc = _run_refresh_command(command, timeout=240)
            results[name] = {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "detail": (proc.stdout or proc.stderr or "").strip()[-500:],
            }
        except Exception as exc:
            results[name] = {"ok": False, "reason": str(exc)[:160]}
        if not results[name].get("ok"):
            if name == "figma":
                results["generate"] = {
                    "ok": False,
                    "skipped": True,
                    "reason": "figma refresh failed",
                }
            break

    # Ingest is independent of source refresh. Even if Figma or digest
    # generation fails, the existing validated digest set still gets indexed
    # and its liveness watermark advances truthfully.
    try:
        command = [sys.executable, str(index_script), "ingest", "digests"]
        proc = _run_refresh_command(command, timeout=240)
        results["ingest"] = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "detail": (proc.stdout or proc.stderr or "").strip()[-500:],
        }
    except Exception as exc:
        results["ingest"] = {"ok": False, "reason": str(exc)[:160]}
    return {"ok": all(item.get("ok") for item in results.values()), "steps": results}


def _topic_digests_fresh(now: float | None = None) -> bool:
    """Avoid rebuilding five source-heavy digests inside every five-minute vector refresh."""
    ttl = _env_int("WORK_RAG_TOPIC_DIGEST_TTL", 600, minimum=0)
    if ttl == 0:
        return False
    if TOPIC_DIGEST_REFRESH_MARKER.exists():
        return False
    current = time.time() if now is None else now
    for filename in TOPIC_DIGEST_FILES:
        path = TOPIC_DIGEST_DIR / filename
        try:
            if current - path.stat().st_mtime > ttl:
                return False
            text = path.read_text(encoding="utf-8", errors="ignore")
            if (
                len(text) < 80
                or "AUTO-REFRESHED" not in text[:500]
                or "_build error:" in text
            ):
                return False
        except OSError:
            return False
    return True


def index(incremental: bool = True) -> dict:
    """Embed the work corpus into data/learning/work_rag.sqlite. Verifies nomic-embed-text is
    available FIRST (prints the pull command + exits cleanly if not). incremental=False rebuilds."""
    if not _require_embed():
        project_stats = _refresh_project_rags()
        return {"ok": False, "semantic": False,
                "reason": _embed_block_reason(), "project_rags": project_stats}
    con = _db()
    if not incremental:
        con.execute("DELETE FROM vchunks")
        con.execute("DELETE FROM seen")
        con.commit()
    t0 = time.time()
    stats = _index_docs(_iter_corpus(), con)
    stats["pruned"] = _prune_isolated_vectors(con)
    total = con.execute("SELECT COUNT(*) FROM vchunks").fetchone()[0]
    con.close()
    project_stats = _refresh_project_rags()
    stats.update({"ok": True, "total_chunks": total, "secs": round(time.time() - t0, 1),
                  "db": str(WORK_RAG_DB), "project_rags": project_stats})
    print(f"[work_rag] index done: {stats}")
    return stats


def index_sources(sources: set[str] | list[str] | tuple[str, ...], *,
                  refresh_projects: bool = True) -> dict:
    """Incrementally embed an exact source slice already present in WCI.

    Local Git hooks use this for `git_staged`, so a staged/committed change becomes
    semantic context without forcing a whole-corpus refresh. Callers must hold the
    normal writer lock; project manifests are rebuilt locally with OneDrive export off.
    """
    wanted = {str(source) for source in sources if str(source)}
    if not wanted:
        return {"ok": True, "sources": [], "docs_embedded": 0, "chunks_added": 0}
    if not _require_embed():
        return {"ok": False, "semantic": False, "reason": _embed_block_reason(),
                "sources": sorted(wanted)}
    con = _db()
    t0 = time.time()
    stats = _index_docs(
        (doc for doc in _iter_corpus() if doc[0] in wanted), con, progress_every=0
    )
    stats["pruned"] = _prune_isolated_vectors(con)
    stats["total_chunks"] = con.execute("SELECT COUNT(*) FROM vchunks").fetchone()[0]
    con.close()
    project_stats = _refresh_project_rags() if refresh_projects else {"ok": True, "skipped": True}
    stats.update({"ok": True, "sources": sorted(wanted),
                  "secs": round(time.time() - t0, 2), "project_rags": project_stats})
    return stats


# ─────────────────────────────────────────────────────────────────────── retrieval

def _vec_index_search(qv, k: int, db_path: Path, sources=None) -> list[dict]:
    """2026-10-18: sqlite-vec ANN over vchunks_vec (bge-small 384-dim, :8097). Returns [] to fall
    back to the legacy scan when the extension/table is missing or dims mismatch."""
    if len(qv) != 384:
        return []
    try:
        import array, sqlite3, sqlite_vec
        con = sqlite3.connect(str(db_path), timeout=5.0)
        con.enable_load_extension(True); sqlite_vec.load(con); con.enable_load_extension(False)
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='vchunks_vec'").fetchone():
            con.close(); return []
        # sqlite-vec caps knn k at 4096; a scoped assembler call (k=120 -> 3000+) used to raise here
        # and silently drop to the 8k-row legacy scan (2026-10-19 spfix).
        want = min(4096, max(k * (25 if sources else 6), 50))
        q = array.array("f", qv).tobytes()
        hits = con.execute(
            "SELECT v.rowid, v.distance, c.source, c.ref, c.title, c.url, c.ts, c.text "
            "FROM (SELECT rowid, distance FROM vchunks_vec WHERE embedding MATCH ? AND k = ?) v "
            "JOIN vchunks c ON c.id = v.rowid ORDER BY v.distance", (q, want)).fetchall()
        con.close()
    except Exception as e:  # noqa: BLE001
        print(f"[work_rag] vec index unavailable ({type(e).__name__}: {e}); legacy scan", file=sys.stderr)
        return []
    out, seen = [], set()
    srcs = set(sources) if sources else None
    for _rid, dist, source, ref, title, url, ts, text in hits:
        if srcs and source not in srcs:
            continue
        if (source, ref) in seen:
            continue
        seen.add((source, ref))
        out.append({"source": source, "source_label": wci.source_label(source), "ref": ref, "title": title, "url": url, "ts": ts,
                    "text": text, "score": round(1.0 - float(dist) ** 2 / 2.0, 4)})
        if len(out) >= k:
            break
    return out


def _vector_search(query: str, k: int, db_path: Path = WORK_RAG_DB,
                   sources: list[str] | None = None) -> list[dict]:
    """Cosine top-k over the work vector index, deduped to the best chunk per (source, ref) so the
    fusion key aligns with FTS docs. Uses text_index's embedder + numpy cosine (pure-py fallback).

    `sources` restricts the vector query to those chunk sources (e.g. ['memory']) so a scoped caller
    gets top-k WITHIN that slice instead of a global ranking the 28k email chunks dominate."""
    if not db_path.exists():
        return []
    try:
        qv = _txt.embed(str(query or "")[:8000])
    except Exception:
        qv = None
    if not qv:
        return []
    ann = _vec_index_search(qv, k, db_path, sources)
    if ann:
        return ann
    con = _db(db_path)
    max_rows = _env_int("WORK_RAG_VECTOR_MAX_ROWS", 8000, minimum=500)
    deadline = time.monotonic() + _env_float("WORK_RAG_SQL_DEADLINE", 1.2, minimum=0.2)
    con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
    rows = []
    try:
        selected_sources = list(dict.fromkeys(sources or [r[0] for r in con.execute(
            "SELECT DISTINCT source FROM vchunks"
        ).fetchall()]))
        per_source = max(100, max_rows // max(1, len(selected_sources)))
        for source in selected_sources:
            rows.extend(con.execute(
                "SELECT source, ref, title, url, ts, text, emb FROM vchunks "
                "WHERE source=? ORDER BY ts DESC LIMIT ?",
                (source, per_source),
            ).fetchall())
    except Exception as e:
        # A dead vector channel used to be indistinguishable from an empty one: a tripped
        # WORK_RAG_SQL_DEADLINE or SQLITE_BUSY behind the 300s refresh writer set rows=[] with no
        # log line, so search() quietly returned FTS-only output that reads like a healthy answer.
        # Same stderr shape as _fts_search below; still fails soft to the lexical half.
        print(f"[work_rag] vector search failed ({type(e).__name__}: {e}); "
              f"this query degrades to FTS-only", file=sys.stderr)
        rows = []
    finally:
        con.set_progress_handler(None, 0)
        con.close()
    if not rows:
        return []
    scored = []
    try:
        import numpy as np
        q = np.asarray(qv, dtype="float32")
        q /= (np.linalg.norm(q) + 1e-9)
        for source, ref, title, url, ts, text, emb in rows:
            v = np.frombuffer(emb, dtype="float32")
            if v.shape[0] != q.shape[0]:
                continue
            s = float(np.dot(q, v) / (np.linalg.norm(v) + 1e-9))
            scored.append((s, source, ref, title, url, ts, text))
    except ImportError:
        import array
        import math
        qn = math.sqrt(sum(x * x for x in qv)) + 1e-9
        qu = [x / qn for x in qv]
        for source, ref, title, url, ts, text, emb in rows:
            if len(emb) != len(qu) * 4:
                continue
            v = array.array("f")
            v.frombytes(emb)
            dot = vn = 0.0
            for a, b in zip(qu, v):
                dot += a * b
                vn += b * b
            scored.append((dot / (math.sqrt(vn) + 1e-9), source, ref, title, url, ts, text))
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for s, source, ref, title, url, ts, text in scored:
        key = (source, ref)
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "source": source, "source_label": wci.source_label(source),
                    "ref": ref, "title": title, "url": url, "ts": ts, "score": round(s, 4)})
        if len(out) >= k:
            break
    return out


# stopwords stripped before the FTS channel: work_context_index ORs every len>=2 token, so bare
# "the"/"is"/"what" match half the corpus and drown real hits in the fusion. Dropping them is a
# free precision win and isolates what the SEMANTIC channel actually adds.
_STOP = frozenset("a am an and are as at be been but by can did do does for from had has have how "
                  "i in is it its me my no not of on or our so that the their them then there these "
                  "they this to was we were what when where which who whom will with would you your "
                  "yet".split())


def _destop(query: str) -> str:
    kept = [w for w in (query or "").split() if w.lower().strip(".,!?'\"") not in _STOP]
    return " ".join(kept) if kept else (query or "")


_MONTH_NUM = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_SPOKEN_DATE = __import__("re").compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)\b"
    r"|\b(\d{1,2})/(\d{1,2})/(20\d\d)\b", __import__("re").I)


def _iso_dates(query: str) -> list[str]:
    """'October 19, 2026' / '10/19/2026' -> '2026-10-19'. wci turns that into the exact phrase
    "2026 10 19", so a dated file (NEO Agenda 2026-10-19) wins on the date instead of drowning in
    the OR of 'october' / '19' / '2026' that every agenda shares (2026-10-19 spfix)."""
    out = []
    for m in _SPOKEN_DATE.finditer(query or ""):
        try:
            if m.group(1):
                y, mo, d = int(m.group(3)), _MONTH_NUM[m.group(1).lower()[:3]], int(m.group(2))
            else:
                y, mo, d = int(m.group(6)), int(m.group(4)), int(m.group(5))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                iso = f"{y:04d}-{mo:02d}-{d:02d}"
                if iso not in out:
                    out.append(iso)
        except (KeyError, ValueError):
            continue
    return out


def _fts_search(query: str, k: int, sources=None) -> list[dict]:
    """The existing keyword path — tools.work_context_index.search (BM25), with stopwords stripped so
    pure-noise tokens can't pollute the fusion. Normalized to the fusion dict shape."""
    out = []
    try:
        fts_query = " ".join([*_iso_dates(query), _destop(query)])
        for r in wci.search(fts_query, limit=k, sources=sources):
            out.append({"text": r.get("body") or r.get("snippet") or r.get("title") or "", "source": r.get("source"),
                        "source_label": r.get("source_label") or wci.source_label(r.get("source")),
                        "ref": r.get("ref"), "title": r.get("title") or r.get("ref"),
                        "url": r.get("url") or "", "ts": r.get("ts") or 0, "score": None})
    except Exception as e:
        print(f"[work_rag] fts search failed: {e}", file=sys.stderr)
    return out


OWN_SOURCES = frozenset({"memory", "session_insight", "work_vault", "playbooks", "findings", "digest",
                         "customer_domain_master_doc", "sfdc_master_doc", "sp_portal_master_workbook",
                         "sfdc_kb", "docs", "topic_brief", "alex_note"})
SP_BACKING_SOURCES = frozenset({"sharepoint", "sp_corpus"})
SP_BACKING_WEIGHT = 0.5


# 2026-10-19 spfix: hand the model the top 2-3 matching passages of a doc instead of only the
# single best chunk (the vector channel dedupes to one chunk per (source, ref)). Applies to
# SharePoint hits always, and to any other hit whose text is a single vector chunk.
CHUNKS_PER_DOC = _env_int("WORK_RAG_CHUNKS_PER_DOC", 3, minimum=1)
_PASSAGE_SEP = "\n…\n"
_PSTOP = frozenset("about after also and are been but can could does for from has have how into its "
                   "not only that the their them then there these they this what when where which "
                   "who whom will with would your valmont".split())


def _ptoks(text: str) -> set[str]:
    import re as _re
    out = set()
    for w in _re.findall(r"[a-z0-9][a-z0-9'-]{2,}", str(text or "").lower()):
        w = w.strip("'-")
        if len(w) < 3 or w in _PSTOP:
            continue
        for suf in ("'s", "ly", "es", "s", "ed", "ing"):
            if len(w) > 4 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


def _attach_passages(hits: list[dict], query: str, db_path: Path = WORK_RAG_DB) -> list[dict]:
    if CHUNKS_PER_DOC <= 1 or not hits or not db_path.exists():
        return hits
    targets = [h for h in hits if h.get("source") in SP_BACKING_SOURCES
               or len(str(h.get("text") or "")) <= 1200]
    if not targets:
        return hits
    try:
        qv = _txt.embed(str(query or "")[:8000]) or None
    except Exception:
        qv = None
    qtok = _ptoks(query)
    try:
        import array, math, sqlite3
        con = sqlite3.connect(str(db_path), timeout=5.0)
    except Exception:
        return hits
    try:
        qn = math.sqrt(sum(x * x for x in qv)) + 1e-9 if qv else 0.0
        for h in targets:
            rows = con.execute(
                "SELECT sub, text, emb FROM vchunks WHERE source=? AND ref=? ORDER BY sub LIMIT 60",
                (h.get("source"), h.get("ref"))).fetchall()
            if len(rows) < 2:
                continue
            scored = []
            for sub, text, emb in rows:
                s = 0.0
                if qv and emb and len(emb) == len(qv) * 4:
                    v = array.array("f"); v.frombytes(emb)
                    dot = sum(a * b for a, b in zip(qv, v))
                    s = dot / (qn * (math.sqrt(sum(b * b for b in v)) + 1e-9))
                if qtok:
                    s += 0.15 * len(qtok & _ptoks(text)) / len(qtok)
                scored.append((s, int(sub or 0), str(text or "")))
            top = sorted(scored, key=lambda r: -r[0])[:CHUNKS_PER_DOC]
            top.sort(key=lambda r: r[1])
            h["text"] = _PASSAGE_SEP.join(t.strip() for _, _, t in top if t.strip())
            h["passages"] = len(top)
    except Exception as e:  # noqa: BLE001 -- passages are polish; never break retrieval
        print(f"[work_rag] passage attach failed ({type(e).__name__}: {e})", file=sys.stderr)
    finally:
        con.close()
    return hits


def _rrf(lists: list[list[dict]], k: int, rrf_k: int = 60) -> list[dict]:
    """Reciprocal-rank fusion: fuse ranked lists so a chunk high in EITHER FTS or vector wins.
    Keyed on (source, ref) so the same doc from both paths reinforces instead of duplicating."""
    scores: dict = {}
    meta: dict = {}
    for lst in lists:
        for rank, item in enumerate(lst):
            key = (item["source"], item["ref"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
            # prefer the richer full-chunk text (vector) over the FTS snippet when merging
            cur = meta.get(key)
            if cur is None or (len(item.get("text") or "") > len(cur.get("text") or "")):
                merged = dict(item)
                if cur:  # keep any field the other path filled
                    for fld in ("text", "url", "ts", "title"):
                        if not merged.get(fld) and cur.get(fld):
                            merged[fld] = cur[fld]
                meta[key] = merged
            meta[key]["ref"] = item["ref"]
    # Age the fused score, not just the keyword channel. Semantic similarity has no sense of
    # time at all, so without this a months-old chunk that merely reads like the question can
    # ride RRF to the top and become the "context" a draft is built on.
    for key in scores:
        scores[key] *= wci.recency_weight((meta.get(key) or {}).get("ts"))
        if key[0] in SP_BACKING_SOURCES:
            scores[key] *= SP_BACKING_WEIGHT
    # 2026-10-18 RANK RULE (Alex): his own generated/verified context always ranks first;
    # bulk SharePoint corpus is a lower-priority backing layer and can never outrank it.
    ranked = sorted(scores.items(), key=lambda kv: (0 if kv[0][0] in OWN_SOURCES else 1, -kv[1]))
    out = []
    for key, sc in ranked[:k]:
        d = dict(meta[key])
        d["score"] = round(sc, 5)
        out.append({"text": d.get("text", ""), "source": d.get("source"),
                    "source_label": d.get("source_label") or wci.source_label(d.get("source")),
                    "ref": d.get("ref"),
                    "title": d.get("title"), "url": d.get("url"), "ts": d.get("ts") or 0,
                    "score": d["score"]})
    return out


def search(query: str, k: int = 8, *, db_path: Path = WORK_RAG_DB, rerank: bool = False,
           sources: list[str] | None = None) -> list[dict]:
    """HYBRID work-lane retrieval -> [{text, source, ref, score}]. Runs BOTH the existing FTS
    (tools.work_context_index.search) AND vector cosine over work_rag.sqlite, then RRF-merges so
    keyword-precise AND reworded/semantic hits both surface. This is the whole point: semantic
    catches what FTS misses. rerank=True adds an LLM relevance pass (text_index.rerank) on the fusion.

    `sources` scopes the SEMANTIC (vector) channel to those chunk sources — e.g. sources=['memory']
    returns memory-only semantic hits so durable memories aren't buried under the ~28k email chunks
    in a global top-k. When scoped, the FTS channel is skipped: tools.work_context_index is a DISJOINT
    corpus (no memory rows, no per-source filter), so fusing it would drag in out-of-scope docs.

    Degrades gracefully: if the vector index is empty / Ollama is unreachable, vector returns []
    and this is just FTS (never worse than the current keyword-only path)."""
    # A scoped caller used to skip keyword search. The assembler always passes
    # the corp allowlist, so one old vector hit hid newer notes that only the
    # keyword index had. Fuse both. An empty keyword slice stays empty.
    fts = _fts_search(query, max(k * 4, 40), sources=sources)
    vec = _vector_search(query, max(k * 4, 40), db_path=db_path, sources=sources)
    lists = [fts, vec]
    # 2026-10-19 spfix: an explicit date in the question is an exact identifier. Docs whose title/ref
    # carry that date form a third RRF list (one extra rank-1 vote), so "the Oct 19 2026 agenda"
    # beats 300 near-identical agendas the embedding cannot tell apart. Source weights
    # (SP_BACKING_WEIGHT) and the own-first rule still apply to the fused score unchanged.
    iso = _iso_dates(query)
    if iso:
        exact = [h for h in fts if any(d in f"{h.get('ref') or ''} {h.get('title') or ''}" for d in iso)]
        if exact:
            lists.append(exact)
    fused = _rrf(lists, k=max(k, k * 3 if rerank else k))
    if rerank and fused:
        cands = [(h.get("score") or 0.0, f"{h['source']}:{h['ref']}", h.get("text") or "")
                 for h in fused]
        reranked = _txt.rerank(query, cands, keep=k)
        keymap = {f"{h['source']}:{h['ref']}": h for h in fused}
        out = [dict(keymap[key], score=round(s, 3)) for s, key, _ in reranked if key in keymap][:k]
        return _attach_passages(out, query, db_path)
    return _attach_passages(fused[:k], query, db_path)


# ─────────────────────────────────────────────────────────────────────── smoke proof
# (source, ref, title, participants, body) — controlled WORK-lane docs; NO ai/stack trace.
# GOLD bodies are deliberately worded so the reworded PROBE below shares NO content word with them
# (that's the hard case: pure FTS structurally cannot match; only semantics bridges the wording gap).
_GOLD = [
    ("zoom", "cal_0715", "Weekly schedule", "ops",
     "the back half of the week stays wide open, nothing booked past 11am on the 17th, "
     "only midweek DPP reviews on the calendar"),
    ("digest", "tavant_cov", "Sprint staffing", "delivery",
     "Tavant India resources hit a shortfall Tuesday and Wednesday next sprint, "
     "onshore backup needed to keep tickets moving"),
    ("salesforce", "share_blake", "Portal permission scope", "sharing",
     "Blake Harmon visibility was finalized read only to Valley Partners records, "
     "write denied, isolation by explicit record scope"),
    ("salesforce", "portal_live", "SP status", "release",
     "the MVP shipped to production, Valley Partners community switched to Live, app bundle deployed clean"),
]
_DISTRACTORS = [  # ordinary, topically-varied noise — NOT adversarial keyword spam
    ("zoom", "cal_hyg", "Calendar hygiene", "ops",
     "reminder to decline invites you cannot attend and keep meeting titles short"),
    ("email", "standup", "Standup", "people",
     "the monday standup covered blockers and quick intros from new joiners"),
    ("digest", "deploy_notes", "Deploy notes", "eng",
     "release train cadence and the freeze policy were reviewed for next month"),
    ("email", "newsletter", "Newsletter", "mktg",
     "marketing draft with product spotlights and customer updates for the quarter"),
]
# reworded queries with ZERO content-word overlap with their gold — each maps to the (source, ref)
# that truly answers it. Pure FTS misses (nothing to match); semantics bridges the rewording.
_PROBES = [
    ("am i free friday", ("zoom", "cal_0715")),
    ("when is the offshore team coverage gap", ("digest", "tavant_cov")),
    ("what did we decide on Blake's access", ("salesforce", "share_blake")),
    ("is the partner site in prod yet", ("salesforce", "portal_live")),
]


def _rank_of(target, results) -> int:
    for i, r in enumerate(results):
        if (r["source"], r["ref"]) == target:
            return i + 1
    return 0  # 0 == not found


def _smoke() -> int:
    """PROOF: seed controlled work-lane docs into a temp FTS (via work_context_index's own machinery)
    AND a temp vector index, then run reworded queries showing hybrid/semantic surfaces the right
    chunk where pure FTS misses or mis-ranks. Uses the REAL code paths on controlled content so the
    numbers are honest and reproducible. Degrades cleanly if Ollama/embeddings are unreachable."""
    import sqlite3
    import tempfile
    print("=" * 78)
    print("work_rag smoke — HYBRID vs FTS-only on reworded queries (honest, seeded proof)")
    print("=" * 78)

    if not embed_available():
        print(f"\n[degraded] embedding model '{EMBED_MODEL}' unavailable in Ollama.")
        print(f"           run:  ollama pull {EMBED_MODEL}")
        print("           (FTS-only path still works; semantic proof skipped.)")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="work_rag_smoke_"))
    fts_db = tmp / "fts.db"
    vec_db = tmp / "vec.sqlite"

    # ── seed the FTS store through work_context_index's OWN upsert/schema (real BM25 path) ──
    orig_wci_db = wci.DB_PATH
    wci.DB_PATH = fts_db
    try:
        c = wci._conn()
        wci._ensure_schema(c)
        for source, ref, title, parts, body in (_GOLD + _DISTRACTORS):
            wci._upsert(c, source, ref, time.time(), title, parts, body, url=f"{source}:{ref}")
        c.commit()
        c.close()

        # ── seed the vector index (same docs) ──
        vcon = _db(vec_db)
        docs = [(s, r, t, f"{s}:{r}", time.time(), f"{t} · {p}\n{b}")
                for (s, r, t, p, b) in (_GOLD + _DISTRACTORS)]
        _index_docs(docs, vcon, progress_every=0)
        vcon.close()

        fts_hits = fts_wins = hybrid_wins = 0
        for query, target in _PROBES:
            fts_only = _fts_search(query, 8)
            hybrid = search(query, k=8, db_path=vec_db)  # exercises the real fusion
            fr = _rank_of(target, fts_only)
            hr = _rank_of(target, hybrid)
            fts_hits += 1 if fr == 1 else 0
            fts_wins += 1 if fr == 1 else 0
            hybrid_wins += 1 if hr == 1 else 0
            print(f"\nQ: {query!r}")
            print(f"   gold answer = ({target[0]}, {target[1]})")
            print(f"   FTS-only  top1={_fmt(fts_only, 0)}  | gold rank: {fr or 'MISS'}")
            print(f"   HYBRID    top1={_fmt(hybrid, 0)}  | gold rank: {hr or 'MISS'}")
            verdict = ("hybrid rescues (FTS missed/mis-ranked)" if hr == 1 and fr != 1
                       else "tie" if hr == fr else f"FTS#{fr or 'MISS'} -> HYBRID#{hr or 'MISS'}")
            print(f"   => {verdict}")

        n = len(_PROBES)
        print("\n" + "-" * 78)
        print(f"gold-at-rank-1:  FTS-only {fts_wins}/{n}   |   HYBRID {hybrid_wins}/{n}")
        print("-" * 78)
        if hybrid_wins > fts_wins:
            print("PROVEN: hybrid/semantic surfaces the answering chunk where pure FTS misses/mis-ranks.")
        elif hybrid_wins == fts_wins == n:
            print("Both perfect on this seed (raise distractor difficulty to separate them).")
        else:
            print("Hybrid did not beat FTS on this seed — numbers reported honestly, no cherry-picking.")
    finally:
        wci.DB_PATH = orig_wci_db  # restore — never leave the real FTS path pointed at the temp db
    return 0


def _fmt(hits, i) -> str:
    if not hits or i >= len(hits):
        return "(none)"
    h = hits[i]
    return f"({h['source']},{h['ref']})"


def _main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    if cmd == "index":
        from machine.heavy_work import allowed as _hw_allowed
        _ok, _why = _hw_allowed("heavy")
        if not _ok:
            print(f"[work_rag] skipped: {_why}")
            return 0
        lock = _try_index_lock()
        if lock is None:
            print("[work_rag] refresh already running; skipped")
            return 0
        try:
            incr = "--rebuild" not in sys.argv
            topic_stats = _refresh_topic_digests()
            if not topic_stats.get("ok"):
                print(f"[work_rag] topic digest refresh degraded: {topic_stats}", file=sys.stderr)
            result = index(incremental=incr)
            result["topic_digests"] = topic_stats
            return 0 if result.get("ok") else 1
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
    if cmd == "index-sources":
        names = [a for a in sys.argv[2:] if a and not a.startswith("-")]
        if not names:
            print("usage: python -m core.work_rag index-sources <source> ...", file=sys.stderr)
            return 2
        lock = _try_index_lock()
        if lock is None:
            print("[work_rag] refresh already running; skipped")
            return 0
        try:
            result = index_sources(names, refresh_projects=False)
            print(result)
            return 0 if result.get("ok") else 1
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
    if cmd == "search":
        q = " ".join(sys.argv[2:]) or "am i free friday"
        for r in search(q, k=8):
            print(f"[{r['source']}] {(r.get('title') or '')[:70]}  (ref={r['ref']}, score={r['score']})")
            print(f"   {(r.get('text') or '')[:200]}")
        return 0
    return _smoke()


if __name__ == "__main__":
    raise SystemExit(_main())
