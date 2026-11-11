#!/usr/bin/env python3
"""Unified work-context index — ONE full-text store the work GPT can search across
every local work source at once: Outlook email (subject + participants + preview for
all history, bodies backfilled incrementally), the valmont-local digests (DPP program,
people map, sprint, Zoom calendar/chat/summaries, reporting, Tavant, SP portal),
Little Bird notes/transcripts/action items, Salesforce tickets, and Monday sprint items.

Server-side only. Powers the `searchWorkContext` action so a single free-text query
fans out across everything instead of hitting one source at a time. CLI:
    python3 tools/work_context_index.py reindex
    python3 tools/work_context_index.py search "DPP onsite priorities Rich Matt"
"""
from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
DB_PATH = BASE_DIR / "data" / "work_context.db"
DIGESTS_DIR = Path.home() / ".openclaw" / "workspace" / "valmont-local" / "digests"

# Fail closed: this directory also contains private runtime, chat-continuity, travel,
# and revenue digests. The work index may ingest only named corporate/work artifacts.
# New work digests must be reviewed and added explicitly instead of being swept in by
# a broad ``*.md`` glob.
WORK_DIGEST_ALLOWLIST = frozenset({
    "ACTIVITY_LOG.md",
    "CURRENT_SPRINT_SWEEP_20260629.md",
    "FIGMA_SP_PORTAL.md",
    "HANDOFF_fern_sp_portal_2026-07-14.md",
    "HANDOFF_prepay_and_sp_portal_2026-07-14.md",
    # Reviewed and added 2026-08-09: all work-lane artifacts that were sitting on disk invisible
    # to the brain. Deliberately still EXCLUDED: STACK_MAP.md (the hustle-lane inventory),
    # TRIP_RECEIPTS.md, and WORK_SESSION_INSIGHTS.md — the last is a rollup of rows the
    # `session_insight` source already indexes directly, so admitting it would duplicate the
    # corpus for no retrieval gain.
    "HANDOFF_sp_portal_partial_2026-07-29.md",
    "HANDOFF_sp_portal_pipeline_2026-07-28.md",
    "REPORTING_INDEX.md",
    "SFDC_GIT_ONEDRIVE_MIGRATION_2026-07-29.md",
    "SP_PORTAL_DATA_CATALOG.md",
    "SP_PORTAL_DATA_RECONCILIATION.md",
    "SP_PORTAL_DATA_SOURCES.md",
    "SP_PORTAL_TIEOUT_PLAN_2026-08-04.md",
    "WORKTREE_TRIAGE_sp_portal_2026-07-29.md",
    "tavant.md",
    "LITTLEBIRD_NOTES.md",
    "OUTLOOK_INBOX.md",
    "OUTLOOK_SENT.md",
    # Mail-comb digests (Safari rotate / ops_owa_mail_comb) — reviewed 2026-09-27.
    # Full mailbox surface: unread Inbox stays OUTLOOK_INBOX; recent + Sent/Junk/
    # Archive/Deleted/Auto-* land as their own digests so work brain sees mail that
    # left the unread pile.
    "OUTLOOK_INBOX_RECENT.md",
    "OUTLOOK_MAIL_COMB.md",
    "OUTLOOK_JUNK.md",
    "OUTLOOK_ARCHIVE.md",
    "OUTLOOK_DELETED.md",
    "OUTLOOK_AUTO_ARCHIVED.md",
    "OUTLOOK_AUTO_TRIAGED.md",
    "OUTLOOK_CONVERSATION_HISTORY.md",
    "OUTLOOK_DRAFTS.md",
    "OUTLOOK_OPENED.md",
    "SFDC_REFERENCE.md",
    "SF_BUILD_LOG.md",
    "SF_SPRINT_BOARD.md",
    "WEEKLY_DIGEST.md",
    "WORKING_ON_NOW.md",
    "WORK_SOLUTIONS.md",
    "ZOOM_CALENDAR.md",
    "ZOOM_MEETING_SUMMARIES.md",
    "ZOOM_TEAM_CHAT.md",
    "dpp_program.md",
    # Hand-maintained corrections and facts that live in no email/ticket/thread. Every other
    # digest here is regenerated, so a fact written into one is lost on the next refresh.
    "key_facts.md",
    "people_map.md",
    "sp_portal.md",
    "sprint_current.md",
    "writing_voice.md",
})

# Alex 2026-10-16 "every possibly valuable bit of context MUST get to work brain".
# Work digests that were sitting outside the allowlist. WCI_DIGEST_EXTRA=0 turns them off.
# Still excluded on purpose: STACK_MAP.md (non-work lanes), TRIP_RECEIPTS.md (receipts /
# finance line), WORK_SESSION_INSIGHTS.md (duplicate of session_insight rows), QWEN_*.md (bot ops).
WORK_DIGEST_ALLOWLIST_EXTRA = frozenset({
    "ADO_ASSIGNED_TOME_BRIDGE.md",
    "ADO_OPEN_SPRINT.md",
    "HYBRID_LOCAL_PREP.md",
    "HANDOFF_dpp_number_opens_modal_2026-09-03.md",
    "HANDOFF_dpp_validation_drilldown_2026-09-03.md",
    "HANDOFF_sp_portal_2026-09-07.md",
    "SESSION_PICKUP_2026-08-28_marie-sp-portal.md",
    "WC_PROJECT_FACTS.md",
    "WORK_REFERENCE_INBOUND.md",
    "WORK_MAC_INBOUND.md",
    "WORK_PARITY.md",
    "ZOOM_TEAM_CHAT_BRIDGE.md",
    "LITTLEBIRD_OPENED.md",
    "LITTLEBIRD_IDB_OFFLINE.md",
    "LITTLEBIRD_BRIDGE.md",
    "RECENT_CHAT_CONTEXT.md",
})

# FTS columns: 0 source, 1 ref, 2 ts, 3 url (all UNINDEXED) / 4 title, 5 participants, 6 body
_BM25_WEIGHTS = "0.0, 0.0, 0.0, 0.0, 5.0, 3.0, 1.0"
# Recency weighting for search(). bm25 returns negative scores (more negative = better), so
# the weight shrinks an old hit's magnitude rather than adding a penalty. Anything inside the
# grace window is untouched; past it the score halves every _RECENCY_HALFLIFE_DAYS down to
# _RECENCY_FLOOR, which is what keeps a strong old exact match (an ID, a ticket number)
# findable instead of filtered away.
_RECENCY_GRACE_DAYS = float(os.environ.get("WCI_RECENCY_GRACE_DAYS", "14"))
_RECENCY_HALFLIFE_DAYS = float(os.environ.get("WCI_RECENCY_HALFLIFE_DAYS", "60"))
_RECENCY_FLOOR = float(os.environ.get("WCI_RECENCY_FLOOR", "0.40"))
_SEARCH_DEADLINE_S = float(os.environ.get("WCI_SEARCH_DEADLINE", "0.8"))
_BODY_DEADLINE_S = float(os.environ.get("WCI_BODY_DEADLINE", "0.6"))
_READ_BUSY_MS = int(os.environ.get("WCI_READ_BUSY_MS", "600"))
_WRITE_LOCK_TIMEOUT_S = float(os.environ.get("WCI_WRITE_LOCK_TIMEOUT", "600"))
_UFN_RE = re.compile(r"\b(?:github|fullsand|tavantfull)\b", re.I)
_UFN_TERMS = ("github", "fullsand", "tavantfull")

# Stable labels for retrieval/UI consumers. GitHub stays split by record kind so a
# caller can distinguish source, change, review, and transient local work precisely.
SOURCE_LABELS = {
    "github_repo": "GitHub repository",
    "github_source": "GitHub default-branch source",
    "github_commit": "GitHub remote change",
    "github_pr": "GitHub pull request",
    "github_pr_comment": "GitHub pull-request comment",
    "github_pr_review": "GitHub pull-request review",
    "github_pr_check": "GitHub pull-request check",
    "github_pr_file": "GitHub pull-request file",
    "github_issue": "GitHub issue",
    "github_issue_comment": "GitHub issue comment",
    "git_staged": "local Salesforce work in progress",
}
GITHUB_WORK_SOURCES = frozenset(SOURCE_LABELS)


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(str(source or ""), str(source or "work context"))


@contextmanager
def _sqlite_deadline(c: sqlite3.Connection, seconds: float):
    """Interrupt expensive SQLite plans on wall-clock deadline; always clear the handler."""
    setter = getattr(c, "set_progress_handler", None)
    if not setter:
        yield
        return
    deadline = time.monotonic() + max(0.01, float(seconds))
    setter(lambda: int(time.monotonic() >= deadline), 500)
    try:
        yield
    finally:
        setter(None, 0)


# Sentinel for "caller did not prefetch". None is a real OWA deferral.
class _WciUnset:
    pass


_WCI_UNSET = _WciUnset()
_prefetched_meta: contextvars.ContextVar = contextvars.ContextVar("wci_prefetched_meta", default=_WCI_UNSET)
_prefetched_body: contextvars.ContextVar = contextvars.ContextVar("wci_prefetched_body", default=_WCI_UNSET)
_progress_tls = threading.local()
_HOLDER_IO = threading.Lock()
_WRITER_POLL_S = 0.1
_YIELD_POLL_S = 0.25
_HEARTBEAT_S = 15.0


def _wci_kill(pid: int, sig: int) -> None:
    os.kill(pid, sig)


def _wci_time() -> float:
    return time.time()


def _wci_monotonic() -> float:
    return time.monotonic()


def _wci_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _wci_pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def _writer_stale_s() -> float:
    try:
        return float(os.environ.get("WCI_WRITER_STALE_S", "1200"))
    except (TypeError, ValueError):
        return 1200.0


def _writer_lock_path() -> Path:
    return DB_PATH.with_name(f"{DB_PATH.name}.writer.lock")


def _holder_json_path(lock_path: Path | None = None) -> Path:
    return Path(str(lock_path or _writer_lock_path()) + ".holder.json")


def _waiters_dir(lock_path: Path | None = None) -> Path:
    return Path(str(lock_path or _writer_lock_path()) + ".waiters")


def _argv_text() -> str:
    try:
        return " ".join(sys.argv)[:200]
    except Exception:
        return ""


def _read_holder(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_holder(path: Path, data: dict) -> None:
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)


def _mutate_holder(path: Path, fn) -> None:
    with _HOLDER_IO:
        info = _read_holder(path)
        if not info or int(info.get("pid") or 0) != os.getpid():
            return
        fn(info)
        _write_holder(path, info)


def _progress_age(info: dict) -> float | None:
    stamp = info.get("progress_at")
    if stamp is None:
        stamp = info.get("acquired_at")
    try:
        return _wci_time() - float(stamp)
    except (TypeError, ValueError):
        return None


def _writer_progress(label: str | None = None) -> None:
    """Bump progress_at on the holder sidecar. No-op when this process is not the holder."""
    path = getattr(_progress_tls, "path", None)
    if not path:
        return

    def mutate(info: dict) -> None:
        info["progress_at"] = _wci_time()
        if label:
            info["source"] = label
            info["label"] = label

    try:
        _mutate_holder(path, mutate)
    except Exception:
        pass


def _heartbeat_loop(path: Path, stop: threading.Event) -> None:
    while not stop.wait(_HEARTBEAT_S):
        def mutate(info: dict) -> None:
            info["heartbeat_at"] = _wci_time()

        try:
            _mutate_holder(path, mutate)
        except Exception:
            return


def _clean_dead_waiters(directory: Path) -> None:
    if not directory.is_dir():
        return
    for entry in list(directory.iterdir()):
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if not _wci_pid_alive(pid):
            try:
                entry.unlink()
            except OSError:
                pass


def _live_waiters(directory: Path) -> set[str]:
    _clean_dead_waiters(directory)
    found: set[str] = set()
    if not directory.is_dir():
        return found
    me = str(os.getpid())
    for entry in directory.iterdir():
        if entry.name == me:
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if _wci_pid_alive(pid):
            found.add(entry.name)
    return found


def _yield_to_waiters(max_s: float = 30.0) -> None:
    """Let a live waiter acquire between email slices. Returns when the set changes or max_s elapses."""
    initial = _live_waiters(_waiters_dir())
    if not initial:
        return
    deadline = _wci_monotonic() + max(0.0, max_s)
    while _wci_monotonic() < deadline:
        _wci_sleep(_YIELD_POLL_S)
        if _live_waiters(_waiters_dir()) != initial:
            return


def _arm_waiter(lock_path: Path) -> Path | None:
    directory = _waiters_dir(lock_path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _clean_dead_waiters(directory)
        path = directory / str(os.getpid())
        path.write_text(str(_wci_time()))
        return path
    except OSError:
        return None


def _wait_until_dead(pid: int, seconds: float = 15.0) -> bool:
    deadline = _wci_monotonic() + seconds
    while _wci_pid_alive(pid):
        if _wci_monotonic() >= deadline:
            return False
        _wci_sleep(min(0.25, max(0.01, deadline - _wci_monotonic())))
    return True


def _take_over_stale(lock_path: Path, already: set[int]) -> None:
    """Drop a dead holder's sidecar, or SIGTERM/SIGKILL a stale work_context_index.py writer."""
    side = _holder_json_path(lock_path)
    info = _read_holder(side) or {}
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        return
    if pid <= 0 or pid == os.getpid() or pid in already:
        return
    if not _wci_pid_alive(pid):
        try:
            side.unlink()
        except OSError:
            pass
        return
    age = _progress_age(info)
    if age is None or age < _writer_stale_s():
        return
    argv = str(info.get("argv") or "")
    if "work_context_index.py" not in argv:
        return
    already.add(pid)
    try:
        _wci_kill(pid, signal.SIGTERM)
    except OSError:
        pass
    if not _wait_until_dead(pid, 15.0):
        try:
            _wci_kill(pid, signal.SIGKILL)
        except OSError:
            pass
    print(
        f"[wci-lock] took over stale writer pid={pid} held={int(age)}s",
        file=sys.stderr,
    )


def _lock_timeout_message(timeout: float, side: Path) -> str:
    info = _read_holder(side) or {}
    pid = info.get("pid", "?")
    argv = info.get("argv", "")
    try:
        held = int(_wci_time() - float(info.get("acquired_at")))
    except (TypeError, ValueError):
        held = 0
    return (
        f"work-context writer lock exceeded {timeout:g}s "
        f"(holder pid={pid} argv={argv} held={held}s)"
    )


@contextmanager
def _index_write_lock(timeout_seconds: float | None = None, *, label: str | None = None):
    """Serialize every index writer so scoped feeds cannot starve a full refresh."""
    import fcntl

    timeout = _WRITE_LOCK_TIMEOUT_S if timeout_seconds is None else max(0.0, timeout_seconds)
    lock_path = _writer_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    deadline = _wci_monotonic() + timeout
    side = _holder_json_path(lock_path)
    waiter_file = None
    stop = threading.Event()
    hb: threading.Thread | None = None
    acquired = False
    tried_kill: set[int] = set()
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if waiter_file is None:
                    waiter_file = _arm_waiter(lock_path)
                _take_over_stale(lock_path, tried_kill)
                if _wci_monotonic() >= deadline:
                    raise TimeoutError(_lock_timeout_message(timeout, side))
                _wci_sleep(_WRITER_POLL_S)
        now = _wci_time()
        payload = {
            "pid": os.getpid(),
            "argv": _argv_text(),
            "source": label or "",
            "label": label or "",
            "acquired_at": now,
            "heartbeat_at": now,
            "progress_at": now,
        }
        try:
            with _HOLDER_IO:
                _write_holder(side, payload)
        except OSError:
            pass
        _progress_tls.path = side
        hb = threading.Thread(target=_heartbeat_loop, args=(side, stop), daemon=True)
        hb.start()
        yield
    finally:
        stop.set()
        if hb is not None:
            hb.join(timeout=1.0)
        if acquired:
            try:
                with _HOLDER_IO:
                    info = _read_holder(side)
                    if info and int(info.get("pid") or 0) == os.getpid():
                        side.unlink()
            except (OSError, ValueError, TypeError):
                pass
            _progress_tls.path = None
        if waiter_file is not None:
            try:
                waiter_file.unlink()
            except OSError:
                pass
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def writer_lock_status() -> dict:
    """Non-blocking view of the writer flock plus the holder sidecar. No heavy imports."""
    import fcntl

    lock_path = _writer_lock_path()
    info = _read_holder(_holder_json_path(lock_path)) or {}
    held = False
    fh = None
    if lock_path.exists():
        try:
            fh = lock_path.open("a+")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                held = True
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            held = False
        finally:
            if fh is not None:
                fh.close()
    now = _wci_time()

    def _age(stamp):
        try:
            return now - float(stamp)
        except (TypeError, ValueError):
            return None

    acquired = info.get("acquired_at")
    progress = info.get("progress_at")
    if progress is None:
        progress = acquired
    return {
        "held": bool(held),
        "pid": info.get("pid"),
        "argv": info.get("argv"),
        "held_s": _age(acquired),
        "progress_age_s": _age(progress),
    }


def _serialized_index_write(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _index_write_lock():
            return function(*args, **kwargs)

    return wrapped


WRITE_BUSY_S = float(os.environ.get("WORK_BRAIN_WRITE_BUSY_S", "600"))


def _conn() -> sqlite3.Connection:
    """Writer connection. WAL keeps live context searches readable during reindex."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 30s was not survivable: work-rag-refresh runs every 20 min and holds the write lock on a
    # 684MB db for MINUTES, so the session miner died mid-INSERT with "database is locked" and
    # the session lane admitted nothing for 73 hours (2026-08-25). Every writer here is a batch
    # job -- waiting out a long reindex is always better than losing the write. The lock is
    # still bounded, so a genuinely stuck holder still surfaces rather than hanging forever.
    c = sqlite3.connect(str(DB_PATH), timeout=WRITE_BUSY_S)
    c.row_factory = sqlite3.Row
    c.execute(f"PRAGMA busy_timeout={int(WRITE_BUSY_S * 1000)}")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _read_conn() -> sqlite3.Connection:
    """Read-only connection; never acquire a schema/write lock from a draft lookup."""
    c = sqlite3.connect(
        DB_PATH.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=max(0.05, _READ_BUSY_MS / 1000),
    )
    c.row_factory = sqlite3.Row
    c.execute(f"PRAGMA busy_timeout={max(50, _READ_BUSY_MS)}")
    return c


def _ensure_schema(c: sqlite3.Connection) -> None:
    c.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
        "source UNINDEXED, ref UNINDEXED, ts UNINDEXED, url UNINDEXED, "
        "title, participants, body, tokenize='porter unicode61')"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS seen("
        "source TEXT, ref TEXT, hash TEXT, has_body INT DEFAULT 0, ts REAL, "
        "PRIMARY KEY(source, ref))"
    )
    # Per-source semantic watermarks live in the same database transaction as
    # their indexed documents.  A rebuilt database therefore cannot mistake a
    # stale filesystem marker for indexed content.
    c.execute(
        "CREATE TABLE IF NOT EXISTS index_state("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS seen_source_body_ts "
        "ON seen(source, has_body, ts DESC)"
    )
    # docrow maps (source,ref) -> docs.rowid so DELETE/body lookups hit the FTS5 rowid index
    # instead of full-scanning the UNINDEXED source/ref columns (that scan turned ingest O(N^2)
    # and pinned it for minutes in fts5NextMethod/pread). Migrate + one-time backfill in place.
    cols = {r[1] for r in c.execute("PRAGMA table_info(seen)").fetchall()}
    if "docrow" not in cols:
        c.execute("ALTER TABLE seen ADD COLUMN docrow INTEGER")
    # A body fetch that comes back neither ok nor blocked is treated as a transport
    # failure and retried -- correct for a down bridge, and an infinite loop for a
    # message id that will never resolve again (deleted, moved, or purged). Those refs
    # sit at the head of the newest-first queue and consume the whole budget forever.
    if "body_tries" not in cols:
        c.execute("ALTER TABLE seen ADD COLUMN body_tries INTEGER DEFAULT 0")
    # Backfill any rows still missing docrow with a single full scan of docs (one-time cost).
    need = c.execute(
        "SELECT 1 FROM seen WHERE docrow IS NULL LIMIT 1"
    ).fetchone()
    if need:
        for rowid, source, ref in c.execute("SELECT rowid, source, ref FROM docs").fetchall():
            c.execute(
                "UPDATE seen SET docrow=? WHERE source=? AND ref=? AND docrow IS NULL",
                (rowid, source, ref),
            )


def _to_epoch(v) -> float:
    """Coerce a received/date value to a float epoch. Accepts a numeric epoch, an ISO string
    ('2026-07-02T13:28:33Z' / '...+00:00'), or 'YYYY-MM-DD HH:MM[:SS]' / 'YYYY-MM-DD'. NEVER raises —
    unknown -> 0.0. (The email bridges started returning ISO/'YYYY-MM-DD HH:MM' strings, which
    float() choked on, silently aborting the whole email pass and freezing the email index.)"""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except Exception:
            return 0.0
    s = str(v).strip()
    if not s:
        return 0.0
    try:
        return float(s)  # already-numeric string
    except Exception:
        pass
    from datetime import datetime as _dt
    try:
        return _dt.fromisoformat(s.replace("Z", "+00:00")).timestamp()  # py3.11+ is lenient
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.strptime(s, fmt).timestamp()
        except Exception:
            pass
    return 0.0


# The two-worlds firewall, at the one door every document walks through. Hustle-brand mail
# reaches the Valmont mailbox (a test to himself, a brand's own outreach landing in Archive or  # pii-allow
# Deleted Items) and the indexer, seeing ordinary email, filed it into the WORK brain -- where it
# then surfaced in work-lane reasoning. Blocking at read time is the only placement that holds:
# any later filter has to be remembered by every consumer, and one that forgets is a leak.
_HUSTLE_LANE_RE = re.compile(
    r"@(?:imafurad|hailports|aibuiltfast|buyersignalhq|orgdoc)\.(?:com|dev)"
    r"|\b(?:imafurad|hailports|aibuiltfast|buyersignalhq|orgdoc)\.(?:com|dev)\b",
    re.I,
)


def is_hustle_lane(*fields: str) -> bool:
    """True when any field carries a hustle-brand identity. Substring, deliberately blunt --
    over-blocking costs one work document, under-blocking crosses the firewall."""
    return any(_HUSTLE_LANE_RE.search(str(f or "")) for f in fields)


def _upsert(c, source, ref, ts, title, participants, body, url="", has_body=0) -> bool:
    """Insert/replace a doc; skip when content hash is unchanged. Returns True if written."""
    if is_hustle_lane(title, participants, body, url):
        return False
    title = (title or "").strip()
    participants = (participants or "").strip()
    body = (body or "").strip()
    _ts = _to_epoch(ts)
    h = hashlib.sha1((title + "\x00" + participants + "\x00" + body).encode("utf-8", "ignore")).hexdigest()
    row = c.execute(
        "SELECT hash, docrow, has_body FROM seen WHERE source=? AND ref=?", (source, ref)
    ).fetchone()
    if row and row["hash"] == h:
        # has_body is INDEX STATE, not content, so an unchanged content hash must not
        # veto it. When the meta pass already stored the whole message (short mail:
        # "can we push to a little later?"), the backfill re-fetches the identical
        # text, the hash matches, and the early return left has_body pinned at 0 --
        # so the ref stayed at the head of the backfill queue and was re-fetched every
        # cycle, forever. Measured 2026-08-07: 1,286 refs stuck that way, and because
        # they refilled the whole per-cycle budget, every email BEHIND them was starved
        # of a body too. Advance the flag even when nothing else changed.
        if has_body and int(row["has_body"] or 0) != int(has_body):
            c.execute(
                "UPDATE seen SET has_body=? WHERE source=? AND ref=?",
                (has_body, source, ref),
            )
            return True
        return False
    if row is not None:
        # Existing ref: drop the old doc by rowid (FTS5 rowid index, O(1)) not a source/ref scan.
        old = row["docrow"]
        if old is not None:
            c.execute("DELETE FROM docs WHERE rowid=?", (old,))
        else:
            c.execute("DELETE FROM docs WHERE source=? AND ref=?", (source, ref))
    # New ref (row is None): nothing to delete — skip the scan entirely.
    cur = c.execute(
        "INSERT INTO docs(source,ref,ts,url,title,participants,body) VALUES(?,?,?,?,?,?,?)",
        (source, ref, _ts, url, title, participants, body),
    )
    docrow = cur.lastrowid
    c.execute(
        "INSERT INTO seen(source,ref,hash,has_body,ts,docrow) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(source,ref) DO UPDATE SET hash=excluded.hash, "
        "has_body=excluded.has_body, ts=excluded.ts, docrow=excluded.docrow",
        (source, ref, h, has_body, _ts, docrow),
    )
    return True


def _doc_row_for(c, source: str, ref: str, seen_row, columns: str):
    """Fetch docs by rowid. Scan source/ref only when seen.docrow IS NULL.

    A rowid miss means the doc is gone — do not fall back to an UNINDEXED scan.
    No seen row means the ref was never indexed.
    """
    if seen_row is None:
        return None
    docrow = seen_row["docrow"]
    if docrow is not None:
        return c.execute(
            f"SELECT {columns} FROM docs WHERE rowid=?", (docrow,)
        ).fetchone()
    return c.execute(
        f"SELECT {columns} FROM docs WHERE source=? AND ref=?", (source, ref)
    ).fetchone()


def _delete_index_ref(c, source: str, ref: str) -> bool:
    """Remove one exact locally indexed document without touching its source system."""
    row = c.execute(
        "SELECT docrow FROM seen WHERE source=? AND ref=?", (source, ref)
    ).fetchone()
    if row is None:
        # Never indexed. docs.source/ref are UNINDEXED — a DELETE would full-scan FTS5.
        return False
    if row["docrow"] is not None:
        c.execute("DELETE FROM docs WHERE rowid=?", (row["docrow"],))
    else:
        # Compatibility cleanup for an old/orphaned row that predates docrow tracking.
        c.execute("DELETE FROM docs WHERE source=? AND ref=?", (source, ref))
    c.execute("DELETE FROM seen WHERE source=? AND ref=?", (source, ref))
    return True


def _delete_index_prefix(c, source: str, ref_prefix: str) -> int:
    """Remove one exact ref namespace; prefix matching is literal, never wildcard based."""
    prefix_len = len(ref_prefix)
    rows = c.execute(
        "SELECT ref FROM seen WHERE source=? AND substr(ref,1,?)=?",
        (source, prefix_len, ref_prefix),
    ).fetchall()
    for row in rows:
        _delete_index_ref(c, source, str(row["ref"]))
    # Remove any legacy FTS rows that never acquired a corresponding seen record.
    c.execute(
        "DELETE FROM docs WHERE source=? AND substr(ref,1,?)=?",
        (source, prefix_len, ref_prefix),
    )
    return len(rows)


def repair_duplicate_docs(dry_run: bool = False) -> dict:
    """Reap doc rows that share a (source, ref) with a newer copy of the same ref.

    `_upsert` removes the previous row by the rowid recorded in `seen`, which is O(1)
    and correct as long as that record exists. A crash between the `docs` INSERT and
    the `seen` upsert leaves a doc row that `seen` does not point at, so the next pass
    reads the ref as new, skips the delete, and inserts a second copy that nothing will
    ever remove. Measured 2026-09-02: 7 refs / 12 excess rows, including FIVE copies of
    `LITTLEBIRD_NOTES.md#0` with the oldest dated July. Rare, but a stale copy of a
    REGENERATED digest ranking above the current one is precisely the failure the index
    exists to prevent, and it is invisible to every freshness check because the newest
    copy is present and correct.

    Keeps the row `seen` points at; absent that, keeps the highest rowid (newest write).
    """
    c = _conn()
    try:
        dupes = c.execute(
            "SELECT source, ref FROM docs GROUP BY source, ref HAVING COUNT(*) > 1"
        ).fetchall()
        removed = 0
        details: list[str] = []
        for d in dupes:
            source, ref = d["source"], d["ref"]
            rows = [r["rowid"] for r in c.execute(
                "SELECT rowid FROM docs WHERE source=? AND ref=? ORDER BY rowid",
                (source, ref))]
            keep = c.execute("SELECT docrow FROM seen WHERE source=? AND ref=?",
                             (source, ref)).fetchone()
            keeper = keep["docrow"] if keep and keep["docrow"] in rows else max(rows)
            doomed = [r for r in rows if r != keeper]
            details.append(f"{source}|{ref}: {len(rows)} rows, keep {keeper}")
            if not dry_run:
                for rowid in doomed:
                    c.execute("DELETE FROM docs WHERE rowid=?", (rowid,))
            removed += len(doomed)
        if not dry_run and removed:
            c.commit()
        return {"duplicate_refs": len(dupes), "removed": removed,
                "dry_run": dry_run, "details": details}
    finally:
        c.close()


def _chunk_markdown(text: str, size: int = 1800):
    """Split markdown into (heading, chunk) pieces, carrying the nearest H1/H2/H3."""
    out, head, buf = [], "", []

    def flush():
        joined = "\n".join(buf).strip()
        if joined:
            out.append((head, joined))

    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            flush()
            buf = [line]
            head = m.group(2).strip()[:120]
        else:
            buf.append(line)
        if sum(len(x) for x in buf) >= size:
            flush()
            buf = []
    flush()
    return out or ([("", text.strip())] if text.strip() else [])


def _index_text_blob(c, source, ref_base, text, ts=None, title=None, size=None) -> int:
    text = (text or "").strip()
    if not text:
        return 0
    ts = ts if ts is not None else time.time()
    n = 0
    if size is None:
        try:
            size = int(os.environ.get("WCI_BLOB_CHUNK", "8000"))
        except Exception:
            size = 8000
        size = max(1500, min(size, 24000))
    chunks = [text[i : i + size] for i in range(0, len(text), size)] or [text]
    for i, chunk in enumerate(chunks):
        ref = f"{ref_base}#{i}"
        if _upsert(c, source, ref, ts, title or ref_base, ref_base, chunk, url=f"{source}:{ref_base}"):
            n += 1
    return n


def _background_owa_env() -> dict[str, str]:
    """Give scheduled index reads a bounded OWA token-capture lease.

    The bearer OWA hands out stays valid ~40 minutes and every REST call after capture
    is plain HTTPS, so a warm cache means this reader never touches CDP and never meets
    the inbox writer's lock at all. RAW_PASSIVE is what keeps that true across an
    expiry: it re-mints by observing the existing calendar tab's own traffic, taking no
    lease, which is safe even while the writer owns the bridge. Without it a cache that
    went cold inside a writer-held window could never re-mint -- and the writer holds
    ~8 of every 10 minutes -- so the reader stayed cold indefinitely. Defaulted here
    rather than only in the plists so an ad-hoc or hand-run pass behaves the same.
    """
    env = dict(os.environ)
    homebrew = "/opt/homebrew/bin:/usr/local/bin"
    env["PATH"] = f"{homebrew}:{env.get('PATH') or '/usr/bin:/bin'}"
    env.setdefault("OWA_TOKEN_CACHE", "1")
    env.setdefault("OWA_RAW_PASSIVE", "1")
    env["OWA_BACKGROUND_READER"] = "1"
    return env


def _owa_bridge_busy(payload: str) -> bool:
    """True when owa.js declined because the draft WRITER holds the bridge.

    A transport a writer is holding is healthy-but-occupied, not broken. owa.js
    exits non-zero with ``failure_kind: bridge_busy`` in that case, and treating
    it as an error made every collision a hard failure: measured 2026-08-06, the
    email index had been frozen since 2026-08-05 21:33 behind 238 consecutive
    "OWA bridge exited 1" lines that were all busy locks. The draft lane then had
    only 12-hour-old attention data, so it faithfully re-assessed the same stale
    items and drafted nothing new -- each job starving the other. This is the
    same distinction outlook_transport_guard._edge_result_is_busy already makes;
    this call site never learned it.
    """
    import json as _json
    try:
        value = _json.loads((payload or "").strip())
    except Exception:
        return False
    return (
        isinstance(value, dict)
        and value.get("ok") is False
        and str(value.get("failure_kind") or "") == "bridge_busy"
    )


_OWA_DOWN_RE = re.compile(
    # The last alternative is load-bearing. owa.js gained a dormancy PREFLIGHT that returns
    # {"ok":false,"reason":"dormant"} without ever opening a socket, so it never emits the
    # ECONNREFUSED string the other alternatives key on. The wake path below therefore stopped
    # firing the day dormancy shipped, and an idle on-demand bridge -- the normal state between
    # operations -- surfaced as the hard error `email_meta_err: OWA bridge refused`.
    r"OWA profile down|ECONNREFUSED 127\.0\.0\.1:18822|NOT logged in"
    r'|"reason"\s*:\s*"dormant"',
    re.I,
)


def _owa_bridge_down(payload: str) -> bool:
    """True when owa.js could not reach the managed Edge OWA profile at all."""
    return bool(_OWA_DOWN_RE.search(payload or ""))


def _wake_owa_bridge(*, wait_s: float = 30.0) -> bool:
    """Lease the one managed OWA bridge; never launch a second profile owner."""
    import subprocess as _sp, os as _os

    controller = _os.path.expanduser("~/valmont-drafter/bridgectl.sh")  # pii-allow
    try:
        done = _sp.run(
            [controller, "outlook", "ensure"],
            capture_output=True,
            text=True,
            timeout=max(45.0, wait_s),
        )
    except Exception:
        return False
    return done.returncode == 0 and (done.stdout or "").strip().splitlines()[-1:] == ["UP"]


def _node_bin() -> str:
    """Absolute node. launchd PATH often has none, which froze email_meta for days."""
    import shutil as _shutil
    for candidate in (
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
        os.path.expanduser("~/.npm-global/bin/node"),
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = _shutil.which("node")
    if found:
        return found
    raise FileNotFoundError("node binary not found")


def _owa_json(args: list, *, strict: bool = False, timeout: int = 25, _woke: bool = False):
    """Read email index data through OWA REST; never consult desktop Outlook."""
    import subprocess as _sp, json as _json, os as _os
    base = _os.path.expanduser("~/claude-stack")
    try:
        r = _sp.run([_node_bin(), _os.path.join(base, "tools/owa.js"), *args],
                    cwd=base, capture_output=True, text=True, timeout=timeout,
                    env=_background_owa_env())
        if not _woke and _owa_bridge_down(f"{r.stdout}\n{r.stderr}"):
            # Asleep, not broken. Wake the on-demand lane and read exactly once more.
            if _wake_owa_bridge():
                return _owa_json(args, strict=strict, timeout=timeout, _woke=True)
        if r.returncode != 0:
            # Busy is a deferral, never a failure. Returning None leaves the
            # existing index untouched and retries on the next cycle, which is
            # what the caller already does for a down bridge.
            if _owa_bridge_busy(r.stdout):
                return None
            raise RuntimeError(f"OWA bridge exited {r.returncode}")
        payload = (r.stdout or "").strip()
        if not payload:
            raise RuntimeError("OWA bridge returned no JSON")
        value = _json.loads(payload)
        # owa.js can print its refusal envelope {ok:false, failure_kind:...} and still
        # exit 0, so the returncode check above misses it. Callers expect a LIST of
        # records and iterate whatever comes back — iterating that dict yields its keys,
        # which is why a busy bridge surfaced as "'str' object has no attribute 'get'"
        # and silently stalled the email meta pass instead of deferring like the body
        # pass did. A refusal is never data; make it a deferral here, once, for everyone.
        if isinstance(value, dict) and value.get("ok") is False:
            if _owa_bridge_busy(payload):
                return None
            raise RuntimeError(str(value.get("error") or "OWA bridge refused")[:160])
        return value
    except Exception:
        if strict:
            raise
        return None


def _owa_text(args: list) -> str:
    import subprocess as _sp, os as _os
    base = _os.path.expanduser("~/claude-stack")
    try:
        r = _sp.run([_node_bin(), _os.path.join(base, "tools/owa.js"), *args],
                    cwd=base, capture_output=True, text=True, timeout=25,
                    env=_background_owa_env())
        return (r.stdout or "").strip()
    except Exception:
        return ""


_YIELD_BOX = contextvars.ContextVar("wci_draft_yield_box", default=None)
_DRAFT_RUN_LOCK = Path.home() / "llm-appliance/runtime/local-cycle-browser/DRAFT_RUNNER_LOCK"


def _yield_cap(cap_s=None) -> float:
    if cap_s is not None:
        try:
            return max(0.0, float(cap_s))
        except (TypeError, ValueError):
            return 180.0
    try:
        return max(0.0, float(os.environ.get("WCI_DRAFT_YIELD_CAP_S", "180")))
    except (TypeError, ValueError):
        return 180.0


def _new_yield_box(cap_s=None) -> dict:
    return {"left": _yield_cap(cap_s), "waited": 0.0, "sleeps": 0, "still_busy": False}


@contextmanager
def draft_yield_budget(cap_s=None):
    """One shared wait cap for every email yield in this scope."""
    token = _YIELD_BOX.set(_new_yield_box(cap_s))
    try:
        yield _YIELD_BOX.get()
    finally:
        _YIELD_BOX.reset(token)


def _runner_lock_held(path: Path | None = None) -> bool:
    lock = path or Path(os.environ.get("DRAFT_RUNNER_LOCK", str(_DRAFT_RUN_LOCK)))
    try:
        pid = int(lock.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _slots_busy(payload) -> bool:
    if not isinstance(payload, list):
        return False
    return any(isinstance(row, dict) and row.get("is_processing") is True for row in payload)


def _metrics_busy(text: str) -> bool:
    for line in str(text or "").splitlines():
        if not line.startswith("llamacpp:requests_processing"):
            continue
        try:
            return float(line.split()[-1]) > 0
        except (TypeError, ValueError):
            return False
    return False


def _draft_in_flight_live() -> bool:
    """True when the runner lock is held, a slot is processing, or metrics say so."""
    if _runner_lock_held():
        return True
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/slots", timeout=1.5) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if _slots_busy(payload):
            return True
    except Exception:
        pass
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/metrics", timeout=1.5) as resp:
            if _metrics_busy(resp.read().decode("utf-8", "replace")):
                return True
    except Exception:
        pass
    return False


def yield_while_draft(*, probe=None, sleep=None, cap_s=None) -> dict:
    """Sleep and back off while a draft is in flight, then resume.

    The caller always continues. Wait is capped so a stuck slot cannot drop the ingest.
    """
    if probe is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return {"waited_s": 0.0, "sleeps": 0, "still_busy": False, "resumed": True}
    sleeper = sleep or time.sleep
    check = probe if probe is not None else _draft_in_flight_live
    box = _YIELD_BOX.get()
    if box is None:
        box = _new_yield_box(cap_s)
    delay = 2.0
    while check():
        if box["left"] <= 0:
            box["still_busy"] = True
            break
        step = min(delay, box["left"])
        sleeper(step)
        box["left"] -= step
        box["waited"] += step
        box["sleeps"] += 1
        delay = min(delay * 2.0, 8.0)
    else:
        box["still_busy"] = False
    return {
        "waited_s": round(box["waited"], 3),
        "sleeps": box["sleeps"],
        "still_busy": bool(box["still_busy"]),
        "resumed": True,
    }


def _email_index_window(limit=25000):
    import os as _os
    try:
        top = int(_os.environ.get("WCI_EMAIL_TOP", "8000"))
    except Exception:
        top = 8000
    try:
        body_top = int(_os.environ.get("WCI_EMAIL_BODY_TOP", "0"))
    except Exception:
        body_top = 500
    effective_top = min(limit, max(top, 600))
    try:
        index_timeout = int(_os.environ.get("WCI_EMAIL_INDEX_TIMEOUT_S", "0"))
    except Exception:
        index_timeout = 0
    if index_timeout <= 0:
        index_timeout = max(60, min(300, 25 + effective_top // 10))
    return effective_top, max(0, body_top), index_timeout


def _fetch_email_meta_rows(limit=25000):
    """OWA meta fetch. No database and no writer lock."""
    effective_top, body_top, index_timeout = _email_index_window(limit)
    return _owa_json(
        [
            "index",
            "--top",
            str(effective_top),
            "--body-top",
            str(body_top),
        ],
        strict=True,
        timeout=index_timeout,
    )


def index_email_meta(c, limit=25000, rows=None) -> int:
    """Index recent mail across every mailbox folder — HEADLESS via OWA REST (no app).
    Depth is env-tunable (WCI_EMAIL_TOP, default 2500) so the corpus goes as far back as the bridge
    exposes. WCI_EMAIL_BODY_TOP controls how many newest messages carry authoritative full
    bodies in the bulk pass. Existing full bodies are never downgraded to previews.
    Pass rows to skip the network fetch (ingest pulls them before taking the writer lock)."""
    if rows is None:
        pending = _prefetched_meta.get()
        if pending is _WCI_UNSET:
            rows = _fetch_email_meta_rows(limit)
        else:
            rows = pending
    if not rows:  # bridge down / not logged in → leave the existing index untouched
        return 0
    n = 0
    for r in rows:
        rid = str(r.get("id") or "")
        if not rid:
            continue
        if r.get("is_draft") is True:
            # OWA is authoritative here. A tombstone removes any exact draft that an
            # older all-folder index pass admitted, without reading its body again.
            _delete_index_ref(c, "email", rid)
            continue
        if r.get("is_draft") is not False:
            # Missing/ambiguous draft state is not permission to absorb mailbox text.
            continue
        title = (r.get("subject") or "(no subject)").strip()
        parts = (r.get("parts") or "").strip()
        preview = (r.get("preview") or "").strip()
        full_body = (r.get("body") or "").strip()
        if _UFN_RE.search(f"{title}\n{parts}\n{preview}\n{full_body}"):
            continue
        prior = c.execute(
            "SELECT has_body,docrow FROM seen WHERE source='email' AND ref=?", (rid,)
        ).fetchone()
        hb = int(prior["has_body"] or 0) if prior else 0
        body = full_body
        if full_body:
            hb = 1
        elif prior and hb == 1:
            old = _doc_row_for(c, "email", rid, prior, "body")
            body = str(old["body"] or "") if old else preview
        else:
            body = preview
        if r.get("has_attachments") and "[has_attachments]" not in body:
            body = (body + "\n[has_attachments]").strip()
        cap = int(os.environ.get("WCI_EMAIL_BODY_CHARS", "120000"))
        if _upsert(c, "email", rid, r.get("received"), title, parts, body[:cap],
                   url=f"owa:{rid}", has_body=hb):
            n += 1
            # Commit in chunks for the same reason the body drain does: the caller commits
            # once per _run(), so upserting 2500 rows in one transaction holds the write lock
            # for the whole pass. The sibling indexer overlaps at :06/:21/:36/:51 and dies on
            # "database is locked" -- and that exception is swallowed into stats, so the pass
            # reported `email_meta: 0` and read as "no new mail" while the index went days
            # stale and the brain audit fired every hour with nobody able to see why.
            if n % 200 == 0:
                c.commit()
                _writer_progress()
    return n


_OWA_WRITER_LOCK = Path.home() / ".stack-locks" / "inbox_reply_draft.lock"
_BODY_MAX_TRIES = int(os.environ.get("WCI_EMAIL_BODY_MAX_TRIES", "4"))


def _await_owa_writer_idle(wait_s: float = 150.0) -> bool:
    """Block until the inbox draft writer releases the OWA bridge.

    owa.js refuses a BACKGROUND_READER while the writer holds the lock, and the
    caller correctly treats that as a deferral rather than an error. But the writer
    fires every 5 minutes and a single cycle can run longer than the gap, so
    "defer to the next cycle" degenerates into never reading at all -- which is how
    1,286 emails ended up permanently body-less. Waiting for the lock instead of
    skipping keeps the writer's priority intact (it still finishes first) while
    guaranteeing the index eventually gets its turn. Bounded, so a wedged writer
    costs one slow cycle, not a hung job.
    """
    import time as _time

    deadline = _time.monotonic() + wait_s
    while _time.monotonic() < deadline:
        try:
            pid = int((_OWA_WRITER_LOCK.read_text() or "0").split()[0])
        except (OSError, ValueError, IndexError):
            return True
        try:
            os.kill(pid, 0)
        except OSError:
            return True  # stale lock; owner is gone
        _time.sleep(5.0)
    return False



_ATTACH_HARDLINE = re.compile(
    r"(?i)\b(lawyer|attorney|legal|finance|financial|accountant|bank|payroll|password|passcode|passwd|"
    r"seed phrase|2fa|otp|verification code)\b"
)
_SECRET_KV = re.compile(r"(?i)\b(password|passcode|pwd|otp|2fa code|verification code|security code)\b(\s*(?:is|:|=)\s*)\S+")


def _mask_secrets_inline(text: str) -> str:
    return _SECRET_KV.sub(lambda m: m.group(1) + m.group(2) + "[masked]", text or "")


def index_email_attachments(c, budget=None) -> int:
    """Pull extractable file-attachment TEXT into the brain. No binaries stored."""
    import base64
    import tempfile

    from tools.attachment_extract import extract_text

    try:
        budget = int(budget if budget is not None else os.environ.get("WCI_EMAIL_ATTACH_TOP", "40"))
    except Exception:
        budget = 40
    budget = max(0, min(budget, 80))
    if budget <= 0:
        return 0
    _await_owa_writer_idle()
    rows = _owa_json(["attachment-texts", "--top", str(budget)], strict=False, timeout=45)
    if not isinstance(rows, list):
        return 0
    n = 0
    extractable = {
        ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".csv", ".md", ".html",
        ".htm", ".rtf", ".eml", ".json", ".xml", ".yaml", ".yml", ".log",
    }
    try:
        char_cap = int(os.environ.get("WCI_EMAIL_ATTACH_CHARS", "80000"))
    except Exception:
        char_cap = 80000
    for item in rows:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or "")
        if not rid or item.get("is_draft") is True:
            continue
        subj = str(item.get("subject") or "")
        parts = str(item.get("parts") or "")
        files = item.get("files") or []
        names = []
        for att in files:
            if not isinstance(att, dict):
                continue
            name = str(att.get("name") or "attachment")
            names.append(name)
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
            b64 = att.get("content_b64") or ""
            if ext not in extractable or not b64:
                continue
            try:
                raw = base64.b64decode(b64, validate=False)
            except Exception:
                continue
            if _UFN_RE.search(f"{subj}\n{parts}\n{name}"):
                continue
            with tempfile.NamedTemporaryFile(suffix=ext or ".bin", delete=True) as tmp:
                tmp.write(raw)
                tmp.flush()
                text = extract_text(tmp.name, cap=char_cap)
            if not text or str(text).startswith("[extract error"):
                continue
            # Hard data line (Alex): no lawyer/accountant/bank/legal/finance or credential content.
            if _ATTACH_HARDLINE.search(f"{subj}\n{parts}\n{name}\n{text}"):
                continue
            text = _mask_secrets_inline(str(text))
            blob = f"Email attachment {name} on {subj}\n{text}"
            n += _index_text_blob(
                c,
                "email_attachment",
                f"{rid}::{name}",
                blob,
                title=f"Attachment: {name} — {subj}"[:120],
            )
        if names:
            n += _index_text_blob(
                c,
                "email_attachment",
                f"{rid}::names",
                f"Attachments on {subj}: " + ", ".join(names),
                title=f"Attachment names: {subj}"[:120],
            )
    return n


_BODY_CANDIDATE_SQL = (
    "SELECT s.ref,d.title,d.participants,d.body "
    "FROM seen s JOIN docs d ON d.rowid=s.docrow "
    "WHERE s.source='email' AND s.has_body=0 AND COALESCE(s.body_tries,0) < ? "
    "ORDER BY s.ts DESC LIMIT ?"
)


def _collect_body_refs(rows, budget: int) -> tuple[list[str], dict]:
    refs: list[str] = []
    jtxt: dict = {}
    for row in rows:
        if _UFN_RE.search(
            f"{row['title'] or ''}\n{row['participants'] or ''}\n{row['body'] or ''}"
        ):
            continue
        rid = str(row["ref"])
        refs.append(rid)
        jtxt[rid] = (
            f"From: {row['participants'] or ''} | Subject: {row['title'] or ''} | "
            f"{str(row['body'] or '')[:300]}"
        )
        if len(refs) >= budget:
            break
    return refs, jtxt


def _jev_noise_refs(refs: list[str], jtxt: dict) -> list[str]:
    """Refs the opt-in JEV noise filter would skip. No database writes."""
    if not refs or os.environ.get("JEV_DISABLE", "") == "1" or os.environ.get("WCI_JEV_NOISE_SKIP", "0") != "1":
        return []
    try:
        import sys as _jsys
        _jcore = str(Path.home() / "claude-stack" / "core")
        if _jcore not in _jsys.path:
            _jsys.path.append(_jcore)
        import jev_hooks as _jh
        cand = refs[:20]
        noise = _jh.noise_batch([jtxt.get(r, "") for r in cand], caller="wci_email_bodies")
        return [cand[i] for i in sorted(noise) if isinstance(i, int) and 0 <= i < len(cand)]
    except Exception:
        return []


def _mark_body_noise(c, noise: list[str]) -> None:
    for rid in noise:
        c.execute(
            "UPDATE seen SET body_tries=? WHERE source='email' AND ref=?",
            (_BODY_MAX_TRIES, rid),
        )


def _fetch_bodies_owa(refs: list[str]):
    fetched = None
    for _ in range(3):
        _await_owa_writer_idle()
        fetched = _owa_json(["bodies", "--ids-json", json.dumps(refs)], strict=True)
        if isinstance(fetched, list):
            return fetched
    return fetched


def _bodies_by_ref(fetched) -> dict:
    if not isinstance(fetched, list):
        return {}
    return {
        str(item.get("id") or ""): item
        for item in fetched
        if isinstance(item, dict) and item.get("id")
    }


def _fetch_email_body_payload(budget=80):
    """Select body candidates and fetch OWA bodies. No writer lock.

    Returns None when the read-only lookup cannot run (caller fetches under the lock).
    """
    try:
        c = _read_conn()
    except Exception:
        return None
    try:
        try:
            rows = c.execute(
                _BODY_CANDIDATE_SQL, (_BODY_MAX_TRIES, max(budget, budget * 10))
            ).fetchall()
        except Exception:
            return None
    finally:
        c.close()
    refs, jtxt = _collect_body_refs(rows, budget)
    noise = _jev_noise_refs(refs, jtxt)
    skip = set(noise)
    fetch_refs = [r for r in refs if r not in skip]
    fetch_failed = False
    by_ref: dict = {}
    if fetch_refs:
        fetched = _fetch_bodies_owa(fetch_refs)
        if not isinstance(fetched, list):
            fetch_failed = True
        else:
            by_ref = _bodies_by_ref(fetched)
    return {
        "refs": fetch_refs,
        "noise_refs": noise,
        "by_ref": by_ref,
        "fetch_failed": fetch_failed,
    }


def index_email_bodies(c, budget=80, max_batches: int = 8, payload=None) -> int:
    """Backfill decoded full bodies through the OWA REST bridge.

    Drains repeatedly while the bridge stays free. The inbox draft writer holds the
    OWA lock for most of every 5-minute cycle (its specialist review alone is
    budgeted at 540s), so a reader that fetches exactly one batch per launchd tick
    lands maybe once an hour -- far slower than new mail arrives, which is how the
    body backlog grew instead of shrinking. Making one hard-won window worth many
    batches is what turns the backlog around; it still yields the moment the writer
    reappears.
    A payload from _fetch_email_body_payload applies one already-fetched batch.
    """
    if payload is None:
        pending = _prefetched_body.get()
        if pending is not _WCI_UNSET:
            payload = pending
    if payload is not None:
        total = _index_email_bodies_batch(c, budget, payload=payload)
        c.commit()
        _writer_progress()
        return total
    total = 0
    for _ in range(max(1, max_batches)):
        got = _index_email_bodies_batch(c, budget)
        total += got
        # Commit BETWEEN batches, never once at the end. The caller commits per _run(),
        # so a multi-batch drain would otherwise hold one write transaction across every
        # batch's network round-trips — minutes, well past the 30s busy_timeout — and the
        # sibling indexer job (they overlap at :06/:21/:36/:51) dies on "database is
        # locked". Batching is a throughput win only if it does not hold the lock.
        c.commit()
        _writer_progress()
        if got <= 0 or _owa_writer_active():
            break
    return total


def _owa_writer_active() -> bool:
    try:
        pid = int((_OWA_WRITER_LOCK.read_text() or "0").split()[0])
    except (OSError, ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _apply_email_bodies(c, refs, by_ref) -> int:
    n = 0
    for rid in refs:
        item = by_ref.get(rid) or {}
        body = str(item.get("body") or "").strip()
        if body and not body.startswith("("):
            # Scan source/ref only when docrow IS NULL. A rowid miss means the doc is gone.
            sref = c.execute(
                "SELECT docrow FROM seen WHERE source='email' AND ref=?", (rid,)
            ).fetchone()
            d = _doc_row_for(c, "email", rid, sref, "title,participants,ts,url,body")
            if d:
                if _upsert(
                    c,
                    "email",
                    rid,
                    d["ts"],
                    d["title"],
                    d["participants"],
                    body[:int(os.environ.get("WCI_EMAIL_BODY_CHARS", "120000"))],
                    url=d["url"],
                    has_body=1,
                ):
                    n += 1
        elif item.get("ok") or item.get("blocked"):
            # A successful empty read or a deliberately excluded body is final.
            c.execute("UPDATE seen SET has_body=2 WHERE source='email' AND ref=?", (rid,))
        else:
            # Neither ok nor blocked: retry, but count the attempt. A genuine transport
            # blip clears well inside the allowance; an id that no longer resolves never
            # will, and unbounded retries let it hold the head of the queue forever.
            c.execute(
                "UPDATE seen SET body_tries=COALESCE(body_tries,0)+1 "
                "WHERE source='email' AND ref=?",
                (rid,),
            )
    return n


def _index_email_bodies_batch(c, budget=80, payload=None) -> int:
    if payload is not None:
        noise = [str(r) for r in (payload.get("noise_refs") or [])]
        _mark_body_noise(c, noise)
        if payload.get("fetch_failed"):
            return 0
        refs = [str(r) for r in (payload.get("refs") or [])]
        by_ref = payload.get("by_ref") or {}
        # Re-validate under the lock: a ref that gained a body or aged out is skipped.
        still = []
        for rid in refs:
            row = c.execute(
                "SELECT has_body, body_tries FROM seen WHERE source='email' AND ref=?",
                (rid,),
            ).fetchone()
            if row is None:
                continue
            if int(row["has_body"] or 0) != 0:
                continue
            if int(row["body_tries"] or 0) >= _BODY_MAX_TRIES:
                continue
            still.append(rid)
        refs = still
        if not refs:
            return 0
        return _apply_email_bodies(c, refs, by_ref)
    rows = c.execute(
        _BODY_CANDIDATE_SQL, (_BODY_MAX_TRIES, max(budget, budget * 10))
    ).fetchall()
    refs, _jtxt = _collect_body_refs(rows, budget)
    # JEV 2026-10-07 noise filter: confident automated/newsletter noise skips the OWA body
    # fetch (metadata stays indexed; body_tries maxed so it is not re-picked). Fail-open.
    # Alex 2026-10-16: fetch every body; the JEV noise skip is now opt-in (WCI_JEV_NOISE_SKIP=1).
    if refs and os.environ.get("JEV_DISABLE", "") != "1" and os.environ.get("WCI_JEV_NOISE_SKIP", "0") == "1":
        try:
            import sys as _jsys
            _jcore = str(Path.home() / "claude-stack" / "core")
            if _jcore not in _jsys.path:
                _jsys.path.append(_jcore)
            import jev_hooks as _jh
            _cand = refs[:20]
            _noise = _jh.noise_batch([_jtxt.get(r, "") for r in _cand], caller="wci_email_bodies")
            for i in sorted(_noise):
                c.execute("UPDATE seen SET body_tries=? WHERE source='email' AND ref=?", (_BODY_MAX_TRIES, _cand[i]))
            if _noise:
                refs = [r for j, r in enumerate(refs) if not (j < 20 and j in _noise)]
        except Exception:
            pass
    if not refs:
        return 0
    # Wait-then-fire races the writer: the lock can be re-taken between our check
    # and owa.js's own, and one lost race used to cost the whole cycle. Retry across
    # the wait instead of checking once.
    fetched = None
    for _ in range(3):
        _await_owa_writer_idle()
        fetched = _owa_json(["bodies", "--ids-json", json.dumps(refs)], strict=True)
        if isinstance(fetched, list):
            break
    if not isinstance(fetched, list):
        return 0
    by_ref = _bodies_by_ref(fetched)
    return _apply_email_bodies(c, refs, by_ref)


def index_digests(c) -> int:
    if not DIGESTS_DIR.exists():
        return 0
    from tools.valmont_surface_guard import filter_text_strict

    n = 0
    current_refs: set[str] = set()
    unavailable_files: set[str] = set()
    for f in sorted(DIGESTS_DIR.glob("*.md")):
        if f.name not in WORK_DIGEST_ALLOWLIST and not (
            os.environ.get("WCI_DIGEST_EXTRA", "1") == "1" and f.name in WORK_DIGEST_ALLOWLIST_EXTRA
        ):
            continue
        try:
            # Source filtering is defensive even for approved files: work chat and
            # build logs can still contain a one-off local-tooling line.
            txt = filter_text_strict(f.read_text(errors="ignore"))
        except Exception:
            unavailable_files.add(f.name)
            continue
        ts = f.stat().st_mtime
        for i, (head, chunk) in enumerate(_chunk_markdown(txt, 1800)):
            ref = f"{f.name}#{i}"
            current_refs.add(ref)
            title = f"{f.stem}" + (f": {head}" if head else "")
            if _upsert(c, "digest", ref, ts, title, f.name, chunk, url=f"digest:{f.name}"):
                n += 1

    # Incremental ingest must also remove chunks whose file is no longer approved,
    # disappeared, or shrank after filtering. Otherwise old private chunks survive
    # forever in FTS even though the source is now fenced. Preserve an approved file's
    # prior chunks when that file was temporarily unreadable during this pass.
    for ref, docrow in c.execute(
        "SELECT ref, docrow FROM seen WHERE source='digest'"
    ).fetchall():
        if ref in current_refs:
            continue
        if ref.split("#", 1)[0] in unavailable_files:
            continue
        if docrow is not None:
            c.execute("DELETE FROM docs WHERE rowid=?", (docrow,))
        else:
            c.execute("DELETE FROM docs WHERE source='digest' AND ref=?", (ref,))
        c.execute("DELETE FROM seen WHERE source='digest' AND ref=?", (ref,))
    return n


def is_work_digest_ref(ref: str) -> bool:
    """True when an indexed digest ref belongs to the reviewed work-only corpus."""
    stem = str(ref or "").split("#", 1)[0]
    if stem in WORK_DIGEST_ALLOWLIST:
        return True
    # Ingest admits the extra set by default. The vector pass has to use the same rule,
    # or those notes sit in keyword search and never become embeddings.
    return os.environ.get("WCI_DIGEST_EXTRA", "1") == "1" and stem in WORK_DIGEST_ALLOWLIST_EXTRA


_MONDAY_ITEM_LINE = re.compile(r"^(.*?)\s\(ID:\s*(\d+)\):\s?(.*)$")
_MONDAY_ITEM_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_LEGACY_MONDAY_SPRINT_REF = re.compile(r"^sprint_items#[0-9]+$")


def _iter_monday_items(text: str):
    """Yield only current line-per-item records from a successful Monday board body."""
    for line in (text or "").splitlines():
        match = _MONDAY_ITEM_LINE.match(line.strip())
        if match is None:
            continue
        name, item_id, columns = (
            match.group(1).strip(),
            match.group(2).strip(),
            match.group(3).strip(),
        )
        if name and _MONDAY_ITEM_ID.fullmatch(item_id):
            yield name, item_id, columns


def _monday_item_ids(text: str) -> set[str]:
    """Return the exact native item identities present in a board response."""
    return {item_id for _name, item_id, _columns in _iter_monday_items(text)}


def _prune_legacy_monday_sprint_refs(c, current_item_ids: set[str]) -> int:
    """Remove obsolete positional board chunks after a valid current item pull.

    ``sprint_items#<n>`` is a legacy collection snapshot, not a native Monday
    identity.  Do not prune it merely because a request succeeded: a malformed
    or empty response must preserve the previous local context.  Once the
    current response contains at least one exact ``item_<id>`` record, those
    stale local-only chunks are safe to retire.  Update/comment refs remain
    intact because they are a different indexed source shape.
    """
    valid_ids = {
        str(item_id) for item_id in current_item_ids
        if _MONDAY_ITEM_ID.fullmatch(str(item_id))
    }
    if not valid_ids:
        return 0
    try:
        if not c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='seen' LIMIT 1"
        ).fetchone():
            return 0
        rows = c.execute(
            "SELECT rowid, ref FROM docs WHERE source='monday'"
        ).fetchall()
    except sqlite3.Error:
        return 0
    removed = 0
    for rowid, ref_raw in rows:
        ref = str(ref_raw or "")
        if _LEGACY_MONDAY_SPRINT_REF.fullmatch(ref) is None:
            continue
        try:
            c.execute("DELETE FROM docs WHERE rowid=?", (rowid,))
            c.execute("DELETE FROM seen WHERE source='monday' AND ref=?", (ref,))
            removed += 1
        except sqlite3.Error:
            # A partial prune must never turn an indexing success into a failed
            # run. The next confirmed board pull will retry the remaining row.
            continue
    return removed


def _index_monday_items(c, text: str, ref_prefix: str = "item") -> int:
    """Parse _get_board_items' line-per-item output ('<name> (ID: <id>): col=val, col=val, ...')
    into ONE doc per item (ref=item_<id>) instead of one 1500-char positional blob, so retrieval
    doesn't straddle multiple tickets in a single chunk."""
    n = 0
    ts = time.time()
    for name, iid, cols in _iter_monday_items(text):
        if _upsert(c, "monday", f"{ref_prefix}_{iid}", ts, name, "monday_sprint_items", cols,
                   url=f"monday:{iid}"):
            n += 1
    return n


def _require_salesforce_pull(raw, label: str):
    """Reject registry/tool failures that are returned as strings instead of raised."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if any(raw.get(key) for key in ("error", "errors", "blocked")):
            raise RuntimeError(f"{label} pull failed")
        return raw
    text = str(raw or "").strip()
    if re.match(r"^Total:\s*\d+\s+records(?:\s|$)", text):
        return raw
    raise RuntimeError(f"{label} pull returned no verified Salesforce result")


async def _pull_sf_tickets_paged(reg, page_size: int = 200, max_rows: int = 3000) -> list:
    """Page past salesforce_soql_query's fixed 250-row TEXT-FORMAT cap (tools/salesforce.py
    _format_query_results limit=250 truncates the STRING regardless of the SOQL LIMIT) via
    LastModifiedDate keyset pagination — plain OFFSET can't be used past Salesforce's own 2000-row
    OFFSET ceiling. Each page stays under 250 rows so the tool never truncates it. Returns a
    normalized list of record dicts (via _sf_recs, which also handles the tool's plain-text
    'Total: N records\\n' + one python-dict-repr line per row format)."""
    out: list = []
    cursor = None
    pages = 0
    max_pages = (max_rows // page_size) + 2
    fields = "Name, Subject__c, Status__c, Priority__c, Description__c, LastModifiedDate"
    while len(out) < max_rows and pages < max_pages:
        where = f"WHERE LastModifiedDate < {cursor} " if cursor else ""
        soql = (f"SELECT {fields} FROM Salesforce_Ticket__c {where}"
                f"ORDER BY LastModifiedDate DESC LIMIT {page_size}")
        r = await reg.execute("salesforce_soql_query", {"query": soql}, source="ctx_index")
        _require_salesforce_pull(r, "Salesforce tickets")
        recs = _sf_recs(r)
        pages += 1
        if not recs:
            break
        out.extend(recs)
        last = recs[-1].get("LastModifiedDate") if isinstance(recs[-1], dict) else None
        if not last or last == cursor:
            break  # no progress (duplicate boundary ts) — stop rather than loop forever
        cursor = last
        if len(recs) < page_size:
            break  # short page = exhausted
    return out[:max_rows]


def _index_salesforce(c, result) -> int:
    """Index Salesforce tickets one-doc-per-ticket. Parses via _sf_recs — the tool returns
    'Total: N records\\n' + one python-dict-repr line per row (NOT JSON), so the old json.loads
    path silently failed on nearly every real payload and fell through to a single opaque blob,
    which is why ~91% of tickets were missing from the index. Accepts either that raw tool
    string/dict or an already-normalized list of record dicts (e.g. from _pull_sf_tickets_paged)."""
    recs = _sf_recs(result)
    if not recs:
        raw = str(result).strip()
        # never index an empty pull or a registry/tool error string as "Salesforce tickets" content
        if not raw or raw == "[]" or raw.startswith(("Error", "ERROR", "BLOCKED", "Salesforce returned")):
            return 0
        return _index_text_blob(c, "salesforce", "tickets", raw, title="Salesforce tickets")
    n = 0
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        name = str(rec.get("Name") or rec.get("Id") or "").strip()
        if not name:
            continue
        title = f"{name}: {rec.get('Subject__c') or ''}".strip()
        body = " | ".join(
            f"{k}={rec.get(k)}"
            for k in ("Status__c", "Priority__c", "Description__c", "LastModifiedDate")
            if rec.get(k)
        )
        if _upsert(c, "salesforce", name, time.time(), title, "Salesforce_Ticket__c", body,
                   url=f"sf:{name}"):
            n += 1
    return n


def _sf_recs(raw):
    """Normalize a salesforce_soql_query result into a list-of-dict records. The tool returns a
    human-formatted STRING ('Total: N records\\n' + one python-dict-repr per line), so parse that via
    ast.literal_eval; also handle a raw dict/list/JSON-string in case a caller returns structured."""
    if isinstance(raw, dict):
        r = raw.get("records") or raw.get("results") or raw.get("data")
        return r if isinstance(r, list) else []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        return []
    try:
        p = json.loads(raw)
        if isinstance(p, dict) and isinstance(p.get("records"), list):
            return p["records"]
        if isinstance(p, list):
            return p
    except Exception:
        pass
    import ast as _ast
    out = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            d = _ast.literal_eval(ln)
            if isinstance(d, dict):
                out.append(d)
        except Exception:
            continue
    return out


async def _index_sf_chatter(c, reg_execute) -> int:
    """Index Chatter as first-class context: FeedItem posts + FeedComment replies, deepest history the
    org retains. Author comes from the double-nested CreatedBy.Name (a known parse gotcha)."""
    import html as _html
    n = 0

    def _clean(b):
        b = re.sub(r"<[^>]+>", " ", str(b or ""))
        return re.sub(r"\s+", " ", _html.unescape(b)).strip()

    async def _pull(soql):
        result = await reg_execute("salesforce_soql_query", {"query": soql}, source="ctx_index")
        return _require_salesforce_pull(result, "Salesforce Chatter")

    # FeedComment (replies) — full history, batched by the org's row ceiling
    for obj, body_field in (("FeedComment", "CommentBody"), ("FeedItem", "Body")):
        # FeedItem.Body is not filterable in Salesforce SOQL. Filtering it in
        # the query makes the entire Chatter pull fail with INVALID_FIELD even
        # though FeedComment.CommentBody accepts the same predicate. Empty
        # FeedItem bodies are already discarded by _clean() below.
        where = f" WHERE {body_field} != null" if obj == "FeedComment" else ""
        soql = (f"SELECT Id, CreatedBy.Name, {body_field}, ParentId, CreatedDate "
                f"FROM {obj}{where} ORDER BY CreatedDate DESC LIMIT 5000")
        recs = _sf_recs(await _pull(soql))
        for r in recs:
            if not isinstance(r, dict):
                continue
            body = _clean(r.get(body_field))
            if len(body) < 4:
                continue
            cby = r.get("CreatedBy") if isinstance(r.get("CreatedBy"), dict) else {}
            who = str((cby or {}).get("Name") or "")
            parent = str(r.get("ParentId") or "")
            when = str(r.get("CreatedDate") or "")
            text = f"{who} ({obj}) on {parent}: {body}"
            if _upsert(c, "sf_chatter", f"{obj}_{r.get('Id')}", when,
                       f"Chatter: {who} — {body[:70]}", who, text, url=f"sf:{parent}"):
                n += 1
    return n


def _index_sf_activities(c, recs, kind) -> int:
    """Index open Salesforce Tasks / Cases (action items directed at the owner) one doc each."""
    n = 0
    for r in recs:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("Id") or "")
        if not rid:
            continue
        if kind == "task":
            what = r.get("What") if isinstance(r.get("What"), dict) else {}
            who = r.get("Who") if isinstance(r.get("Who"), dict) else {}
            title = f"Task: {r.get('Subject') or '(no subject)'}"
            body = (f"Subject={r.get('Subject')} | Status={r.get('Status')} | Priority={r.get('Priority')} "
                    f"| Due={r.get('ActivityDate')} | Re={(what or {}).get('Name')} "
                    f"| Contact={(who or {}).get('Name')}")
            when = str(r.get("LastModifiedDate") or r.get("ActivityDate") or "")
        else:  # case
            title = f"Case {r.get('CaseNumber') or ''}: {r.get('Subject') or ''}".strip()
            body = (f"CaseNumber={r.get('CaseNumber')} | Status={r.get('Status')} "
                    f"| Priority={r.get('Priority')} | Subject={r.get('Subject')} "
                    f"| {str(r.get('Description') or '')[:600]}")
            when = str(r.get("LastModifiedDate") or "")
        if _upsert(c, "salesforce", f"{kind}_{rid}", when, title, kind, body, url=f"sf:{rid}"):
            n += 1
    return n


async def index_async_sources(c) -> dict:
    from auth.oauth_manager import OAuthManager
    from core.chat_registry import build_chat_tool_registry

    reg = build_chat_tool_registry(OAuthManager().get_token)
    stats: dict = {}

    for tool in ("littlebird_recent_notes", "littlebird_action_items", "littlebird_transcripts"):
        try:
            r = await reg.execute(tool, {}, source="ctx_index")
            stats[tool] = _index_text_blob(c, "littlebird", tool, str(r), title=tool)
        except Exception as e:
            stats[tool + "_err"] = str(e)[:120]

    try:
        recs = await _pull_sf_tickets_paged(reg)
        stats["salesforce"] = _index_salesforce(c, recs)
    except Exception as e:
        stats["salesforce_err"] = str(e)[:120]

    # Chatter (FeedItem + FeedComment) — deepest history the org retains.
    try:
        stats["sf_chatter"] = await _index_sf_chatter(c, reg.execute)
    except Exception as e:
        stats["sf_chatter_err"] = str(e)[:120]

    # Open SF Tasks + Cases owned by Alex (action items directed at him — feeds the open-item sweep too).
    try:
        _me = ("(SELECT Id FROM User WHERE Email IN "
               "('alex.democskonyi@valmont.com','alex.democskonyi@valmont.com'))")  # pii-allow: owner's own SF identity, needed to scope 'my open items'
        t_soql = ("SELECT Id, Subject, Status, Priority, ActivityDate, LastModifiedDate, What.Name, Who.Name "
                  f"FROM Task WHERE OwnerId IN {_me} AND IsClosed = false "
                  "ORDER BY LastModifiedDate DESC LIMIT 2000")
        task_result = await reg.execute("salesforce_soql_query", {"query": t_soql}, source="ctx_index")
        stats["sf_tasks"] = _index_sf_activities(
            c, _sf_recs(_require_salesforce_pull(task_result, "Salesforce tasks")), "task"
        )
    except Exception as e:
        stats["sf_tasks_err"] = str(e)[:120]
    try:
        _me = ("(SELECT Id FROM User WHERE Email IN "
               "('alex.democskonyi@valmont.com','alex.democskonyi@valmont.com'))")  # pii-allow: owner's own SF identity, needed to scope 'my open items'
        c_soql = ("SELECT Id, CaseNumber, Subject, Status, Priority, Description, LastModifiedDate "
                  f"FROM Case WHERE OwnerId IN {_me} AND IsClosed = false "
                  "ORDER BY LastModifiedDate DESC LIMIT 2000")
        case_result = await reg.execute("salesforce_soql_query", {"query": c_soql}, source="ctx_index")
        stats["sf_cases"] = _index_sf_activities(
            c, _sf_recs(_require_salesforce_pull(case_result, "Salesforce cases")), "case"
        )
    except Exception as e:
        stats["sf_cases_err"] = str(e)[:120]

    try:
        r = await reg.execute(
            "monday_get_salesforce_sprint_items", {"limit": 300, "include_closed": True},
            source="ctx_index",
        )
        rs = str(r)
        current_monday_item_ids = _monday_item_ids(rs)
        stats["monday"] = _index_monday_items(c, rs)
        # Heartbeat: body carries the run ts so its content-hash always changes and _upsert always
        # writes a fresh row (ts advances) on a genuinely successful pull. Per-item docs are
        # hash-deduped and legitimately stop changing when nothing on the board moved, so they can't
        # be used alone to prove the board isn't frozen — this doc can. GATED on a real board
        # payload: registry.execute NEVER raises (tool failures come back as an
        # 'Error executing ...'/'ERROR: ...' STRING), so an unconditional write here would keep the
        # canary green while the Monday integration is dead. Success always starts 'Board: '.
        if rs.startswith("Board:"):
            _upsert(c, "monday", "_fetch_ok", time.time(), "Monday fetch heartbeat", "monday",
                    f"ok {int(time.time())}")
            stats["monday_legacy_pruned"] = _prune_legacy_monday_sprint_refs(
                c, current_monday_item_ids,
            )
        else:
            stats["monday_err"] = rs[:120]
    except Exception as e:
        stats["monday_err"] = str(e)[:120]

    # Monday item UPDATES (comment threads on sprint items) — the discussion the sprint snapshot omits.
    try:
        stats["monday_updates"] = await _index_monday_updates(c, reg)
    except Exception as e:
        stats["monday_updates_err"] = str(e)[:120]

    return stats


async def _index_monday_updates(c, reg) -> int:
    """Index the update/comment thread on each open sprint item (bounded). monday_get_updates is
    per-item, so we walk the current sprint items' ids. Idempotent via _upsert hash."""
    n = 0
    try:
        items = await reg.execute("monday_get_salesforce_sprint_items",
                                   {"limit": 120, "include_closed": False}, source="ctx_index")
    except Exception:
        return 0
    text = items if isinstance(items, str) else json.dumps(items, default=str)
    ids = re.findall(r"\(ID:\s*(\d+)\)", text)[:80]  # bounded: at most 80 items/cycle
    for iid in ids:
        try:
            up = await reg.execute("monday_get_updates", {"item_id": iid, "limit": 20}, source="ctx_index")
        except Exception:
            continue
        body = up if isinstance(up, str) else json.dumps(up, default=str)
        if not body or len(body.strip()) < 20 or "no update" in body.lower()[:40]:
            continue
        if _upsert(c, "monday", f"updates_{iid}", time.time(),
                   f"Monday updates · item {iid}", "monday_updates", body[:8000], url=f"monday:{iid}"):
            n += 1
    return n


# --- Azure DevOps (ValmontInd/Irrigation) ---------------------------------------------
# Successor feed to the Monday board (migrated 2026-08-10). Monday ingest stays wired until
# Alex retires it; these are independent sources and neither gates the other.
_ADO_WATERMARK_KEY = "ado_changed_watermark"
_ADO_COMMENT_COUNTS_KEY = "ado_comment_counts"
# Re-pull a small overlap: WIQL ChangedDate is server-side and an item saved during the
# previous sweep can land microseconds behind the watermark we stored.
_ADO_OVERLAP_S = 900


def _index_state_get(c, key: str) -> str:
    try:
        row = c.execute("SELECT value FROM index_state WHERE key=?", (key,)).fetchone()
    except Exception:
        return ""
    return str(row[0]) if row else ""


def _index_state_set(c, key: str, value: str) -> None:
    c.execute(
        "INSERT INTO index_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _ado_doc(wi: dict, ado) -> tuple:
    """(ref, ts, title, participants, body, url) for one work item."""
    f = wi.get("fields") or {}
    wid = wi.get("id") or f.get("System.Id")
    wtype = str(f.get("System.WorkItemType") or "Work item")
    title = str(f.get("System.Title") or "").strip()
    people = {
        "Assigned to": ado.display_name(f.get("System.AssignedTo")),
        "Created by": ado.display_name(f.get("System.CreatedBy")),
        "Last changed by": ado.display_name(f.get("System.ChangedBy")),
    }
    header = [
        f"{wtype} {wid}: {title}",
        f"State: {f.get('System.State') or ''} ({f.get('System.Reason') or ''})",
        f"Area: {f.get('System.AreaPath') or ''}",
        f"Iteration: {f.get('System.IterationPath') or ''}",
    ]
    for label, value in people.items():
        if value:
            header.append(f"{label}: {value}")
    for label, key in (("Tags", "System.Tags"),
                       ("Priority", "Microsoft.VSTS.Common.Priority"),
                       ("Story points", "Microsoft.VSTS.Scheduling.StoryPoints"),
                       ("Parent", "System.Parent")):
        if f.get(key) not in (None, ""):
            header.append(f"{label}: {f.get(key)}")
    header.append(f"Changed: {str(f.get('System.ChangedDate') or '')[:19]}")
    sections = ["\n".join(header)]
    for label, key in (("Description", "System.Description"),
                       ("Repro steps", "Microsoft.VSTS.TCM.ReproSteps"),
                       ("Acceptance criteria", "Microsoft.VSTS.Common.AcceptanceCriteria")):
        text = ado.html_to_text(f.get(key))
        if text:
            sections.append(f"{label}:\n{text}")
    participants = " | ".join(v for v in people.values() if v) + " | " + str(f.get("System.Tags") or "")
    return (
        f"wi_{wid}",
        _to_epoch(f.get("System.ChangedDate")),
        f"[{wtype} {wid}] {title}"[:180],
        participants.strip(" |"),
        "\n\n".join(sections)[:12000],
        ado.item_url(wid),
    )


def index_ado(c, comment_budget: int = 80, max_items: int = 4000) -> int:
    """Azure DevOps work items + comment threads for Alex's lane, incrementally.

    Two passes on purpose:
      1. a CHEAP metadata sweep over every in-scope id (id/ChangedDate/CommentCount only,
         ~7 batch calls for the measured 691 items) — this is what makes the refresh
         incremental AND is the only way to see new comments, because posting a comment
         does NOT create a work-item revision, so System.ChangedDate never moves for it.
         A ChangedDate-only watermark would silently miss every discussion.
      2. a DEEP pull of full fields for the items whose ChangedDate crossed the watermark,
         plus comment fetches only where CommentCount actually changed.
    """
    from tools import ado_client as ado

    n = 0
    watermark = _index_state_get(c, _ADO_WATERMARK_KEY)
    try:
        counts = json.loads(_index_state_get(c, _ADO_COMMENT_COUNTS_KEY) or "{}")
    except Exception:
        counts = {}
    cutoff = (_to_epoch(watermark) - _ADO_OVERLAP_S) if watermark else 0.0

    with ado.AdoBridge() as bridge:
        ids = ado.changed_ids(bridge)[:max_items]
        if not ids:
            return 0
        meta = ado.work_items(bridge, ids, fields=[
            "System.Id", "System.ChangedDate", "System.CommentCount"])
        newest = cutoff
        deep_ids, comment_ids = [], []
        for wi in meta:
            f = wi.get("fields") or {}
            wid = wi.get("id") or f.get("System.Id")
            changed = _to_epoch(f.get("System.ChangedDate"))
            newest = max(newest, changed)
            if changed >= cutoff:
                deep_ids.append(wid)
            cc = int(f.get("System.CommentCount") or 0)
            if cc and counts.get(str(wid)) != cc:
                comment_ids.append((wid, cc, changed))
        # Newest-first so a backlog of comment work drains from what matters now.
        comment_ids.sort(key=lambda x: x[2], reverse=True)
        items = ado.work_items(bridge, deep_ids) if deep_ids else []
        by_id = {}
        for wi in items:
            ref, ts, title, participants, body, url = _ado_doc(wi, ado)
            by_id[wi.get("id")] = title
            if _upsert(c, "ado", ref, ts, title, participants, body, url=url, has_body=1):
                n += 1
        for wid, cc, changed in comment_ids[:comment_budget]:
            try:
                thread = ado.comments(bridge, wid)
            except Exception:
                continue
            lines = []
            for cm in thread:
                who = ado.display_name(cm.get("createdBy"))
                when = str(cm.get("createdDate") or "")[:19]
                text = ado.html_to_text(cm.get("text"))
                if text:
                    lines.append(f"{when} {who}: {text}")
            if lines and _upsert(
                    c, "ado", f"comments_{wid}", changed or time.time(),
                    f"ADO comments · {by_id.get(wid, wid)}"[:180],
                    "ado_comments", "\n\n".join(lines)[:12000],
                    url=ado.item_url(wid), has_body=1):
                n += 1
            counts[str(wid)] = cc

    # Heartbeat: per-item docs are hash-deduped and legitimately stop changing on a quiet
    # board, so they cannot prove the feed is alive. This doc's body carries the run stamp,
    # and it is only written after a pull that really returned items.
    _upsert(c, "ado", "_fetch_ok", time.time(), "Azure DevOps fetch heartbeat", "ado",
            f"ok {int(time.time())} in_scope={len(ids)} deep={len(deep_ids)}")
    # Prune the count map to the current scope so a retired item can't grow it forever.
    counts = {k: v for k, v in counts.items() if int(k) in set(int(i) for i in ids)}
    _index_state_set(c, _ADO_COMMENT_COUNTS_KEY, json.dumps(counts, separators=(",", ":")))
    if newest:
        from datetime import datetime as _dt, timezone as _tz
        _index_state_set(c, _ADO_WATERMARK_KEY,
                         _dt.fromtimestamp(newest, _tz.utc).strftime(
                             "%Y-%m-%dT%H:%M:%S.0000000Z"))
    return n


def index_littlebird_meetings(c, max_meetings=None) -> int:
    """Index the atomically published Littlebird work digest, never the live browser.

    The sync coordinator has already completed the authenticated raw-CDP read and
    freshness gate before it calls scoped ingest. Reopening the bridge here used
    a second competing CDP/IndexedDB read, which could hold the sync lock for
    minutes after an otherwise successful update. The JSON digest is its atomic
    commit marker, so this read sees either the last complete snapshot or the
    new complete snapshot -- never a partial publish.

    HARD (Alex 2026-09-26): always sort newest-first before the cap. An unsorted
    meetings[:300] silently dropped fresh notes from refresh while older IDs
    stayed indexed — every meeting note must land in work brain.
    """
    from datetime import datetime as _dt
    digest = DIGESTS_DIR / "LITTLEBIRD_NOTES.json"
    try:
        # Transcripts ride in this file. 8MB rejected a full pull and the
        # indexer then reported a clean 0, which looks like "nothing new".
        if digest.stat().st_size <= 0 or digest.stat().st_size > 96_000_000:
            return 0
        d = json.loads(digest.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(d, dict):
        return 0
    meetings = d.get("meetings")
    journals = d.get("journals")
    if not isinstance(meetings, list) or not isinstance(journals, list):
        return 0
    try:
        env_max = int(os.environ.get("WCI_LITTLEBIRD_MAX", "4000"))
    except (TypeError, ValueError):
        env_max = 4000
    try:
        want = env_max if max_meetings is None else int(max_meetings)
    except (TypeError, ValueError):
        want = env_max
    limit = max(0, min(want, env_max, 8000))

    def _when_key(row: dict) -> str:
        for k in ("sortAt", "lastTranscribedAt", "last_transcribed_at", "createdAt", "created_at"):
            v = row.get(k)
            if v:
                return str(v)
        return ""

    prohibited = re.compile(r"\b(?:github|fullsand|tavantfull)\b", re.I)
    eligible = [
        row for row in meetings
        if isinstance(row, dict)
        and row.get("id")
        and (os.environ.get("WCI_LB_PROHIBITED_FILTER", "0") != "1"  # Alex 2026-10-16: off by default
             or not prohibited.search(f"{row.get('name', '')} {row.get('tldr', '')}"))
    ]
    eligible.sort(key=_when_key, reverse=True)
    meeting_rows = eligible[:limit]
    journal_rows = [row for row in journals if isinstance(row, dict)]
    try:
        from tools.littlebird_meeting_enricher import load_enrichments, valid_enrichment
        enrichments = load_enrichments()
        enriched_rows = {
            str(row.get("id") or ""): item
            for row in meeting_rows
            if (item := valid_enrichment(row, enrichments))
        }
    except Exception:
        enriched_rows = {}

    # The bridge publishes a new transport timestamp every healthy poll even
    # when the underlying notes are unchanged. Fingerprint only the semantic
    # rows that this function indexes, so an unchanged four-minute poll is a
    # constant-time no-op instead of thousands of FTS chunk lookups.
    state_key = f"littlebird_published_digest_v2:{limit}"
    h = hashlib.sha256()
    for row in meeting_rows:
        h.update(
            f"{row.get('id')}|{row.get('sortAt')}|{len(str(row.get('tldr') or ''))}|"
            f"{len(str(row.get('summary') or ''))}|{len(str(row.get('transcript') or ''))}\n".encode()
        )
    for row in journal_rows:
        h.update(f"j|{row.get('id')}|{len(str(row.get('text') or ''))}\n".encode())
    h.update(str(sorted(enriched_rows)).encode())
    fingerprint = h.hexdigest()
    try:
        state = c.execute("SELECT value FROM index_state WHERE key=?", (state_key,)).fetchone()
        if state and state[0] == fingerprint:
            return 0
    except Exception:
        # Unit callers without a SQLite connection can still exercise the
        # deterministic content transform; production always has index_state.
        state_key = ""

    def _ep(s):
        try: return _dt.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
        except Exception: return 0.0
    n = 0
    for m in meeting_rows:
        att = ", ".join(str(a) for a in (m.get("attendees") or [])[:20])
        extra = enriched_rows.get(str(m.get("id") or ""), {})
        summary = str(m.get("summary") or "").strip()
        if not summary and extra:
            sections = [str(extra.get("overview") or "").strip()]
            for label, key in (
                ("Decisions", "decisions"),
                ("Action items", "action_items"),
                ("Open questions", "open_questions"),
                ("Key details", "key_details"),
            ):
                values = [str(value).strip() for value in (extra.get(key) or []) if str(value).strip()]
                if values:
                    sections.append(f"{label}:\n" + "\n".join(f"- {value}" for value in values))
            summary = "\n\n".join(section for section in sections if section)
        text = (f"Meeting: {m.get('name','')}\nDate: {str(m.get('sortAt'))[:16]}\nSource: {m.get('sourceApp','')}\n"
                f"Attendees: {att}\n\nTLDR: {m.get('tldr','')}\n\nSummary:\n{m.get('summary','')}\n\n"
                f"Transcript:\n{m.get('transcript','')}").strip()
        if summary != str(m.get("summary") or ""):
            text = (f"Meeting: {m.get('name','')}\nDate: {str(m.get('sortAt'))[:16]}\n"
                    f"Source: {m.get('sourceApp','')}\nAttendees: {att}\n\n"
                    f"TLDR: {m.get('tldr','')}\n\nSummary:\n{summary}\n\n"
                    f"Transcript:\n{m.get('transcript','')}").strip()
        n += _index_text_blob(c, "littlebird", f"meeting_{m.get('id')}", text,
                              ts=_ep(m.get("sortAt")), title=m.get("name") or "Meeting")
        if n and n % 25 == 0:
            c.commit()
    for j in journal_rows:
        _index_text_blob(c, "littlebird", f"journal_{j.get('id')}", j.get("text", ""),
                         ts=_ep(j.get("date")), title="Daily work summary " + str(j.get("date"))[:10])
    if state_key:
        c.execute(
            "INSERT INTO index_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (state_key, fingerprint),
        )
    return n


_ZOOM_WORKBRAIN_EXCLUDED_THREAD_SIDS = frozenset({
    # Alex's private operational draft feed, not human work context.
    "88047d44b6de43a09302ce45a85ba06b@conference.xmpp.zoom.us",
})


def index_zoom(c) -> int:
    """Index Zoom as a first-class brain source: every Team Chat thread (full DM/channel
    conversations from ZOOM_TEAM_CHAT_THREADS.json) + meeting summaries + calendar. Idempotent."""
    n = 0
    threads_path = DIGESTS_DIR / "ZOOM_TEAM_CHAT_THREADS.json"
    if threads_path.exists():
        try:
            threads = json.loads(threads_path.read_text(errors="ignore"))
        except Exception:
            threads = {}
        for key, th in (threads.items() if isinstance(threads, dict) else []):
            if not isinstance(th, dict):
                continue
            if key in _ZOOM_WORKBRAIN_EXCLUDED_THREAD_SIDS:
                _delete_index_prefix(c, "zoom", f"thread_{key}#")
                continue
            name = th.get("name") or key
            kind = th.get("kind") or "chat"
            msgs = th.get("messages") or []
            convo = "\n".join(f"{m.get('from','?')}: {m.get('text','')}" for m in msgs if m.get("text"))
            if not convo.strip():
                continue
            text = f"Zoom {kind} with {name} ({len(msgs)} messages):\n{convo}"
            ts = (th.get("lastT") or 0) / 1000.0
            n += _index_text_blob(c, "zoom", f"thread_{key}", text, ts=ts, title=f"Zoom: {name}")
        # The generic text helper stores ``zoom:thread_<jid>``.  Exact source rereads
        # intentionally accept only ``zoom:<jid>``; repair the owned local index metadata
        # even when message content is unchanged and the hash-deduped upsert did not
        # rewrite the row.
        # ONE guarded post-loop statement, not one per thread: source/ref are FTS5
        # UNINDEXED, so each in-loop UPDATE was a full SCAN of ~105k rows / 306 MB of body
        # text -- 156 threads x 96 runs/day dragged ~47 GB through the page cache per cycle,
        # and the unguarded WHERE rewrote all 728 already-correct rows every single pass.
        # IFNULL is load-bearing: a plain `url <> expr` evaluates to NULL (not TRUE) for a
        # NULL url, silently skipping exactly the broken rows this exists to fix.
        # Post-loop also reaches the 79 docs on 69 DEAD threads the loop never visits --
        # they carried ``zoom:thread_<jid>`` and were unreachable by the exact-source reread.
        c.execute(
            r"UPDATE docs SET url = 'zoom:'||substr(ref,8,instr(ref,'#')-8) "
            r"WHERE source='zoom' AND ref LIKE 'thread\_%#%' ESCAPE '\' "
            r"AND IFNULL(url,'') <> 'zoom:'||substr(ref,8,instr(ref,'#')-8)"
        )
    # meeting summaries + calendar (first-class under zoom; also swept generically by index_digests)
    for fname, label in (("ZOOM_MEETING_SUMMARIES.md", "Zoom meeting summaries"), ("ZOOM_CALENDAR.md", "Zoom calendar")):
        p = DIGESTS_DIR / fname
        if p.exists():
            try:
                txt = p.read_text(errors="ignore")
            except Exception:
                continue
            ts = p.stat().st_mtime
            for i, (head, chunk) in enumerate(_chunk_markdown(txt, 1800)):
                if _upsert(c, "zoom", f"{fname}#{i}", ts, f"{label}" + (f": {head}" if head else ""), fname, chunk, url=f"zoom:{fname}"):
                    n += 1
    return n


def index_zoom_ai_summaries(c, max_emails=800) -> int:
    """Index Zoom AI Companion summaries — they arrive as 'Meeting assets ... are ready!' emails
    from no-reply@zoom.us (body carries the summary). Pulled via OWA, indexed as source='zoom'
    so meeting summaries are first-class + always captured. Idempotent."""
    from datetime import datetime as _dt
    # Goes through _owa_json on purpose. This was the last OWA caller still doing its own
    # subprocess + json.loads, so it never received the "a refusal is never data" fix -- it
    # iterated the {"ok":false,...} envelope as if it were a list of messages, got the dict's
    # KEYS, and died on `'str' object has no attribute 'get'` every cycle the bridge was idle.
    items = _owa_json(["zoom-summaries", "--top", str(max_emails)])
    if not isinstance(items, list):
        return 0        # None = busy/deferred. Anything else is an envelope, never records.
    def _ep(s):
        try: return _dt.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
        except Exception: return 0.0
    n = 0
    for m in items:
        body = (m.get("body") or "").strip()
        if len(body) < 40:
            continue
        n += _index_text_blob(c, "zoom", f"ai_summary_{m.get('id')}", body,
                              ts=_ep(m.get("received")), title="Zoom AI summary: " + (m.get("subject") or "")[:80])
    return n


def reindex(body_budget=80, async_sources=True) -> dict:
    """Full reindex. Wait out an in-flight draft first, then take the writer lock."""
    with draft_yield_budget():
        yield_while_draft()
        return _reindex_locked(body_budget, async_sources)


@_serialized_index_write
def _reindex_locked(body_budget=80, async_sources=True) -> dict:
    return _reindex_body(body_budget, async_sources)


def _reindex_body(body_budget=80, async_sources=True) -> dict:
    c = _conn()
    _ensure_schema(c)
    stats: dict = {}
    try:
        stats["zoom"] = index_zoom(c)
        c.commit()
    except Exception as e:
        stats["zoom_err"] = str(e)[:160]
    try:
        stats["zoom_ai_summaries"] = index_zoom_ai_summaries(c)
        c.commit()
    except Exception as e:
        stats["zoom_ai_err"] = str(e)[:160]
    try:
        stats["email_meta"] = index_email_meta(c)
        c.commit()
    except Exception as e:
        stats["email_meta_err"] = str(e)[:160]
    try:
        stats["littlebird_meetings"] = index_littlebird_meetings(c)
        c.commit()
    except Exception as e:
        stats["littlebird_err"] = str(e)[:160]
    try:
        stats["digests"] = index_digests(c)
        c.commit()
        # Full reindexing is also a real digest ingest. Without a per-source
        # stamp the sentinel can report this healthy path as dead indefinitely.
        stats["ingest_ts_digests"] = time.time()
    except Exception as e:
        stats["digests_err"] = str(e)[:160]
    try:
        stats["ado"] = index_ado(c)
        c.commit()
        stats["pull_ts_ado"] = time.time()
    except Exception as e:
        stats["ado_err"] = str(e)[:160]
    if async_sources:
        try:
            async_stats = asyncio.run(index_async_sources(c))
            stats["async"] = async_stats
            c.commit()
            async_errors = sorted(
                str(key) for key in async_stats if str(key).endswith("_err")
            )
            if async_errors:
                stats["async_err"] = "failed sources: " + ", ".join(async_errors)
            else:
                pulled_at = time.time()
                if "monday" in async_stats:
                    stats["pull_ts_monday"] = pulled_at
                if all(key in async_stats for key in ("salesforce", "sf_tasks", "sf_cases")):
                    stats["pull_ts_salesforce"] = pulled_at
                if "sf_chatter" in async_stats:
                    stats["pull_ts_sf_chatter"] = pulled_at
        except Exception as e:
            stats["async_err"] = str(e)[:160]
    try:
        stats["email_bodies"] = index_email_bodies(c, body_budget)
        c.commit()
    except Exception as e:
        stats["email_bodies_err"] = str(e)[:160]
    try:
        stats["email_attachments"] = index_email_attachments(c)
        c.commit()
    except Exception as e:
        stats["email_attachments"] = 0
        stats["email_attachments_note"] = str(e)[:160]
    try:
        stats["total_docs"] = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    except Exception:
        pass
    blocking = [
        k for k in stats
        if str(k).endswith("_err") and not str(k).startswith(("email_attachments", "ado_"))
    ]
    if not blocking:
        # Scoped ingests must never advance this; it proves a completed full
        # reindex rather than merely any heartbeat-file write.
        stats["reindex_ts"] = time.time()
    c.close()
    try:
        # MERGE, don't overwrite: a wholesale write drops every ingest_ts_<src> stamp, and
        # work_brain_sentinel falls back to the global (just-refreshed) ingest_ts when a per-source
        # stamp is missing — so a dead feed would read as alive from the next reindex onward.
        _merge_health(stats)
    except Exception:
        pass
    return stats


HEALTH_PATH = BASE_DIR / "data" / "work_context_health.json"
# Per-source max-staleness (days) before the source is flagged. Only event-ts sources are canaries;
# salesforce ticket docs carry index-time ts so they're not staleness-meaningful (skip). "monday" IS
# a canary via the _fetch_ok heartbeat doc (ts refreshed every successful sprint pull regardless of
# whether any item content changed) — the per-item docs alone are hash-deduped and can legitimately
# go stale, so the heartbeat is what makes a frozen board detectable.
_STALE_DAYS = {"email": 3.0, "zoom": 7.0, "littlebird": 7.0, "digest": 2.0, "sf_chatter": 5.0, "monday": 1.5, "ado": 1.5}


@contextmanager
def _health_write_lock():
    """Serialize cross-process heartbeat merges without blocking readers."""
    import fcntl

    lock_path = HEALTH_PATH.with_name(f"{HEALTH_PATH.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def health() -> dict:
    """CHEAP ($0, deterministic) freshness snapshot: per-source doc count + newest ts + staleness flag,
    plus last-reindex age. No LLM, no network — just the local SQLite + heartbeat file. Lets the stack
    (watchdogs / a status probe) SEE when a single source silently froze even while others refresh."""
    now = time.time()
    out: dict = {"now": now, "db": str(DB_PATH), "sources": {}, "stale": [], "errors": []}
    try:
        c = _read_conn()
        for src, n, mx in c.execute("SELECT source, COUNT(*), MAX(ts) FROM docs GROUP BY source"):
            newest = _to_epoch(mx)
            age_days = (now - newest) / 86400 if newest else None
            flag = bool(src in _STALE_DAYS and (age_days is None or age_days > _STALE_DAYS[src]))
            out["sources"][src] = {
                "docs": n, "newest_ts": newest,
                "newest": time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)) if newest else "",
                "age_days": round(age_days, 2) if age_days is not None else None,
                "stale": flag,
            }
            if flag:
                out["stale"].append(src)
        out["total_docs"] = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        c.close()
    except Exception as e:
        out["errors"].append(f"db:{str(e)[:100]}")
    try:
        hb = json.loads(HEALTH_PATH.read_text())
        hb_stats = hb.get("stats") or {}
        # `ts` is retained as a one-cycle migration fallback for heartbeat
        # files created before reindex_ts existed. New scoped merges preserve
        # reindex_ts and cannot make a stale full reindex appear fresh.
        reindex_ts = hb_stats.get("reindex_ts") or hb.get("ts")
        out["last_reindex_ts"] = reindex_ts
        out["last_reindex_age_min"] = (
            round((now - float(reindex_ts)) / 60, 1) if reindex_ts else None
        )
        out["last_errors"] = [k for k in (hb.get("stats") or {}) if k.endswith("_err")]
    except Exception:
        out["last_reindex_ts"] = None
    return out


def _write_health_unlocked(stats: dict) -> None:
    """Replace the heartbeat atomically while the caller holds its writer lock."""
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot = health()
    # health() reads the previously persisted heartbeat, so its last_errors
    # field otherwise lags the stats being written here by one run.
    snapshot["last_errors"] = [
        key for key in (stats or {}) if str(key).endswith("_err")
    ]
    # Same lag, same fix. The reindex stamp was left reading the PREVIOUS heartbeat, so a
    # run that just completed still published the old age -- work_brain_sentinel held that
    # to its 90-minute budget and stayed RED for a full cycle after the repair that fixed
    # it (2026-08-24: Salesforce restored, errors cleared, sentinel still red at 313 min).
    if (stats or {}).get("reindex_ts"):
        snapshot["last_reindex_ts"] = stats["reindex_ts"]
        snapshot["last_reindex_age_min"] = round(
            (time.time() - float(stats["reindex_ts"])) / 60, 1)
    payload = json.dumps({
        "ts": time.time(),
        "stats": {k: v for k, v in (stats or {}).items()},
        "snapshot": snapshot,
    }, default=str)
    tmp_path = HEALTH_PATH.with_name(f".{HEALTH_PATH.name}.{os.getpid()}.tmp")
    tmp_path.write_text(payload)
    os.replace(tmp_path, HEALTH_PATH)


def _write_health(stats: dict) -> None:
    """Persist a heartbeat after each reindex: last-run ts + the raw stats (incl any *_err keys)."""
    with _health_write_lock():
        _write_health_unlocked(stats)


_RECOVERED_ERROR_ALIASES = {
    "zoom_ai_summaries": ("zoom_ai_err",),
    "littlebird_meetings": ("littlebird_err",),
}


def _merge_health(stats: dict) -> None:
    """Like _write_health but MERGES into the existing stats blob. Scoped ingests touch only one
    source, so we must not clobber the other sources' last-known counts in the heartbeat file.
    (The `snapshot` is always recomputed live from SQLite, so it stays fully accurate regardless.)"""
    # Scoped feeds run independently and often finish within the same second.
    # Lock the entire read-modify-replace so a slower writer cannot resurrect
    # errors or watermarks from the stale snapshot it read before another feed.
    with _health_write_lock():
        prev = {}
        try:
            prev = (json.loads(HEALTH_PATH.read_text()) or {}).get("stats", {}) or {}
        except Exception:
            prev = {}
        # A successful retry must clear the matching transient failure. Full
        # reindex and scoped retries historically used different error names
        # (for example, zoom_ai_err vs zoom_ai_summaries), so clear both forms.
        for key in (stats or {}):
            if not str(key).endswith("_err"):
                for error_key in (f"{key}_err", *_RECOVERED_ERROR_ALIASES.get(str(key), ())):
                    prev.pop(error_key, None)
        prev.update(stats or {})
        # Per-source ingest stamp. `ingest_ts`/`ingest_source` are single keys that whichever feed ran
        # LAST overwrites, so one dead lane is invisible behind three healthy ones — the heartbeat keeps
        # looking 2 minutes old while e.g. zoom has not ingested in a day. Keying by source is what lets
        # work_brain_sentinel.py hold each feed to its OWN cadence.
        src = str((stats or {}).get("ingest_source") or "").strip()
        ts = (stats or {}).get("ingest_ts")
        if src and isinstance(ts, (int, float)):
            prev[f"ingest_ts_{src}"] = float(ts)
        _write_health_unlocked(prev)


# Which index fn(s) each feed maps to. Scoped ingest runs ONLY these against a fresh conn, so a
# sync script can push new content to the work brain the moment it lands (sub-minute latency)
# without paying the full ~3-min reindex or contending for the launchd reindex lock.
_INGEST_SOURCES = ("littlebird", "zoom", "email", "digests", "ado")


def ingest(source: str, body_budget: int = 40) -> dict:
    """Source-scoped incremental reindex. Email yields while a draft is in flight, then still finishes."""
    src = (source or "").strip().lower()
    if src in ("lb",):
        src = "littlebird"
    if src not in _INGEST_SOURCES:
        raise SystemExit(f"unknown ingest source: {source!r}; use {'|'.join(_INGEST_SOURCES)}")
    if src != "email":
        return _ingest_locked(src, body_budget)
    try:
        body_budget = int(os.environ.get("WCI_EMAIL_BODY_BUDGET", str(body_budget)))
    except Exception:
        pass
    try:
        batches = max(1, int(os.environ.get("WCI_EMAIL_BODY_BATCHES", "12")))
    except Exception:
        batches = 12
    merged: dict = {"ingest_source": "email"}
    indexed_ok = True
    with draft_yield_budget() as ybox:
        yield_while_draft()
        meta_token = None
        try:
            fetched_meta = _fetch_email_meta_rows()
        except Exception:
            fetched_meta = _WCI_UNSET
        if fetched_meta is not _WCI_UNSET:
            meta_rows = fetched_meta if isinstance(fetched_meta, list) else []
            meta_token = _prefetched_meta.set(meta_rows)
        try:
            merged.update(_ingest_locked("email", body_budget, email_phase="meta", write_health=False))
        finally:
            if meta_token is not None:
                _prefetched_meta.reset(meta_token)
        if "email_meta_err" in merged:
            indexed_ok = False
        _yield_to_waiters()
        body_total = 0
        for _batch in range(batches):
            yield_while_draft()
            body_token = None
            try:
                body_payload = _fetch_email_body_payload(body_budget)
            except Exception as exc:
                merged["email_bodies_err"] = str(exc)[:160]
                indexed_ok = False
                break
            if body_payload is not None:
                body_token = _prefetched_body.set(body_payload)
            try:
                part = _ingest_locked("email", body_budget, email_phase="bodies", write_health=False)
            finally:
                if body_token is not None:
                    _prefetched_body.reset(body_token)
            if "email_bodies_err" in part:
                merged["email_bodies_err"] = part["email_bodies_err"]
                indexed_ok = False
                break
            got = int(part.get("email_bodies") or 0)
            body_total += got
            merged["total_docs"] = part.get("total_docs", merged.get("total_docs"))
            _yield_to_waiters()
            if got <= 0 or _owa_writer_active():
                break
        merged["email_bodies"] = body_total
        yield_while_draft()
        part = _ingest_locked("email", body_budget, email_phase="attachments", write_health=False)
        if "email_attachments" in part:
            merged["email_attachments"] = part["email_attachments"]
        if "email_attachments_note" in part:
            merged["email_attachments_note"] = part["email_attachments_note"]
        if part.get("total_docs") is not None:
            merged["total_docs"] = part["total_docs"]
        merged["draft_yield"] = {
            "waited_s": round(ybox["waited"], 3),
            "sleeps": ybox["sleeps"],
            "still_busy": bool(ybox["still_busy"]),
        }
    if indexed_ok:
        merged["ingest_ts"] = time.time()
    try:
        _merge_health(merged)
    except Exception:
        pass
    return merged


@_serialized_index_write
def _ingest_locked(src: str, body_budget: int = 40, email_phase: str | None = None, write_health: bool = True) -> dict:
    """One locked ingest slice. Email phases are meta, bodies, or attachments."""
    c = _conn()
    _ensure_schema(c)
    stats: dict = {"ingest_source": src}
    indexed_ok = True
    _writer_progress(f"{src}:{email_phase}" if email_phase else src)

    def _run(key, fn):
        nonlocal indexed_ok
        try:
            stats[key] = fn()
            c.commit()
            _writer_progress()
        except Exception as e:  # pragma: no cover
            indexed_ok = False
            stats[key + "_err"] = str(e)[:160]

    if src == "littlebird":
        _run("littlebird_meetings", lambda: index_littlebird_meetings(c))
    elif src == "zoom":
        _run("zoom", lambda: index_zoom(c))
        _run("zoom_ai_summaries", lambda: index_zoom_ai_summaries(c))
        # opportunistic $0 gap-check on the same pass — surfaces meetings missing a real summary.
        try:
            stats["reconcile"] = reconcile_zoom_summaries(alert=True)
        except Exception as e:  # pragma: no cover
            stats["reconcile_err"] = str(e)[:160]
    elif src == "email":
        phase = email_phase or "all"
        if phase in ("all", "meta"):
            _run("email_meta", lambda: index_email_meta(c))
        if phase in ("all", "bodies"):
            # One batch when the caller yields between batches. "all" drains here.
            try:
                body_batches = 1 if email_phase == "bodies" else int(os.environ.get("WCI_EMAIL_BODY_BATCHES", "12"))
            except Exception:
                body_batches = 1 if email_phase == "bodies" else 12
            _run("email_bodies", lambda: index_email_bodies(c, body_budget, max_batches=body_batches))
        if phase in ("all", "attachments"):
            try:
                stats["email_attachments"] = index_email_attachments(c)
                c.commit()
            except Exception as e:  # pragma: no cover
                stats["email_attachments"] = 0
                stats["email_attachments_note"] = str(e)[:160]
        if "email_meta_err" in stats or "email_bodies_err" in stats:
            indexed_ok = False
        else:
            indexed_ok = True
    elif src == "digests":
        _run("digests", lambda: index_digests(c))
    elif src == "ado":
        try:
            stats["ado"] = index_ado(c)
            c.commit()
        except Exception as e:  # pragma: no cover
            stats["ado"] = 0
            stats["ado_note"] = str(e)[:160]
        indexed_ok = "ado_err" not in stats
    try:
        stats["total_docs"] = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    except Exception:
        pass
    c.close()
    if not write_health:
        return stats
    if indexed_ok:
        # This is a completed source-scoped index pass, not merely an attempt.
        # _merge_health turns it into the durable per-source watermark.
        stats["ingest_ts"] = time.time()
    try:
        _merge_health(stats)
    except Exception:
        pass
    return stats


_ZOOM_SUMMARY_STUB = re.compile(r"^\s*(no match|n/?a|none|—|-|tbd)\s*$", re.I)
# Recurring / 1:1 meetings that legitimately never get a host "assets are ready" AI summary —
# excluded from the "uncovered" gap list so it only flags real content meetings.
_ZOOM_SKIP_TOPIC = re.compile(r"stand\s?up|1\s?[x:]\s?1|weekly 1|alex\s*<>|<>|check ?in$", re.I)


def _norm_topic(t: str) -> set:
    return {w for w in re.split(r"[^a-z0-9]+", (t or "").lower()) if len(w) > 2}


def reconcile_zoom_summaries(max_age_days: float = 21.0, alert: bool = False) -> dict:
    """Deterministic ($0, no LLM/network) cross-check that every Zoom meeting the history feed knows
    about actually has a real summary body in the work brain. Two gap classes:
      • stub_summaries  — a summary block exists but its body is 'NO MATCH'/empty (asset never landed)
      • uncovered       — a past, non-recurring calendar meeting with no summary block at all
    Parses ZOOM_MEETING_SUMMARIES.md + ZOOM_CALENDAR.md (the digests the syncs already write)."""
    import datetime as _dt
    now = time.time()
    cutoff = now - max_age_days * 86400
    summ_path = DIGESTS_DIR / "ZOOM_MEETING_SUMMARIES.md"
    cal_path = DIGESTS_DIR / "ZOOM_CALENDAR.md"

    # --- parse summary blocks: "## <date> <time> — <topic>\n<body...>"
    blocks: list[dict] = []
    if summ_path.exists():
        cur = None
        for ln in summ_path.read_text(errors="ignore").splitlines():
            m = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})[^\n—-]*—\s*(.+?)\s*$", ln)
            if m:
                cur = {"date": m.group(1), "topic": m.group(2), "body": []}
                blocks.append(cur)
            elif cur is not None:
                cur["body"].append(ln)
    summ_dates = set()
    stub = []
    for b in blocks:
        summ_dates.add(b["date"])
        body = "\n".join(x for x in b["body"] if x.strip() and not x.startswith("**Next steps:**")).strip()
        if not body or _ZOOM_SUMMARY_STUB.match(body):
            stub.append({"when": b["date"], "topic": b["topic"][:80]})

    # --- parse calendar past meetings: "- <date> <time> · <topic> · <dur>"
    uncovered = []
    if cal_path.exists():
        seen = set()
        for ln in cal_path.read_text(errors="ignore").splitlines():
            m = re.match(r"^-\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+·\s+(.+?)\s+·", ln)
            if not m:
                continue
            date, hm, topic = m.group(1), m.group(2), m.group(3)
            try:
                ev = _dt.datetime.fromisoformat(f"{date} {hm}").timestamp()
            except Exception:
                continue
            if ev > now or ev < cutoff:        # only recently-past meetings
                continue
            if _ZOOM_SKIP_TOPIC.search(topic):  # recurring / 1:1 — no AI summary expected
                continue
            key = (date, topic.lower())
            if key in seen:
                continue
            seen.add(key)
            # covered if a summary block shares the date OR strong topic-token overlap
            toks = _norm_topic(topic)
            covered = date in summ_dates or any(
                len(toks & _norm_topic(b["topic"])) >= 2 for b in blocks)
            if not covered:
                uncovered.append({"when": f"{date} {hm}", "topic": topic[:80]})

    # `stub_summaries` is the high-signal gap the user cares about: the feed HAS the meeting but the
    # real body never landed (NO MATCH/empty). `uncovered` is a broader, noisier cross-check (past
    # meetings with no summary at all — many legitimately never get a Zoom AI summary) — kept as data
    # only, NEVER paged, so the alert stays actionable instead of firing on every hold/1:1/external.
    out = {"flagged": len(stub) + len(uncovered),
           "stub_summaries": stub[:25], "stub_count": len(stub),
           "uncovered": uncovered[:25], "uncovered_count": len(uncovered), "checked_ts": now}
    if alert and stub:
        try:
            import subprocess as _sp
            top = "; ".join(f"{x['when']} {x['topic']}" for x in stub[:6])
            _sp.run(["python3", str(BASE_DIR / "core" / "alert_gateway.py"),
                     "--severity", "info", "--source", "zoom-reconcile",
                     "--issue-key", "zoom-summary-gaps",
                     "--subject", f"{len(stub)} Zoom meeting(s) in feed missing a real summary body",
                     "--body", f"{top}"],
                    timeout=20, check=False)
        except Exception:
            pass
    return out


def recency_weight(ts) -> float:
    """Age multiplier in (0, 1]: 1.0 inside the grace window, decaying to _RECENCY_FLOOR.

    Shared by every work-lane ranker so keyword and semantic retrieval age their hits the
    same way. An unusable or missing timestamp weighs 1.0 -- burying a real record for
    missing metadata is worse than the rare undated row ranking high.
    """
    try:
        age_days = (time.time() - float(ts)) / 86400.0
    except (TypeError, ValueError):
        return 1.0
    if not ts or age_days <= _RECENCY_GRACE_DAYS:
        return 1.0
    decay = 0.5 ** ((age_days - _RECENCY_GRACE_DAYS) / max(1.0, _RECENCY_HALFLIFE_DAYS))
    return max(_RECENCY_FLOOR, decay)


def _recency_weighted_rank(rank, ts) -> float:
    """Shrink an old hit's bm25 magnitude so fresh context wins comparable relevance.

    bm25 scores are negative and sort ascending, so scaling toward zero demotes.
    """
    try:
        score = float(rank)
    except (TypeError, ValueError):
        return 0.0
    return score * recency_weight(ts)


def _fts_term(word: str) -> str:
    """Normalize one query word into the body of an FTS5 quoted phrase.

    The old form welded every non-alnum out of the word, and against a `porter unicode61`
    table that indexed `sf` + `3142` separately, `SF-3142` -> `SF3142` matched ZERO rows.
    Measured 2026-08-13 on the live index: SF-3142 38 rows / SF3142 0, DPP-002 37 / 0,
    vrm-partial 508 / 0 -- and `search('who owns SF-4142')` and `search('who owns DPP-002')`
    returned byte-identical hit lists, because the only surviving term in both was `own`.
    So: strip leading/trailing punctuation, split the separator class to a SPACE so the
    caller's f'"{t}"' wrapper emits a real phrase, and keep welding apostrophes (don't ->
    dont, never the live phrase "don t"). The split is deliberately NOT gated on the word
    containing a digit -- that guard misses vrm-partial (508 rows vs 0).
    Doubling an embedded `"` is not optional: the old regex stripped quotes as a side
    effect, so preserving punctuation lets one terminate the phrase early -> OperationalError
    -> the silent except below -> the caller quietly gets zero hits.
    """
    w = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", word or "")
    w = w.replace("'", "").replace("’", "")
    w = re.sub(r"[-_./]+", " ", w)
    return re.sub(r"\s+", " ", w).strip().replace('"', '""')


def _py_snippet(body: str, terms: list[str], width: int = 160) -> str:
    """Keyword-in-context like FTS5 snippet(), for the scoped fast path."""
    flat = " ".join(str(body or "").split())
    low = flat.lower()
    for t in terms:
        i = low.find(t.lower())
        if i >= 0:
            a, b = max(0, i - width // 2), min(len(flat), i + len(t) + width // 2)
            return (("… " if a else "") + flat[a:i] + "«" + flat[i:i + len(t)] + "»"
                    + flat[i + len(t):b] + (" …" if b < len(flat) else ""))
    return flat[:width]


def search(query: str, limit: int = 15, sources=None) -> list[dict]:
    # FTS5 must compute bm25 for every row matching ANY term. Feeding an email-sized query here
    # (hundreds of common words joined by OR) turned a reply pass into minutes of CPU and blocked
    # intake behind old mail. Keep the strongest ordered anchors only; callers already put exact IDs,
    # subject, sender, and project aliases first.
    stop = {
        "about", "after", "again", "also", "and", "are", "been", "but", "can", "could",
        "email", "for", "from", "have", "here", "into", "just", "need", "not", "please", "re",
        "reply", "that", "the", "their", "them", "then", "there", "these", "they", "this", "to",
        "update", "was", "were", "what", "when", "where", "which", "who", "will", "with", "would",
        "you", "your",
    }
    raw = [_fts_term(w) for w in (query or "").split()]
    terms, seen = [], set()
    # Exact-looking identifiers survive even when a caller puts them late in a long body.
    ordered = [t for t in raw if any(ch.isdigit() for ch in t)] + raw
    for term in ordered:
        key = term.lower()
        if len(term) < 3 or key in stop or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= 10:
            break
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    try:
        c = _read_conn()
    except (sqlite3.Error, OSError):
        return []
    where = "docs MATCH ?"
    params: list = [match]
    if sources:
        where += " AND source IN (%s)" % ",".join("?" * len(sources))
        params += list(sources)
    # Rank on relevance, then age. bm25 alone answers "which document shares the most
    # vocabulary", which is not the same question as "what is going on with this now" --
    # a months-old meeting recap on a recurring topic outscored the live thread it was
    # about. Over-fetch, apply the age weight, and keep the caller's window. The decay is
    # bounded by _RECENCY_FLOOR so a decisively stronger old hit (an exact ID) still wins.
    pool = min(400, max(int(limit) * 5, 60))
    sql = (
        "SELECT source, ref, ts, url, title, body, "
        "snippet(docs, -1, '«', '»', ' … ', 16) AS snippet, "
        f"bm25(docs, {_BM25_WEIGHTS}) AS rank "
        f"FROM docs WHERE {where} ORDER BY rank LIMIT ?"
    )
    params.append(pool)
    try:
        with _sqlite_deadline(c, _SEARCH_DEADLINE_S):
            if sources:
                # 2026-10-19 spfix: `MATCH ... AND source IN (...) ORDER BY rank` loses FTS5's
                # ranked-LIMIT fast path (measured 1.1s vs 0.1s on 170k docs), blows the 0.8s
                # deadline and returns NOTHING -- every scoped caller (the inbox assembler) was
                # silently keyword-blind. Rank unscoped on the fast path, filter, then hydrate.
                ph = ",".join("?" * len(sources))
                inner = min(2000, pool * 5)
                ranked = c.execute(
                    "SELECT rowid, rank FROM (SELECT rowid, source, "
                    f"bm25(docs, {_BM25_WEIGHTS}) AS rank FROM docs WHERE docs MATCH ? "
                    f"ORDER BY rank LIMIT ?) WHERE source IN ({ph}) LIMIT ?",
                    [match, inner, *list(sources), pool]).fetchall()
                # snippet() would force a second MATCH pass (1.1s again); build it from the body.
                rank_of = {int(r[0]): r[1] for r in ranked}
                rows = []
                if rank_of:
                    got = c.execute(
                        "SELECT rowid, source, ref, ts, url, title, body FROM docs "
                        f"WHERE rowid IN ({','.join('?' * len(rank_of))})", list(rank_of)
                    ).fetchall()
                    for r in sorted(got, key=lambda r: rank_of[int(r[0])]):
                        d = {k: r[k] for k in ("source", "ref", "ts", "url", "title", "body")}
                        d["snippet"] = _py_snippet(d["body"] or "", terms)
                        d["rank"] = rank_of[int(r[0])]
                        rows.append(d)
            else:
                rows = c.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        c.close()
    # Collapse identical bodies across the whole pool BEFORE the recency sort truncates to
    # `limit`, or the same message eats the caller's slots and its char budget. Measured
    # 2026-08-13 on 'prepay dealer pilot accounts': hits 2-7 were six byte-identical 1,511-char
    # bodies (one email indexed from six folders, so six different refs) -- 3 distinct bodies in
    # 8 slots, 9,066 of 14,379 hydrated chars one repeat.
    # Survivor is the NEWEST copy, never the best-ranked one: after _recency_weighted_rank the
    # top-ranked member is frequently the Archive copy, and the Outlook reply recipe needs the
    # exact newest inbound message id. Bodies at or under 50 normalized chars are left alone --
    # empty/trivial bodies are one huge hash group and collapsing them would drop distinct docs
    # whose whole signal is the title/snippet.
    deduped: list = []
    slot: dict = {}
    dropped: dict = {}
    for row in rows:
        norm = " ".join((row["body"] or "").split())
        if len(norm) <= 50:
            deduped.append(row)
            continue
        key = hashlib.md5(norm.encode("utf-8", "ignore")).hexdigest()
        i = slot.get(key)
        if i is None:
            slot[key] = len(deduped)
            deduped.append(row)
            continue
        dropped[key] = dropped.get(key, 0) + 1
        try:
            newer = float(row["ts"] or 0) > float(deduped[i]["ts"] or 0)
        except (TypeError, ValueError):
            newer = False
        if newer:
            deduped[i] = row
    dup_of = {
        (deduped[i]["source"], deduped[i]["ref"]): n
        for k, n in dropped.items()
        for i in (slot[k],)
    }
    rows = sorted(deduped, key=lambda row: _recency_weighted_rank(row["rank"], row["ts"]))[
        : int(limit)
    ]
    out = []
    for r in rows:
        d = dict(r)
        # identical copies discarded (survivor excluded) -- so "seen in N copies" isn't lost.
        n_dupes = dup_of.get((d.get("source"), d.get("ref")))
        if n_dupes:
            d["dupes"] = n_dupes
        d["source_label"] = source_label(d.get("source"))
        if d.get("ts"):
            try:
                d["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(d["ts"])))
            except Exception:
                d["when"] = ""
        out.append(d)
    return out


def bodies_for(source: str, refs) -> dict:
    """Fast full-body lookup from the index (no AppleScript). {ref: body} for refs that have one."""
    out = {}
    try:
        c = _read_conn()
    except Exception:
        return out
    wanted = list(dict.fromkeys(str(ref) for ref in (refs or []) if str(ref)))[:50]
    if not wanted:
        c.close()
        return out
    try:
        c.execute(f"PRAGMA busy_timeout={max(50, _READ_BUSY_MS)}")
        placeholders = ",".join("?" for _ in wanted)
        # Resolve rowids from seen (PRIMARY KEY(source,ref) — indexed), then fetch bodies from the
        # FTS5 docs table by rowid. This replaces the old bounded full scan over UNINDEXED source/ref,
        # which grew with the whole corpus. Legacy rows without docrow fall back to the scan below.
        row2ref: dict[int, str] = {}
        for ref, drow in c.execute(
            f"SELECT ref, docrow FROM seen WHERE source=? AND ref IN ({placeholders})",
            [str(source), *wanted],
        ).fetchall():
            if drow is not None:
                row2ref[int(drow)] = str(ref)
        with _sqlite_deadline(c, _BODY_DEADLINE_S):
            if row2ref:
                rowids = list(row2ref.keys())
                rph = ",".join("?" for _ in rowids)
                rows = c.execute(
                    f"SELECT rowid, body FROM docs WHERE rowid IN ({rph}) AND length(body)>0",
                    rowids,
                ).fetchall()
                for rid, body in rows:
                    ref = row2ref.get(int(rid))
                    if body and ref is not None and ref not in out:
                        out[ref] = body
            missing = [w for w in wanted if w not in out]
            if missing:
                mph = ",".join("?" for _ in missing)
                rows = c.execute(
                    f"SELECT ref, body FROM docs WHERE source=? AND ref IN ({mph}) AND length(body)>0",
                    [str(source), *missing],
                ).fetchall()
                for ref, body in rows:
                    if body and str(ref) not in out:
                        out[str(ref)] = body
    except sqlite3.OperationalError:
        pass
    finally:
        c.close()
    return out


def recent_meeting_summaries(limit: int = 5, must: str = "", anyterms=None,
                             since_days=None, recap_only: bool = False) -> list[dict]:
    """Zoom AI Companion 'Meeting assets … are ready' recap emails (full indexed body), newest first.
    These carry the Quick Recap / decisions / Next Steps — the meeting summary IS captured here.
      must      = legacy AND single-substring filter (title+body+participants)
      anyterms  = OR-match: keep a meeting if ANY term appears in title+body+participants
      since_days= only meetings within the last N days (widens the scan so a 30-day OR-match
                  over many names isn't starved by the row cap)."""
    import datetime as _dt
    import time as _time
    terms = [t.lower() for t in (anyterms or []) if t]
    scan = max(limit * 6, 60) if (terms or since_days) else max(limit * 4, 12)
    cutoff = (_time.time() - float(since_days) * 86400) if since_days else None
    rows = []
    # participants column exists in newer indexes but guard for older schemas.
    for cols in ("title, body, ts, ref, participants", "title, body, ts, ref"):
        c = None
        try:
            c = _read_conn()
            sql = (f"SELECT {cols} FROM docs WHERE source='email' AND title LIKE '%Meeting assets%' "
                   "AND length(body) > 200")
            params = []
            if cutoff is not None:
                sql += " AND ts >= ?"
                params.append(cutoff)
            sql += " ORDER BY ts DESC LIMIT ?"
            params.append(scan)
            rows = c.execute(sql, params).fetchall()
            break
        except Exception:
            rows = []
        finally:
            if c is not None:
                c.close()
    out, seen = [], set()
    for r in rows:
        title, body, ts, ref = r[0], r[1], r[2], r[3]
        participants = r[4] if len(r) > 4 else ""
        key = (title or "").replace("[Auto-Archived] ", "").replace("[Archive] ", "")[:60]
        if key in seen:
            continue
        bl = (body or "").lower()
        if recap_only and ("quick recap" not in bl or "could not be generated" in bl
                           or "insufficient transcript" in bl):
            continue  # skip 'recording ready' / failed-summary stubs — keep only real AI recaps
        hay = ((title or "") + " " + bl + " " + (participants or "")).lower()
        if must and must.lower() not in hay:
            continue
        if terms and not any(t in hay for t in terms):
            continue
        seen.add(key)
        try:
            when = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
        except Exception:
            when = ""
        out.append({"when": when, "title": title or "", "body": body or "", "ref": ref})
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "reindex"
    if cmd == "lock-status":
        print(json.dumps(writer_lock_status(), default=str))
        raise SystemExit(0)
    if cmd in {"ingest", "reindex", "safe-reindex"}:
        from machine.heavy_work import allowed as _hw_allowed
        _ok, _why = _hw_allowed("heavy")
        if not _ok:
            print(json.dumps({"skipped": cmd, "why": _why}))
            raise SystemExit(0)
    if cmd in {"-h", "--help", "help"}:
        print("usage: work_context_index.py [safe-reindex [body_budget]|reindex [body_budget]|ingest <littlebird|zoom|email|digests|ado>|lock-status|reconcile|health|search QUERY]")
    elif cmd == "ingest":
        # Real-time feed path: scoped, cheap, idempotent — sync scripts call this on new content.
        src = sys.argv[2] if len(sys.argv) > 2 else ""
        try:
            print(json.dumps(ingest(src), indent=2, default=str))
        except TimeoutError as exc:
            print(json.dumps({
                "ingest_source": (src or "").strip().lower(),
                "skipped": "lock_busy",
                "detail": str(exc)[:160],
            }, indent=2))
            raise SystemExit(75 if os.environ.get("WCI_LOCK_BUSY_RC") == "75" else 0)
    elif cmd == "reconcile":
        # $0 deterministic gap-check; --alert pages the gateway when meetings lack a real summary.
        print(json.dumps(reconcile_zoom_summaries(alert="--alert" in sys.argv[2:]), indent=2, default=str))
    elif cmd == "health":
        print(json.dumps(health(), indent=2, default=str))
    elif cmd == "repair":
        print(json.dumps(repair_duplicate_docs(dry_run="--dry-run" in sys.argv[2:]),
                         indent=2, default=str))
    elif cmd == "search":
        for r in search(" ".join(sys.argv[2:]) or "DPP", limit=15):
            print(f"[{r['source']}] {r.get('when','')} {r['title'][:80]}  (ref={r['ref']})")
            print(f"   {(r.get('snippet') or '')[:240]}")
    elif cmd in {"reindex", "safe-reindex"}:
        budget = int(sys.argv[2]) if len(sys.argv) > 2 else 80
        result = reindex(body_budget=budget)
        # keep the SFDC master docs (architecture/process/user-guide) current + RAG-ingested on the
        # SAME cadence as the corpus refresh -- watermark-gated (no-op when no new SF work landed),
        # fail-soft. Runs AFTER reindex so it uses its own fresh connection (no lock contention). The
        # master .md files stay the source of truth; this only refreshes the derived index.
        # Runs for BOTH reindex and safe-reindex. The scheduled job is `safe-reindex`, so this
        # gate meant the docs regenerated only on a manual run -- MASTER_ARCHITECTURE.md,
        # PROCESS_MANUAL.md and USER_GUIDE.md sat frozen at 2026-08-02 for 11 days while the
        # comment above promised corpus cadence. Generation is already watermark-gated and
        # fail-soft, which is what makes it safe on the short cycle.
        try:
            from core import sfdc_master_docs
            result["sfdc_master_docs"] = sfdc_master_docs.generate()
        except Exception as _e:  # pragma: no cover
            result["sfdc_master_docs"] = {"error": str(_e)[:160]}
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"unknown command: {cmd!r}; use --help", file=sys.stderr)
        raise SystemExit(2)
