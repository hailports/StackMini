#!/usr/bin/env python3
"""One useful local completion whenever the llama slot is free.

Each call is one item, in priority order: an open ADO ticket, pull request, or
operator ticket; one local pre-draft; one new work-brain summary; one
draft-versus-sent note; one runbook from a resolved ticket. A source is done
for its content hash and revision, and it comes back only when that content
changes.

The next item's context is gathered while the current one generates. A real
chat message or a queued draft aborts the call. Other background work waits.
Nothing is sent.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

TIMEOUT_S = 240
ITEM_CHARS = 1100
BATCH_CHARS = 4600
MAX_ITEMS = 1
REFRESH_S = 600
# Packed source kept in the prompt. The gather can be larger; this is what the
# model reads, so prefill stays small next to a full note.
_PACK = 2300
_PRIMARY = 1400
# Ceiling for one item. The grammar finishes on ENDNOTE well under this.
# A stop string is not used: a line of source text can begin with the end
# marker, and the server would cut the note off there.
_CAPS = {
    "digest": 4200, "reply": 4200, "compress": 4200,
    "learn": 4200, "runbook": 4200, "style": 4200,
    "nextstep": 4200,
}
_CHECK_CAP = 700
_KINDS = ("digest", "reply", "compress", "learn", "runbook", "style")
_CYCLE = (
    "digest", "reply", "digest",
    "reply", "learn",
    "compress", "style",
    "reply", "runbook",
    "digest", "learn",
)
_STOP = "ENDNOTE"
# One item, one call. The grammar is what makes the note finish, so a short
# eos cannot land after a few hundred tokens and the prefill between those
# short notes cannot pull generation under 95% of wall time.
# Ranges stay narrow. A wide {min,max} builds a deep rule chain and also lets
# a line run into the token ceiling before ENDNOTE.
_GRAMMAR = (
    'root ::= facts result steps checks "ENDNOTE\\n"\n'
    'facts ::= "SOURCE FACTS\\n" fact{6}\n'
    'fact ::= "- " [^\\r\\n]{90,110} "\\n"\n'
    'result ::= "RESULT\\n" rline{16}\n'
    'rline ::= [^\\r\\n]{680,700} "\\n"\n'
    'steps ::= "NEXT STEPS\\n" step{5}\n'
    'step ::= "- " [^\\r\\n]{80,100} "\\n"\n'
    'checks ::= "SELF-CHECK\\n" check{5}\n'
    'check ::= "- " [^\\r\\n]{60,80} "\\n"\n'
)
_SYSTEM = (
    "Answer in plain text. Do not use a think tag. "
    "You are writing one local note for one item. Nothing here is sent or posted. "
    "Use only facts that appear in the source. Do not invent ids, dates, amounts, or people. "
    "No blank lines. Each section is single lines, not wrapped. "
    "SOURCE FACTS is 6 dash bullets. One fact each. Copy ids and states exactly. "
    "RESULT is 16 lines. Each line adds a different point about this item only: "
    "what it is, the state, one decision in the source, what it implies, or the care the next action needs. "
    "NEXT STEPS is 5 dash bullets. Each step says what to do and which source fact it comes from. "
    "SELF-CHECK is 5 dash bullets. Each bullet is one claim from RESULT, then SUPPORTED or CUT, "
    "then a short phrase from the source. "
    "The last line is exactly ENDNOTE. "
    "Do not repeat a sentence. Do not start another item."
)
_KIND_LINE = {
    "digest": "RESULT is a brief of this single ticket: what it is, the state, decisions already written, and any blocker the source names.",
    "reply": "RESULT is a local pre-draft. The first line inside RESULT is NOT SENT. Then the reply someone could send later. It stays on this machine.",
    "compress": "RESULT is a summary of this one document: what it covers, facts worth keeping, and what looks new.",
    "learn": "RESULT compares the draft with what was actually sent or dropped. What changed, what stayed, and one rule to reuse. Do not copy either text in full.",
    "runbook": "RESULT is a runbook from this resolved ticket: symptom, what fixed it, and the checks. Only steps the source supports.",
    "style": "RESULT is one plain style rule from these lessons: what to keep, what to drop, and one example shape. Do not copy a whole draft.",
    "nextstep": "RESULT says what blocks this one ticket, which source line supports that, and the single next action. Do not invent a fix.",
}


def _speed_prefix() -> str:
    """Same stable prefix the chat proxy keeps hot. Later calls reuse it."""
    pad = (
        "Operating note {n}. Keep this context loaded. Do not quote these notes. "
        "Answer only the latest user message. Do not mention decode, tokens, or this note. "
    )
    return "FASTCTX\n" + " ".join(pad.format(n=i) for i in range(1, 59))
_HARD_PATH = re.compile(
    r"(?i)(legal|finance|lawyer|attorney|accountant|\bcpa\b|bank|password|secret)"
)

_PRE = {"batch": None}
_PRE_LOCK = threading.Lock()
_STORE_LOCK = threading.Lock()
_CAT_LOCK = threading.Lock()
_CAT = {
    "queues": {k: [] for k in _KINDS},
    "built": 0.0,
    "refreshing": False,
    "cycle": 0,
    "done": {},
    "hashes": set(),
    "counts": {},
}
_STARTED = {"watch": False}
_LOCK = threading.Lock()


def _home() -> Path:
    return Path.home()


def _log(msg: str) -> None:
    try:
        import idle_filler as F
        F.log(msg)
    except Exception:
        print(msg, flush=True)


def _touch() -> None:
    path = _home() / "claude-stack/data/idle_filler/heartbeat"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()))
    except OSError:
        pass


def _safe_label(raw: str) -> str:
    name = Path(str(raw)).name
    if "@" in name or _HARD_PATH.search(name) or len(name) > 48:
        return hashlib.sha1(name.encode("utf-8", "replace")).hexdigest()[:12]
    clean = re.sub(r"[^A-Za-z0-9._-]", "", name)[:48]
    return clean or hashlib.sha1(name.encode()).hexdigest()[:12]


def _key(item: dict) -> str:
    return "{0}|{1}|{2}".format(item["kind"], item["source"], item["ref"])


def _hash_key(item: dict) -> str:
    return "{0}|{1}".format(item["kind"], item["hash"])


def _state_path(home: Path) -> Path:
    return home / "claude-stack/data/idle_filler/deep_state.json"


def _load_doc(home: Path) -> dict:
    try:
        doc = json.loads(_state_path(home).read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _load_done(home: Path) -> dict:
    done = _load_doc(home).get("done")
    return done if isinstance(done, dict) else {}


def _save_doc(home: Path, done: dict, counts: dict) -> None:
    path = _state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"done": done, "built": time.time(), "counts": counts}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    tmp.replace(path)


def _hashes_from(done: dict) -> set:
    out = set()
    for key, rec in done.items():
        if not isinstance(rec, dict):
            continue
        kind = str(key).split("|", 1)[0]
        digest = rec.get("hash")
        if kind and digest:
            out.add(kind + "|" + str(digest))
    return out


def _fp(text: str) -> str:
    try:
        import sys
        cli = str(_home() / "llm-appliance/cli")
        if cli not in sys.path:
            sys.path.insert(0, cli)
        import draft_context as dc
        return dc.stable_fp(text)
    except Exception:
        return hashlib.sha1(re.sub(r"\s+", " ", text or "").strip().lower().encode("utf-8", "replace")).hexdigest()[:16]


def _already(done: dict, hashes: set, item: dict) -> bool:
    if _hash_key(item) in hashes:
        return True
    rec = done.get(_key(item))
    if not isinstance(rec, dict):
        return False
    if rec.get("hash") == item["hash"]:
        return True
    # Same source revision and we already handled it, even if the slice shifted.
    if rec.get("rev") and rec.get("rev") == item["rev"]:
        return True
    fp = str(item.get("fp") or "")
    if fp and rec.get("fp") and rec.get("fp") == fp:
        return True
    return False


def _repeat_seen(done: dict, item: dict) -> bool:
    """True when this ref was already handled and the words did not change."""
    if _already(done, set(), item):
        rec = done.get(_key(item))
        if isinstance(rec, dict) and rec.get("hash") == item.get("hash"):
            return True
        if isinstance(rec, dict) and rec.get("rev") and rec.get("rev") == item.get("rev"):
            return True
        if isinstance(rec, dict) and item.get("fp") and rec.get("fp") == item.get("fp"):
            return True
    rec = done.get(_key(item)) if isinstance(done, dict) else None
    if not isinstance(rec, dict) or not rec.get("hash"):
        return False
    fp = str(item.get("fp") or "")
    if rec.get("fp"):
        return bool(fp) and rec.get("fp") == fp
    # No fingerprint yet. Remember these words and do not generate again.
    return bool(fp)


def _bad_text(text: str) -> bool:
    """Word filter used for source text. Path rules stay on names only."""
    if not text:
        return False
    try:
        import idle_filler as F
        return bool(F.blocked(text))
    except Exception:
        return False


def _clip(text: str, limit: int) -> str:
    text = (text or "").replace("\r", "")
    text = text.replace(_STOP, "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _unit(kind: str, source: str, ref: str, rev: str, label: str, text: str) -> dict | None:
    full = _clip(text, 4500)
    text = full[:ITEM_CHARS]
    if len(text) < 40 or _bad_text(full):
        return None
    digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
    return {
        "kind": kind,
        "source": source,
        "ref": str(ref)[:180],
        "rev": str(rev)[:80],
        "label": _safe_label(label),
        "hash": digest,
        "fp": _fp(full),
        "text": text,
        "seed": full,
    }


def _db(home: Path):
    path = home / "claude-stack/data/work_context.db"
    con = sqlite3.connect("file:{0}?mode=ro".format(path), uri=True, timeout=10)
    con.execute("PRAGMA query_only=ON")
    return con


def _ado_state(body: str) -> str:
    match = re.search(r"(?m)^State:\s*(.+)$", body or "")
    return match.group(1).strip() if match else ""


def _open_ado(state: str) -> bool:
    return state.startswith("New") or state.startswith("Active") or state.startswith("Testing")


def _closed_ado(state: str) -> bool:
    return state.startswith("Resolved") or state.startswith("Closed")


def _docket(home: Path) -> dict:
    path = home / "claude-stack/data/ado_docket/latest.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for row in doc.get("items") or []:
        if isinstance(row, dict) and row.get("id") is not None:
            out[str(row.get("id"))] = row
    return out


def _comment(home: Path, wid: str) -> str:
    path = home / "llm-appliance/runtime/work-loop/ado-comment-cache" / "{0}.json".format(wid)
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(doc, dict):
        return ""
    return _clip(str(doc.get("text") or ""), 500)


def _priority(body: str) -> int:
    match = re.search(r"(?m)^Priority:\s*(\d+)", body or "")
    if not match:
        return 9
    try:
        return int(match.group(1))
    except ValueError:
        return 9


def _collect_sql(home: Path) -> tuple:
    digest_ado = []
    digest_pr = []
    digest_sf = []
    runbook = []
    reply_mail = []
    reply_zoom = []
    compress = {name: [] for name in ("github_source", "sf_env", "sf_env_partial", "sf_env_full")}
    skipped_hard = 0
    now = time.time()
    docket = _docket(home)
    con = _db(home)
    try:
        ado_rows = con.execute(
            "select ref, ts, title, substr(body,1,3600) from docs "
            "where source='ado' and ref like 'wi_%' and ("
            "body like '%State: New%' or body like '%State: Active%' or body like '%State: Testing%' "
            "or body like '%State: Resolved%' or body like '%State: Closed%')"
        ).fetchall()
        pr_rows = con.execute(
            "select ref, ts, title, substr(body,1,2400) from docs "
            "where source='github_pr' and instr(lower(substr(body,1,240)), 'state: open')>0"
        ).fetchall()
        sf_rows = con.execute(
            "select ref, ts, title, substr(body,1,2000) from docs "
            "where source='operator' and ref like 'SF-%' and ts>? order by ts desc limit 160",
            (now - 21 * 86400,),
        ).fetchall()
        mail_rows = con.execute(
            "select ref, ts, title, substr(body,1,2400) from docs "
            "where source='email' and ts>? order by ts desc limit 140",
            (now - 21 * 86400,),
        ).fetchall()
        zoom_rows = con.execute(
            "select ref, ts, title, substr(body,1,2400) from docs "
            "where source='zoom' and ref like 'thread_%' and ts>? order by ts desc limit 70",
            (now - 21 * 86400,),
        ).fetchall()
        for source, limit in (
            ("github_source", 50),
            ("sf_env", 40),
            ("sf_env_partial", 24),
            ("sf_env_full", 24),
        ):
            compress[source] = con.execute(
                "select ref, ts, title, substr(body,1,2400) from docs "
                "where source=? and ts>? order by ts desc limit ?",
                (source, now - 10 * 86400, limit),
            ).fetchall()
    finally:
        con.close()

    ado_open = []
    for ref, ts, title, body in ado_rows:
        state = _ado_state(body or "")
        wid = str(ref)[3:] if str(ref).startswith("wi_") else str(ref)
        row = docket.get(wid) or {}
        changed = str(row.get("changed") or "")
        rev = "{0:.0f}|{1}".format(float(ts or 0), changed[:32])
        extra = ""
        nxt = _clip(str(row.get("next") or ""), 280)
        if nxt and not _bad_text(nxt):
            extra += "\nNoted next: " + nxt
        if len(body or "") < 500:
            note = _comment(home, wid)
            if note and not _bad_text(note):
                extra += "\nLatest note: " + note
        text = "{0}\nState: {1}\n{2}{3}".format((title or "").strip(), state, body or "", extra)
        if _open_ado(state):
            item = _unit("digest", "ado", str(ref), rev, "ado-" + wid, text)
            if item:
                ado_open.append((_priority(body or ""), -float(ts or 0), len(ado_open), item))
            elif _bad_text(text):
                skipped_hard += 1
        elif _closed_ado(state):
            item = _unit("runbook", "ado", str(ref), rev, "ado-" + wid, text)
            if item:
                runbook.append((-float(ts or 0), len(runbook), item))
            elif _bad_text(text):
                skipped_hard += 1

    ado_open.sort()
    digest_ado = [row[3] for row in ado_open]
    runbook.sort()
    runbook_items = [row[2] for row in runbook[:100]]

    for ref, ts, title, body in pr_rows:
        match = re.search(r"(?m)^state:\s*(\S+)", body or "", re.I)
        if not match or match.group(1).lower() != "open":
            continue
        num = str(ref)[3:] if str(ref).startswith("pr_") else str(ref)
        text = "{0}\n{1}".format((title or "").strip(), body or "")
        item = _unit("digest", "github_pr", str(ref), "{0:.0f}".format(float(ts or 0)), "pr-" + num, text)
        if item:
            digest_pr.append(item)
        elif _bad_text(text):
            skipped_hard += 1

    for ref, ts, title, body in sf_rows:
        num = str(ref)[3:] if str(ref).upper().startswith("SF-") else str(ref)
        text = "{0}\n{1}".format((title or "").strip(), body or "")
        item = _unit("digest", "operator", str(ref), "{0:.0f}".format(float(ts or 0)), "sf-" + num, text)
        if item:
            digest_sf.append(item)
        elif _bad_text(text):
            skipped_hard += 1

    seen_zoom = set()
    for ref, ts, title, body in zoom_rows:
        thread = str(ref).split("@", 1)[0]
        if thread in seen_zoom:
            continue
        seen_zoom.add(thread)
        text = "{0}\n{1}".format((title or "").strip(), body or "")
        label = "zoom-" + hashlib.sha1(thread.encode("utf-8", "replace")).hexdigest()[:10]
        item = _unit("reply", "zoom", thread, "{0:.0f}".format(float(ts or 0)), label, text)
        if item:
            reply_zoom.append(item)
        elif _bad_text(text):
            skipped_hard += 1

    for ref, ts, title, body in mail_rows:
        text = "{0}\n{1}".format((title or "").strip(), body or "")
        label = "mail-" + hashlib.sha1(str(ref).encode("utf-8", "replace")).hexdigest()[:10]
        item = _unit("reply", "email", hashlib.sha1(str(ref).encode()).hexdigest()[:20], "{0:.0f}".format(float(ts or 0)), label, text)
        if item:
            reply_mail.append(item)
        elif _bad_text(text):
            skipped_hard += 1

    compress_lists = []
    for source in ("github_source", "sf_env", "sf_env_partial", "sf_env_full"):
        bucket = []
        for ref, ts, title, body in compress.get(source) or []:
            if "idlefill" in str(title or "").lower() or "idlefill" in str(ref).lower():
                continue
            text = "{0}\n{1}".format((title or "").strip(), body or "")
            label = "doc-" + hashlib.sha1((source + "|" + str(ref)).encode()).hexdigest()[:10]
            item = _unit("compress", source, str(ref)[:160], "{0:.0f}".format(float(ts or 0)), label, text)
            if item:
                bucket.append(item)
            elif _bad_text(text):
                skipped_hard += 1
        compress_lists.append(bucket)

    return {
        "digest": digest_ado + digest_pr + digest_sf,
        "reply": _interleave([reply_mail, reply_zoom]),
        "compress": _interleave(compress_lists),
        "runbook": runbook_items,
        "skip_hard": skipped_hard,
    }


def _interleave(groups: list) -> list:
    out = []
    while any(groups):
        for group in groups:
            if group:
                out.append(group.pop(0))
    return out


def _collect_learn(home: Path) -> tuple:
    items = []
    skipped = 0
    edited = []
    other = []
    path = home / "claude-stack/data/learning/draft_feedback_events.jsonl"
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            outcome = str(obj.get("outcome") or "")
            if outcome not in {"sent_edited", "sent_unchanged", "deleted", "discarded_by_operator"}:
                continue
            draft = _clip(str(obj.get("draft_text") or ""), 1400)
            final = _clip(str(obj.get("final_text") or ""), 1400)
            if len(draft) < 20 or len(final) < 20:
                continue
            text = "outcome: {0}\nDRAFT\n{1}\nACTUAL\n{2}".format(outcome, draft, final)
            ref = str(obj.get("event_id") or hashlib.sha1(line.encode()).hexdigest()[:16])
            rev = "{0}|{1}|{2}".format(
                str(obj.get("observed_at") or "")[:32],
                str(obj.get("draft_hash") or "")[:12],
                str(obj.get("final_hash") or "")[:12],
            )
            label = "learn-" + hashlib.sha1(ref.encode()).hexdigest()[:10]
            item = _unit("learn", "draft_feedback", ref, rev, label, text)
            if not item:
                if _bad_text(text):
                    skipped += 1
                continue
            if outcome == "sent_edited":
                edited.append(item)
            else:
                other.append(item)
    items.extend(reversed(edited[-160:]))
    items.extend(reversed(other[-60:]))
    learn_dir = home / "claude-stack/data/learning"
    if learn_dir.is_dir():
        for path in sorted(learn_dir.glob("operator_review_*pairs.jsonl")):
            if "bak" in path.name:
                continue
            try:
                raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for index, line in enumerate(raw_lines):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                text = "decision: {0}\nREJECTED\n{1}\nCHOSEN\n{2}\nNOTE\n{3}".format(
                    _clip(str(obj.get("decision") or ""), 200),
                    _clip(str(obj.get("rejected") or ""), 700),
                    _clip(str(obj.get("chosen") or ""), 700),
                    _clip(str(obj.get("lesson") or ""), 300),
                )
                rev = str(obj.get("ts") or path.stat().st_mtime)
                label = "pair-" + hashlib.sha1((path.name + str(index)).encode()).hexdigest()[:10]
                item = _unit("learn", "pairs", path.name + "#" + str(index), rev, label, text)
                if item:
                    items.append(item)
                elif _bad_text(text):
                    skipped += 1
    return items, skipped


def _kept(done: dict, item: dict | None) -> dict | None:
    if not item:
        return None
    if _already(done, set(), item):
        return None
    return item


def _collect_extra(home: Path, done: dict) -> tuple:
    """More distinct operator notes. A full source falls through to the next."""
    digest = []
    reply = []
    learn = []
    compress = []
    runbook = []
    style = []
    skipped = 0
    now = time.time()
    try:
        con = _db(home)
    except Exception:
        con = None
    rows = []
    prs = []
    if con is not None:
        try:
            rows = con.execute(
                "select ref, ts, title, substr(body,1,3200) from docs "
                "where source='ado' and ref like 'wi_%' and ("
                "body like '%State: New%' or body like '%State: Active%' or body like '%State: Testing%' "
                "or ts>?)",
                (now - 30 * 86400,),
            ).fetchall()
            prs = con.execute(
                "select ref, ts, title, substr(body,1,1800) from docs "
                "where source='github_pr' and ts>? and ("
                "instr(lower(ifnull(title,'')||ifnull(body,'')), 'operator-operator')>0 "
                "or instr(lower(ifnull(title,'')||ifnull(body,'')), 'operator')>0) "
                "order by ts desc limit 60",
                (now - 30 * 86400,),
            ).fetchall()
        except Exception as exc:
            _log("deepfill extra {0}".format(type(exc).__name__))
            rows, prs = [], []
        finally:
            con.close()

    seen_ref = set()
    parent_ids = []
    reply_n = 0
    recheck_n = 0
    for ref, ts, title, body in rows:
        wid = str(ref)[3:] if str(ref).startswith("wi_") else str(ref)
        state = _ado_state(body or "")
        rev = "{0:.0f}|{1}".format(float(ts or 0), hashlib.sha1((body or "").encode()).hexdigest()[:8])
        text = "{0}\nState: {1}\n{2}".format((title or "").strip(), state, body or "")
        seen_ref.add(str(ref))
        parent = re.search(r"(?m)^Parent:\s*(\d+)\s*$", body or "")
        if parent and parent.group(1) not in parent_ids:
            parent_ids.append(parent.group(1))
        if _open_ado(state) and reply_n < 280:
            item = _kept(done, _unit(
                "reply", "ado", str(ref), rev + "|reply", "ado-" + wid + "-reply",
                "Local pre-draft only. Not sent.\n" + text,
            ))
            if item:
                reply.append(item)
                reply_n += 1
            elif _bad_text(text):
                skipped += 1
        if recheck_n < 220 and (_open_ado(state) or float(ts or 0) > now - 30 * 86400):
            item = _kept(done, _unit(
                "learn", "recheck", str(ref), rev + "|recheck", "recheck-" + wid,
                "Recheck an older note against this ticket. Say what still holds and what the source now says.\n" + text,
            ))
            if item:
                learn.append(item)
                recheck_n += 1

    missing = ["wi_" + pid for pid in parent_ids if "wi_" + pid not in seen_ref][:80]
    if missing and parent_ids:
        try:
            con = _db(home)
            try:
                marks = ",".join("?" for _ in missing)
                parents = con.execute(
                    "select ref, ts, title, substr(body,1,2400) from docs "
                    "where source='ado' and ref in ({0})".format(marks),
                    missing,
                ).fetchall()
            finally:
                con.close()
        except Exception:
            parents = []
        for ref, ts, title, body in parents:
            wid = str(ref)[3:] if str(ref).startswith("wi_") else str(ref)
            text = "{0}\n{1}".format((title or "").strip(), body or "")
            item = _kept(done, _unit(
                "digest", "ado", str(ref), "{0:.0f}|parent".format(float(ts or 0)),
                "ado-" + wid, text,
            ))
            if item:
                digest.append(item)

    for ref, ts, title, body in prs:
        num = str(ref)[3:] if str(ref).startswith("pr_") else str(ref)
        text = "{0}\n{1}".format((title or "").strip(), body or "")
        item = _kept(done, _unit(
            "compress", "github_pr", "sum:" + str(ref), "{0:.0f}".format(float(ts or 0)),
            "pr-" + num, text,
        ))
        if item:
            compress.append(item)

    lessons = _lesson_rows(home)
    learn_n = 0
    for row in reversed(lessons):
        if learn_n >= 200:
            break
        item = _kept(done, _lesson_item(row))
        if item:
            learn.append(item)
            learn_n += 1
    chunks = []
    bucket = []
    surface = ""
    for row in lessons:
        line = _lesson_line(row)
        if not line:
            continue
        if _bad_text(line):
            skipped += 1
            continue
        surf = str(row.get("surface") or row.get("channel") or "note")[:16]
        if bucket and (surf != surface or len(bucket) >= 6):
            chunks.append((surface, list(bucket)))
            bucket = []
        surface = surf
        bucket.append(line)
    if bucket:
        chunks.append((surface, list(bucket)))
    style_n = 0
    for index, (surf, lines) in enumerate(chunks):
        if style_n >= 40:
            break
        text = "surface: {0}\n".format(surf) + "\n".join(lines)
        item = _kept(done, _unit(
            "style", "draft_style", surf + "#" + str(index),
            hashlib.sha1(text.encode()).hexdigest()[:12],
            "style-" + hashlib.sha1(text.encode()).hexdigest()[:10], text,
        ))
        if item:
            style.append(item)
            style_n += 1

    for kind_name, root, source, cap in (
        ("runbook", home / "llm-appliance/runtime/support-playbooks", "playbook", 70),
        ("compress", home / "work-brain-inbox", "work_brain", 80),
    ):
        got, hard = _file_notes(home, done, root, kind_name, source, cap)
        skipped += hard
        if kind_name == "runbook":
            runbook.extend(got)
        else:
            compress.extend(got)
    return {
        "digest": digest, "reply": reply, "learn": learn,
        "compress": compress, "runbook": runbook, "style": style,
        "skip_hard": skipped,
    }


def _lesson_line(row: dict) -> str:
    lesson = str(row.get("lesson") or row.get("diff") or "").strip()
    if len(lesson) >= 24:
        return _clip(lesson, 280)
    example = str(row.get("example") or "").strip()
    if len(example) >= 24:
        return _clip(example, 280)
    return ""


def _lesson_item(row: dict) -> dict | None:
    lesson = _lesson_line(row)
    draft = _clip(str(row.get("draft") or ""), 500)
    final = _clip(str(row.get("final") or row.get("example") or ""), 500)
    if len(lesson) < 24 and len(draft) < 20:
        return None
    surface = str(row.get("surface") or row.get("channel") or "note")[:16]
    text = "surface: {0}\nLESSON\n{1}\nDRAFT\n{2}\nACTUAL\n{3}".format(surface, lesson, draft, final)
    ref = str(row.get("source_event_id") or row.get("ref") or hashlib.sha1(text.encode()).hexdigest()[:16])
    source = "draft_gold" if row.get("_gold") else "draft_edits"
    return _unit("learn", source, ref, str(row.get("ts") or "")[:40], "learn-" + hashlib.sha1(ref.encode()).hexdigest()[:10], text)


def _lesson_rows(home: Path) -> list:
    rows = []
    learn = home / "claude-stack/data/learning"
    for name, gold in (("draft_edits.jsonl", False), ("draft_gold.jsonl", True)):
        path = learn / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                obj["_gold"] = gold
                rows.append(obj)
    return rows


def _file_notes(home: Path, done: dict, root: Path, kind: str, source: str, cap: int) -> tuple:
    items = []
    skipped = 0
    if not root.is_dir():
        return items, skipped
    found = []
    for dirpath, dirs, files in os.walk(root):
        low = dirpath.lower()
        if any(part in low for part in ("legal", "finance", "node_modules", ".git", "onedrive")):
            dirs[:] = []
            continue
        dirs[:] = [name for name in dirs if name.lower() not in {"legal", "finance", ".git"}]
        for name in files:
            if not name.endswith(".md") or name.endswith(".bak") or ".bak" in name:
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > 180_000 or path.stat().st_size < 40:
                    continue
                found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    found.sort(reverse=True)
    for mtime, path in found:
        if len(items) >= cap:
            break
        rel = str(path.relative_to(home))[:160]
        if _HARD_PATH.search(rel):
            skipped += 1
            continue
        rev = "{0:.0f}".format(mtime)
        key = "{0}|{1}|{2}".format(kind, source, rel)
        rec = done.get(key)
        if isinstance(rec, dict) and rec.get("rev") == rev and rec.get("fp"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:3200]
        except OSError:
            continue
        if _bad_text(text) or _HARD_PATH.search(text[:400]):
            skipped += 1
            continue
        label = kind[:4] + "-" + hashlib.sha1(rel.encode()).hexdigest()[:10]
        item = _kept(done, _unit(kind, source, rel, rev, label, path.name + "\n" + text))
        if item:
            items.append(item)
    return items, skipped


def _collect(home: Path) -> dict:
    try:
        sql = _collect_sql(home)
    except Exception as exc:
        _log("deepfill collect {0}".format(type(exc).__name__))
        sql = {"digest": [], "reply": [], "compress": [], "runbook": [], "skip_hard": 0}
    try:
        learn, learn_hard = _collect_learn(home)
    except Exception as exc:
        _log("deepfill learn {0}".format(type(exc).__name__))
        learn, learn_hard = [], 0
    done = _load_done(home)
    try:
        extra = _collect_extra(home, done)
    except Exception as exc:
        _log("deepfill extra {0}".format(type(exc).__name__))
        extra = {"digest": [], "reply": [], "learn": [], "compress": [], "runbook": [], "style": [], "skip_hard": 0}
    hashes = _hashes_from(done)
    queues = {k: [] for k in _KINDS}
    skipped_done = 0
    raw = {
        "digest": list(sql["digest"]) + list(extra.get("digest") or []),
        "reply": list(sql["reply"]) + list(extra.get("reply") or []),
        "compress": list(sql["compress"]) + list(extra.get("compress") or []),
        "learn": list(learn) + list(extra.get("learn") or []),
        "runbook": list(sql["runbook"]) + list(extra.get("runbook") or []),
        "style": list(extra.get("style") or []),
    }
    for kind, rows in raw.items():
        seen = set()
        for item in rows:
            if item["hash"] in seen:
                continue
            seen.add(item["hash"])
            if _already(done, hashes, item):
                skipped_done += 1
                continue
            queues[kind].append(item)
    counts = {k: len(queues[k]) for k in _KINDS}
    counts["skip_done"] = skipped_done
    counts["skip_hard"] = int(sql.get("skip_hard") or 0) + int(learn_hard) + int(extra.get("skip_hard") or 0)
    return {"queues": queues, "counts": counts, "done": done, "hashes": hashes}


def _rebuild(home: Path, persist: bool = True) -> dict:
    packed = _collect(home)
    with _CAT_LOCK:
        # A job can finish while the scan runs. Keep that newer mark.
        merged = dict(packed["done"])
        if isinstance(_CAT.get("done"), dict):
            merged.update(_CAT["done"])
        hashes = _hashes_from(merged)
        queues = {}
        for kind, rows in packed["queues"].items():
            queues[kind] = [item for item in rows if not _already(merged, hashes, item)]
        _CAT["queues"] = queues
        _CAT["done"] = merged
        _CAT["hashes"] = hashes
        counts = {k: len(queues[k]) for k in _KINDS}
        counts["skip_done"] = packed["counts"].get("skip_done", 0)
        counts["skip_hard"] = packed["counts"].get("skip_hard", 0)
        _CAT["counts"] = counts
        _CAT["built"] = time.time()
        _CAT["refreshing"] = False
    _log("deepfill backlog {0}".format(" ".join(
        "{0}={1}".format(k, counts.get(k, 0)) for k in list(_KINDS) + ["skip_done", "skip_hard"]
    )))
    if persist:
        try:
            _save_doc(home, merged, {k: counts.get(k, 0) for k in _KINDS})
        except OSError as exc:
            _log("deepfill state {0}".format(type(exc).__name__))
    return counts


def _refresh_async(home: Path) -> None:
    with _CAT_LOCK:
        if _CAT["refreshing"]:
            return
        if _CAT["built"] and time.time() - float(_CAT["built"]) < REFRESH_S:
            return
        _CAT["refreshing"] = True

    def work() -> None:
        try:
            _rebuild(home)
        except Exception as exc:
            _log("deepfill refresh {0}".format(type(exc).__name__))
            with _CAT_LOCK:
                _CAT["refreshing"] = False

    threading.Thread(target=work, name="idle-fill-refresh", daemon=True).start()


def _ensure(home: Path) -> None:
    with _CAT_LOCK:
        empty = not any(_CAT["queues"].values())
        built = float(_CAT["built"] or 0)
    if built <= 0 or empty:
        _rebuild(home)
        return
    if time.time() - built >= REFRESH_S:
        _refresh_async(home)


def _pop_kind(kind: str) -> dict | None:
    queue = _CAT["queues"][kind]
    done = _CAT["done"]
    hashes = _CAT["hashes"]
    while queue:
        item = queue.pop(0)
        if _already(done, hashes, item):
            continue
        return item
    return None


def _take_items() -> list:
    """Exactly one item. Never several tickets or threads in one call."""
    with _CAT_LOCK:
        for _ in range(len(_CYCLE) * 2):
            kind = _CYCLE[_CAT["cycle"] % len(_CYCLE)]
            _CAT["cycle"] += 1
            first = _pop_kind(kind)
            if first:
                return [first]
    return []


def _cap_for(kind: str, n_items: int = 1) -> int:
    return _CAPS.get(kind, 1400)


def _sf_nums(text: str) -> list:
    found = []
    for match in re.finditer(r"\bSF-(\d{3,8})\b", text or "", re.I):
        num = match.group(1)
        if num not in found:
            found.append(num)
        if len(found) >= 4:
            break
    return found


def _wi_nums(text: str) -> list:
    found = []
    for match in re.finditer(r"\b(?:AB#|wi_)(\d{5,7})\b", text or ""):
        num = match.group(1)
        if num not in found:
            found.append(num)
        if len(found) >= 3:
            break
    return found


def _parent_num(text: str) -> str:
    match = re.search(r"(?m)^Parent:\s*(\d{5,7})\s*$", text or "")
    return match.group(1) if match else ""


def _mask_piece(text: str) -> str:
    text = _clip(text or "", 900)
    if not text:
        return ""
    try:
        import idle_filler as filler
        text = filler.mask(text)
        if filler.blocked(text):
            return ""
    except Exception:
        if _HARD_PATH.search(text):
            return ""
    if _HARD_PATH.search(text):
        return ""
    if re.search(r"(?i)(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)", text):
        return ""
    return text


def _rows(con, expr: str, source: str, limit: int) -> list:
    try:
        return con.execute(
            "select ref, ifnull(url,''), ifnull(title,''), substr(ifnull(body,''),1,700) "
            "from docs where docs match ? and source=? limit ?",
            (expr, source, int(limit)),
        ).fetchall()
    except Exception:
        return []


def _one(con, expr: str, source: str, ref: str):
    try:
        rows = con.execute(
            "select ref, ifnull(url,''), ifnull(title,''), substr(ifnull(body,''),1,700) "
            "from docs where docs match ? and source=? and ref=? limit 1",
            (expr, source, ref),
        ).fetchall()
    except Exception:
        return None
    return rows[0] if rows else None


def _repo_ok(url: str, body: str) -> bool:
    blob = "{0}\n{1}".format(url or "", (body or "")[:500]).lower()
    return ("operator/" in blob) or ("hailports/" in blob) or ("github.com/operator" in blob) or ("github.com/hailports/" in blob)


def _repo_name(url: str) -> str:
    match = re.search(r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", url or "", re.I)
    if not match:
        return ""
    return "{0}/{1}#{2}".format(match.group(1), match.group(2), match.group(3))


def _state_line(body: str) -> str:
    match = re.search(r"(?im)^(?:state|status__c)\s*[:=]\s*\S[^\n]{0,40}", body or "")
    if not match:
        return ""
    return match.group(0).strip()


def _brain_words(item: dict) -> list:
    seed = str(item.get("seed") or item.get("text") or "")[:700]
    stop = {
        "ticket", "tickets", "operator", "operator", "update", "updated", "please",
        "thanks", "change", "changes", "status", "priority", "description", "request",
        "pull", "document", "summary", "review", "notes", "about", "their", "there",
        "which", "would", "could", "should", "user", "story", "closed", "active",
        "outcome", "draft", "actual", "decision", "rejected", "chosen",
    }
    words = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9]{5,18}", seed):
        low = word.lower()
        if low in stop or low in words:
            continue
        words.append(low)
        if len(words) >= 3:
            break
    return words


def _note(home: Path, wid: str) -> str:
    path = home / "llm-appliance/runtime/work-loop/ado-comment-cache" / "{0}.json".format(wid)
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(doc, dict):
        return ""
    text = _clip(str(doc.get("text") or ""), 500)
    if len(text) < 20 or _bad_text(text):
        return ""
    date = _clip(str(doc.get("date") or ""), 40)
    if date and not _bad_text(date):
        return date + "\n" + text
    return text

def _link_pieces(home: Path, item: dict) -> tuple:
    """Real linked pages for one item. Read only, and only a few short pieces."""
    seed = str(item.get("seed") or item.get("text") or "")
    label = str(item.get("label") or "")
    source = str(item.get("source") or "")
    own = str(item.get("ref") or "")
    cached = _PIECE_CACHE.get(label)
    if label and cached and time.time() - cached[0] < 600:
        return cached[1], cached[2]
    pieces = []
    seen = set()

    def add(kind: str, key: str, body: str) -> None:
        if len(pieces) >= 4 or not key or key in seen:
            return
        text = _mask_piece(body)
        if len(text) < 40:
            return
        seen.add(key)
        pieces.append("LINK {0}\n{1}".format(kind, text[:340]))

    try:
        con = _db(home)
    except Exception:
        return [], 0

    def add_comments(wid: str) -> None:
        if not wid.isdigit():
            return
        row = _one(con, wid, "ado", "comments_" + wid)
        if row:
            _ref, _url, title, body = row
            add("comments", "comments:" + wid, "{0}\n{1}".format(title, body))
            return
        note = _note(home, wid)
        if note:
            add("comments", "note:" + wid, note)

    def add_prs(wid: str, limit: int) -> None:
        if not wid.isdigit() or limit <= 0:
            return
        rows = _rows(con, "NEAR(ab {0}, 3)".format(wid), "github_pr", 8)
        opened = []
        other = []
        for ref, url, title, body in rows:
            if str(ref) == own:
                continue
            if "AB#{0}".format(wid) not in (body or "") and "AB#{0}".format(wid) not in (title or ""):
                continue
            if not _repo_ok(url, body):
                continue
            bucket = opened if re.search(r"(?i)\bstate:\s*open\b", body or "") else other
            bucket.append((ref, url, title, body))
        kept = 0
        for ref, url, title, body in opened + other:
            if kept >= limit or len(pieces) >= 4:
                break
            name = _repo_name(url) or str(ref)
            state = _state_line(body)
            add("pr", "pr:" + str(ref), "{0}\n{1}\n{2}\n{3}".format(name, state, title, body[:220]))
            kept += 1

    def add_sf(num: str, skip_ref: str, limit: int = 1) -> None:
        if not num or limit <= 0:
            return
        expr = '"SF-{0}"'.format(num)
        rows = _rows(con, expr, "operator", 6)
        exact = [row for row in rows if str(row[0]).upper() == "SF-" + num]
        other = [row for row in rows if row not in exact]
        # On an SF item the ticket itself is already the primary text.
        ordered = other if skip_ref.upper() == "SF-" + num else exact + other
        kept = 0
        for ref, _url, title, body in ordered:
            if str(ref) == skip_ref or str(ref) == own:
                continue
            add("operator", "sf:" + str(ref), "{0}\n{1}".format(title, body[:260]))
            kept += 1
            if kept >= limit or len(pieces) >= 4:
                break
        if len(pieces) >= 4 or kept >= limit:
            return
        for ref, _url, title, body in _rows(con, expr, "sf_chatter", 2):
            add("chatter", "chatter:" + str(ref)[:40], "{0}\n{1}".format(title, body[:220]))
            break

    def add_ado_for_sf(num: str) -> None:
        if not num:
            return
        expr = '"SF-{0}"'.format(num)
        kept = 0
        for ref, _url, title, body in _rows(con, expr, "ado", 6):
            if not str(ref).startswith("wi_"):
                continue
            if "SF-{0}".format(num) not in (body or "") and "SF-{0}".format(num) not in (title or ""):
                continue
            state = _state_line(body)
            add("ado", "ado:" + str(ref), "{0}\n{1}\n{2}".format(title, state, (body or "")[:180]))
            kept += 1
            if kept >= 2:
                break

    def add_parent(wid: str) -> None:
        parent = _parent_num(seed)
        if not parent or parent == wid:
            return
        row = _one(con, parent, "ado", "wi_" + parent)
        if not row:
            return
        _ref, _url, title, body = row
        add("parent", "parent:" + parent, "{0}\n{1}".format(title, _state_line(body)))

    def add_ado_brief(wid: str) -> None:
        if not wid.isdigit():
            return
        row = _one(con, wid, "ado", "wi_" + wid)
        if not row:
            return
        ref, _url, title, body = row
        add("ado", "ado:" + str(ref), "{0}\n{1}\n{2}".format(title, _state_line(body), (body or "")[:180]))

    def add_brain() -> None:
        words = _brain_words(item)
        if len(words) < 2:
            return
        expr = " ".join('"{0}"'.format(word) for word in words[:2])
        allow = ("ado", "operator", "github_pr", "sp_corpus", "zoom", "playbooks")
        kept = 0
        for source_name in allow:
            if kept >= 2 or len(pieces) >= 4:
                break
            for ref, url, title, body in _rows(con, expr, source_name, 3):
                if str(ref) == own:
                    continue
                blob = "{0}\n{1}\n{2}".format(url or "", title or "", ref)
                if re.search(r"(?i)(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)", blob):
                    continue
                add("work_brain", "wb:" + source_name + ":" + str(ref)[:48], "{0}\n{1}\n{2}".format(source_name, title, body[:180]))
                kept += 1
                if kept >= 2:
                    break

    try:
        wid = ""
        if label.startswith("ado-") and label[4:].isdigit():
            wid = label[4:]
        sf_label = ""
        if label.startswith("sf-") and label[3:].isdigit():
            sf_label = label[3:]
        if source == "ado" or wid:
            add_comments(wid)
            sf_ids = _sf_nums(seed)
            add_prs(wid, 2 if not sf_ids else 1)
            for num in sf_ids[:2]:
                add_sf(num, "")
            add_parent(wid)
            if len(pieces) < 2:
                add_prs(wid, 1)
        elif source == "operator" or sf_label:
            num = sf_label or (_sf_nums(seed)[:1] or [""])[0]
            add_ado_for_sf(num)
            add_sf(num, own if own else "SF-" + num, 2)
            add_prs_for = _wi_nums(seed)
            for rel in add_prs_for[:1]:
                add_prs(rel, 1)
                add_comments(rel)
            if len(pieces) < 1:
                add_brain()
        elif source == "github_pr" or label.startswith("pr-"):
            for rel in _wi_nums(seed)[:2]:
                add_ado_brief(rel)
                add_comments(rel)
            for num in _sf_nums(seed)[:2]:
                add_sf(num, "")
                add_ado_for_sf(num)
        else:
            for num in _sf_nums(seed)[:2]:
                add_ado_for_sf(num)
                add_sf(num, own)
            for rel in _wi_nums(seed)[:2]:
                add_ado_brief(rel)
                add_prs(rel, 1)
            if len(pieces) < 2:
                add_brain()
    finally:
        con.close()
    if label:
        _PIECE_CACHE[label] = (time.time(), pieces, len(pieces))
    return pieces, len(pieces)


def _gather(home: Path, item: dict) -> str:
    """Full context for one item, then packed so the prompt stays bounded."""
    seed = str(item.get("seed") or item.get("text") or "").strip()
    links, n_links = _link_pieces(home, item)
    primary_limit = 900 if links else _PRIMARY
    primary = ""
    if seed and not _bad_text(seed):
        primary = "PRIMARY\n" + seed[:primary_limit]
    elif seed[:ITEM_CHARS] and not _bad_text(seed[:ITEM_CHARS]):
        primary = "PRIMARY\n" + seed[:ITEM_CHARS]
    chunks = []
    used = 0
    if primary:
        chunks.append(primary)
        used = len(primary)
    packed_links = 0
    kinds = []
    for extra in links:
        room = _PACK - used - 2
        if room < 80:
            break
        piece = extra.strip()[:room]
        if len(piece) < 40 or _bad_text(piece):
            continue
        chunks.append(piece)
        used += len(piece) + 2
        if piece.startswith("LINK "):
            packed_links += 1
            kind = piece.split("\n", 1)[0].replace("LINK ", "", 1).strip()
            if kind and kind not in kinds:
                kinds.append(kind)
    packed = "\n\n".join(chunks)[:_PACK]
    item["ctx_gathered"] = len(seed) + sum(len(x) for x in links)
    item["ctx_links"] = packed_links if links else 0
    item["ctx_found"] = n_links
    item["ctx_kinds"] = ",".join(kinds)
    item["context"] = packed
    return packed

def _prompt(kind: str, items: list) -> str:
    item = items[0]
    ctx = item.get("context") or item.get("seed") or item.get("text") or ""
    line = _KIND_LINE.get(kind, _KIND_LINE["digest"])
    return (
        "Kind: {kind}\n{line}\n"
        "One item only. Fill every section, then stop.\n\n"
        "SOURCE\nITEM {label} kind={kind}\n{ctx}"
    ).format(kind=kind, line=line, label=item["label"], ctx=ctx)


def _make_batch(home: Path, items: list) -> dict:
    item = items[0]
    _gather(home, item)
    kind = item["kind"]
    return {
        "kind": kind,
        "items": [item],
        "prompt": _prompt(kind, [item]),
        "cap": _cap_for(kind, 1),
    }


def _prime_one(home: Path, item: dict) -> None:
    """Cache this item's context pack. No model call."""
    if int(_prime_one.n) >= 3:
        return
    _prime_one.n += 1
    try:
        import sys
        cli = str(home / "llm-appliance/cli")
        if cli not in sys.path:
            sys.path.insert(0, cli)
        import draft_context as dc
        dc.prebuild_item(item, home=home)
        _log("deepfill prime ref={0} ".format(item.get("label") or ""))
    except Exception as exc:
        _log("deepfill prime {0}".format(type(exc).__name__))


_prime_one.n = 0
_PIECE_CACHE = {}


def _build_batch(home: Path, depth: int = 0) -> dict | None:
    if depth > 40:
        return None
    _ensure(home)
    items = _take_items()
    if not items and depth == 0:
        _rebuild(home)
        items = _take_items()
    if not items:
        return None
    item = items[0]
    with _CAT_LOCK:
        done = dict(_CAT.get("done") or {})
    if _repeat_seen(done, item):
        item["fp"] = item.get("fp") or _fp(str(item.get("seed") or item.get("text") or ""))
        _mark(home, [item])
        _prime_one(home, item)
        _log("deepfill skip unchanged kind={0} ref={1}".format(item.get("kind"), item.get("label") or ""))
        return _build_batch(home, depth + 1)
    batch = _make_batch(home, items)
    item = items[0]
    thin = len(item.get("context") or "") < 40
    if thin or _bad_text(batch["prompt"]):
        _mark(home, items)
        _log("deepfill skip {0} kind={1} ref={2}".format(
            "thin" if thin else "hard", batch["kind"], item.get("label") or ""))
        return _build_batch(home, depth + 1)
    return batch


def _stale(done: dict, batch: dict) -> bool:
    """True when this prefetched item was already finished or did not change."""
    items = batch.get("items") or []
    if not items:
        return True
    item = items[0]
    if _repeat_seen(done, item) or _already(done, _hashes_from(done), item):
        return True
    return False


def _take_batch(home: Path) -> dict | None:
    with _PRE_LOCK:
        ready = _PRE["batch"]
        _PRE["batch"] = None
    if ready and not _bad_text(ready.get("prompt") or ""):
        with _CAT_LOCK:
            done = dict(_CAT.get("done") or {})
        if not _stale(done, ready):
            return ready
        _log("deepfill skip stale ref={0}".format((ready.get("items") or [{}])[0].get("label") or ""))
    return _build_batch(home)


def _warm_open(home: Path) -> None:
    """Gather the next open card while this call is still on the model."""
    import open_ado_loop as ol
    started = time.time()
    nxt = ol.warm_next(home)
    wid = str((nxt or {}).get("id") or "") if isinstance(nxt, dict) else ""
    if wid:
        body = str(nxt.get("body") or "")
        _link_pieces(home, {
            "label": "ado-" + wid,
            "source": "ado",
            "ref": "wi_" + wid,
            "seed": body,
            "text": body,
            "kind": "nextstep",
        })
    try:
        refreshed = ol.refresh_one(home)
    except Exception as exc:
        refreshed = type(exc).__name__
    if refreshed and refreshed != wid and refreshed in getattr(ol, "_DIRTY", ()):
        row = None
        try:
            state = ol._load(ol.state_path(home))
            for item in state.get("items") or []:
                if str(item.get("id") or "") == refreshed:
                    row = item
                    break
        except Exception:
            row = None
        body = str((row or {}).get("body") or "")
        _link_pieces(home, {
            "label": "ado-" + refreshed,
            "source": "ado",
            "ref": "wi_" + refreshed,
            "seed": body,
            "text": body,
            "kind": "nextstep",
        })
    try:
        import ticket_suggest as _ts
        _ts.refresh(home)
    except Exception as exc:
        _log("deepfill refresh {0}".format(type(exc).__name__))
    _log("deepfill warm next={0} refresh={1} ms={2}".format(
        wid or "-", refreshed or "-", int((time.time() - started) * 1000)))


def _prefetch(home: Path) -> None:
    try:
        _warm_open(home)
    except Exception as exc:
        _log("deepfill warm {0}".format(type(exc).__name__))
    try:
        batch = _build_batch(home)
    except Exception as exc:
        _log("deepfill prefetch {0}".format(type(exc).__name__))
        return
    if batch:
        item = batch["items"][0]
        _log("deepfill prefetched kind={0} ref={1} ctx={2} links={3} kinds={4} gathered={5}".format(
            batch["kind"], item.get("label") or "",
            len(item.get("context") or ""), item.get("ctx_links") or 0,
            item.get("ctx_kinds") or "-", item.get("ctx_gathered") or 0))
    with _PRE_LOCK:
        if _PRE["batch"] is None:
            _PRE["batch"] = batch
        elif batch is not None:
            _requeue(batch["items"])


def _requeue(items: list) -> None:
    with _CAT_LOCK:
        for item in reversed(items):
            _CAT["queues"].setdefault(item["kind"], []).insert(0, item)


def _mark(home: Path, items: list) -> None:
    with _CAT_LOCK:
        done = _CAT["done"] if isinstance(_CAT["done"], dict) else {}
        if not done:
            done = _load_done(home)
        for item in items:
            done[_key(item)] = {
                "hash": item["hash"], "rev": item["rev"], "fp": item.get("fp") or "",
                "ts": time.time(), "repeat": bool(item.get("repeat")),
            }
            _CAT["hashes"].add(_hash_key(item))
        _CAT["done"] = done
        counts = _CAT.get("counts") or {}
    try:
        _save_doc(home, done, {k: counts.get(k, 0) for k in _KINDS})
    except OSError as exc:
        _log("deepfill state {0}".format(type(exc).__name__))


def _allowed_out(path: Path) -> bool:
    home = _home()
    try:
        rp = path.resolve()
    except OSError:
        return False
    roots = [
        home / "work-brain-inbox",
        home / "claude-stack/data/idle_filler",
        home / "claude-stack/data/learning",
        home / "llm-appliance/runtime/work-loop/prep/items",
        home / "llm-appliance/runtime/support-playbooks/idle-runbooks",
    ]
    for root in roots:
        try:
            rp.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _write(path: Path, text: str) -> None:
    if not _allowed_out(path):
        raise PermissionError("path")
    if "OneDrive" in str(path):
        raise PermissionError("path")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _clean(body: str) -> str:
    import idle_filler as F
    body = F.scrub_str(F.strip_think(body))
    kept = []
    for line in body.splitlines():
        if line.strip() == _STOP:
            continue
        if F.blocked(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _repetitive(text: str) -> bool:
    """A loop, not a source phrase that shows up again in the self-check."""
    from collections import Counter
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 40]
    if len(lines) >= 8 and len(set(lines)) / float(len(lines)) < 0.7:
        return True
    if lines:
        top, count = Counter(lines).most_common(1)[0]
        if len(top) > 40 and count >= 3:
            return True
    compact = re.sub(r"\s+", " ", text)
    if len(compact) < 400:
        return False
    window = compact[200:320]
    if len(window) >= 100 and compact.count(window) >= 4:
        return True
    return False


def _checked(text: str) -> bool:
    return bool(re.search(r"(?i)self\s*-?\s*check", text or ""))


def _accept(raw: str) -> tuple:
    body = _clean(raw)
    if len(body) < 360:
        return "", "short"
    if _repetitive(body):
        return "", "repeat"
    if not _checked(body):
        return "", "nocheck"
    if not re.search(r"(?i)\b(result|not sent)\b", body):
        return "", "noresult"
    return body, "ok"


def _index(title: str, body: str) -> str:
    import sys
    home = _home()
    tools = str(home / "claude-stack")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from tools import work_context_index as wci
    ref = "idlefill_" + hashlib.sha1(body.encode("utf-8", "ignore")).hexdigest()[:16]
    con = wci._conn()
    try:
        wci._ensure_schema(con)
        wci._upsert(con, "digest", ref, time.time(), title[:180], "", body[:8000], "", 1)
        con.commit()
    finally:
        con.close()
    return ref


def _labels(batch: dict) -> list:
    return [item["label"] for item in batch["items"]]



def _fact_reply(body: str):
    """Drop a reply note that claims a change, or that pastes unrelated tracker noise."""
    import re
    import sys
    flagged = ""
    cleaned = body or ""
    cli = str(_home() / "llm-appliance" / "cli")
    if cli not in sys.path:
        sys.path.insert(0, cli)
    try:
        import fact_claim as fc
        new_text, hold = fc.outgoing(cleaned)
        if hold:
            cleaned = fc.pending_voice()
            flagged = "unproven_work"
        elif (new_text or "").strip() and (new_text or "").strip() != (body or "").strip():
            cleaned = new_text
            flagged = "prepare_only"
        confused = "SOURCE FACTS" in (body or "") and re.search(r"(?i)\bADO\b", body or "")
        if confused:
            cleaned = fc.pending_voice()
            flagged = "confused_context"
    except Exception:
        if "SOURCE FACTS" in (body or "") and "ADO" in (body or ""):
            cleaned = (
                "I have not changed operator. This note mixed an unrelated tracker item "
                "into a mail reply and was pulled. The request is still pending."
            )
            flagged = "confused_context"
    return cleaned, flagged


def _store(batch: dict, body: str) -> str:
    import idle_filler as F
    body, why = _accept(body)
    if not body:
        return ""
    kind = batch["kind"]
    flagged = ""
    if kind == "reply":
        body, flagged = _fact_reply(body)
    labels = _labels(batch)
    label = labels[0] if len(labels) == 1 else (labels[0] + "-plus")
    stamp = time.strftime("%Y%m%dT%H%M%S")
    name = "idlefill-{0}-{1}-{2}.md".format(kind, label, stamp)
    title = "idlefill {0} {1}".format(kind, label)
    doc = (
        "# {title}\n\nlocal only. not sent. not posted.\n\n"
        "items: {labels}\nrefs: {refs}\n\n{body}\n"
    ).format(
        title=title,
        labels=", ".join(labels),
        refs=", ".join(item["source"] + ":" + item["label"] for item in batch["items"]),
        body=body,
    )
    inbox = _home() / "work-brain-inbox" / name
    _write(inbox, doc)
    ref = ""
    try:
        ref = _index(title, doc)
    except Exception as exc:
        _log("deepfill index {0}".format(type(exc).__name__))
    if kind == "runbook":
        _write(
            _home() / "llm-appliance/runtime/support-playbooks/idle-runbooks" / "{0}.md".format(label),
            doc,
        )
    if kind == "reply":
        side = {
            "local_only": True,
            "staged_to_composer": False,
            "not_sent": True,
            "label": label,
            "ts": F.now_iso(),
            "body": body[:8000],
        }
        if flagged:
            side["flag"] = flagged
            side["staged_to_composer"] = False
        _write(
            _home() / "claude-stack/data/idle_filler/staged_replies" / "{0}.json".format(label),
            json.dumps(side),
        )
    for item in batch["items"]:
        wid = item["label"][4:] if item["label"].startswith("ado-") and item["source"] == "ado" else ""
        if not wid:
            continue
        prep_dir = _home() / "llm-appliance/runtime/work-loop/prep/items" / wid
        if prep_dir.is_dir() and prep_dir.name == wid:
            _write(prep_dir / "idlefill.md", doc)
    gap = " ".join(body.split())[:500]
    if len(gap) >= 80:
        try:
            F.append_jsonl(
                _home(),
                _home() / "claude-stack/data/learning/idle_shadow_lessons.jsonl",
                {
                    "ts": F.now_iso(),
                    "draft_id": hashlib.sha1(label.encode()).hexdigest()[:10],
                    "sent_id": kind,
                    "gap": gap,
                    "kind": "shadow",
                },
            )
        except OSError as exc:
            _log("deepfill lesson {0}".format(type(exc).__name__))
    return ref or name


def _tokens(resp: dict) -> int:
    for key in ("tokens_predicted", "completion_tokens"):
        if isinstance(resp.get(key), int):
            return resp[key]
    timings = resp.get("timings") or {}
    if isinstance(timings.get("predicted_n"), int):
        return timings["predicted_n"]
    return 0


def _record(home: Path, batch: dict, resp: dict, stored: str, reject: str) -> None:
    import idle_filler as F
    labels = _labels(batch)
    row = {
        "ts": F.now_iso(),
        "type": "deepfill_reject" if reject else "deepfill",
        "kind": batch["kind"],
        "item": labels[0],
        "refs": ",".join(labels),
        "n_items": len(batch["items"]),
        "completion_tokens": _tokens(resp),
        "seconds": resp.get("seconds"),
        "stop": resp.get("stop") or "",
        "reject": reject,
        "output": (stored or "")[:80],
    }
    F.append_jsonl(home, home / "claude-stack/data/idle_filler/metrics.jsonl", row)
    F.append_jsonl(home, home / "claude-stack/data/idle_filler/quality_index.jsonl", row)


def _store_async(home: Path, batch: dict, resp: dict) -> None:
    # The next completion has to grab the slot before this write touches the index.
    time.sleep(0.25)
    with _STORE_LOCK:
        try:
            text = str(resp.get("content") or "")
            body, why = _accept(text)
            if not body:
                _record(home, batch, resp, "", why or "reject")
                _log("deepfill reject kind={0} refs={1} why={2} tokens={3}".format(
                    batch["kind"], ",".join(_labels(batch)), why or "reject", _tokens(resp)))
                return
            stored = _store(batch, text)
            _record(home, batch, resp, stored, "")
            _log("deepfill stored kind={0} refs={1} tokens={2} stop={3}".format(
                batch["kind"], ",".join(_labels(batch)), _tokens(resp), resp.get("stop") or ""))
        except Exception as exc:
            _log("deepfill store {0}".format(type(exc).__name__))


_PEER = {"t": 0.0, "hit": False}


def _deferred() -> int:
    import urllib.request
    try:
        raw = urllib.request.urlopen("http://127.0.0.1:18080/metrics", timeout=0.4).read().decode("utf-8", "replace")
    except Exception:
        return 0
    for line in raw.splitlines():
        if line.startswith("llamacpp:requests_deferred "):
            try:
                return int(float(line.split()[-1]))
            except ValueError:
                return 0
    return 0


def _sched() -> dict:
    path = _home() / "claude-stack/data/runtime/chatprio/state.json"
    try:
        doc = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _chat_recent() -> bool:
    """A completion is in flight. An open `l` heartbeat is not."""
    doc = _sched()
    now = time.time()
    try:
        if int(doc.get("inflight") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    try:
        if now < float(doc.get("yield_until") or 0):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _priority_waiting() -> bool:
    """Draft runner (or any non-fill completion) is on the proxy."""
    doc = _sched()
    try:
        return int(doc.get("priority_jobs") or 0) > 0
    except (TypeError, ValueError):
        return False


def _ps_cmd(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "ppid=,command="],
            text=True, timeout=0.8, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def _draft_connected() -> bool:
    now = time.time()
    if now - _PEER["t"] < 0.8:
        return bool(_PEER["hit"])
    hit = False
    try:
        out = subprocess.check_output(
            ["/usr/sbin/lsof", "-nP", "-iTCP:8080", "-iTCP:18080"],
            text=True, timeout=2, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        out = ""
    pids = set()
    for line in out.splitlines():
        if "ESTABLISHED" not in line:
            continue
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        pids.add(int(parts[1]))
    pids.discard(os.getpid())
    for pid in pids:
        blob = _ps_cmd(pid)
        head = blob.split(None, 1)
        if head and head[0].isdigit():
            blob += " " + _ps_cmd(int(head[0]))
        if "ops_draft_immortal" in blob:
            hit = True
            break
    _PEER["t"] = now
    _PEER["hit"] = hit
    return hit


def _looping(text: str) -> bool:
    if len(text) < 500:
        return False
    return _repetitive(text[-1200:])


def _ended(text: str) -> bool:
    # Require the newline. A result line may begin with the end marker and
    # still have hundreds of characters to go. Matching the bare word closed
    # the socket and saved a stub.
    return bool(re.search(r"(?m)^ENDNOTE\n", text or ""))


def _piece(obj: dict) -> str:
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        if choice.get("text"):
            return str(choice.get("text"))
        delta = choice.get("delta") or choice.get("message") or {}
        if isinstance(delta, dict) and delta.get("content"):
            return str(delta.get("content"))
    if obj.get("content"):
        return str(obj.get("content"))
    return ""


def _render(user: str) -> str:
    """Same wrapper the chat template uses, with thinking left empty.

    The stable FASTCTX prefix is first so the slot can reuse it. The chat
    endpoint drops a caller grammar when the template has its own. The plain
    completion endpoint keeps it, which is what makes the note finish at a
    real length instead of stopping after a short stub.
    """
    user = user.replace("<|im_start|>", "").replace("<|im_end|>", "")
    system = _speed_prefix() + "\n\n" + _SYSTEM
    return (
        "<|im_start|>system\n{system}<|im_end|>\n"
        "<|im_start|>user\n{user}<|im_end|>\n"
        "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    ).format(system=system, user=user)


def _complete_direct(prompt: str, cap: int, grammar: str | None = None) -> dict:  # TICKETSUG-20261104
    """Stream straight to llama-server. Closing the socket stops generation.

    Thinking is off, same as a normal local reply. A raw completion was spending
    the whole cap inside a think block and saving only the leftover line.
    """
    body = json.dumps({
        "model": "Qwen3.8-27B-oQ3e-mtp",
        "prompt": _render(prompt),
        "max_tokens": cap,
        "temperature": 0.2,
        "stream": True,
        "cache_prompt": True,
        "grammar": grammar or _GRAMMAR,
    }).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", 18080, timeout=30)
    stop = threading.Event()
    why = {"v": ""}
    owned = {"t": 0.0}

    def halt(reason: str) -> None:
        if why["v"]:
            return
        why["v"] = reason
        try:
            if conn.sock is not None:
                conn.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

    def watch() -> None:
        next_draft = 0.0
        while not stop.wait(0.05):
            reason = ""
            if _chat_recent():
                reason = "chat"
            elif _priority_waiting():
                reason = "draft"
            else:
                now = time.time()
                if owned["t"] and now - owned["t"] >= 0.2 and now >= next_draft:
                    next_draft = now + 0.4
                    if _deferred() > 0 and _draft_connected():
                        reason = "draft"
            if reason:
                halt(reason)
                return

    t0 = time.time()
    parts = []
    last = {}
    try:
        conn.request("POST", "/v1/completions", body=body, headers={"Content-Type": "application/json"})
        if conn.sock is not None:
            conn.sock.settimeout(TIMEOUT_S)
        threading.Thread(target=watch, name="idle-fill-yield", daemon=True).start()
        resp = conn.getresponse()
        owned["t"] = time.time()
        if getattr(resp, "status", 200) != 200:
            err = ""
            try:
                err = resp.read(180).decode("utf-8", "replace")
            except Exception:
                err = ""
            err = re.sub(r"\s+", " ", err)[:140]
            _log("deepfill http {0} {1}".format(resp.status, err))
            return {
                "error": "http{0}".format(resp.status),
                "content": "",
                "seconds": round(time.time() - t0, 2),
            }
        buf = b""
        next_touch = time.time() + 2
        while True:
            chunk = resp.read(1024)
            if not chunk:
                break
            if time.time() >= next_touch:
                _touch()
                next_touch = time.time() + 2
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    buf = b""
                    break
                try:
                    obj = json.loads(payload.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                last = obj
                piece = _piece(obj)
                if piece:
                    parts.append(piece)
                text_now = "".join(parts)
                usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
                predicted = obj.get("tokens_predicted")
                if not isinstance(predicted, int):
                    predicted = usage.get("completion_tokens")
                if _ended(text_now):
                    halt("end")
                    break
                if (isinstance(predicted, int) and predicted >= cap) or len(parts) >= cap:
                    halt("cap")
                    break
            else:
                continue
            break
        text = "".join(parts)
        if _ended(text) and why["v"] in {"", "end", "chat", "draft"}:
            why["v"] = "end"
        n = len(parts)
        timings = last.get("timings") or {}
        if isinstance(timings.get("predicted_n"), int):
            n = timings["predicted_n"]
        elif isinstance(last.get("tokens_predicted"), int):
            n = last["tokens_predicted"]
        else:
            usage = last.get("usage") if isinstance(last.get("usage"), dict) else {}
            if isinstance(usage.get("completion_tokens"), int):
                n = usage["completion_tokens"]
        stop_reason = why["v"] or "eos"
        if why["v"] in {"chat", "draft"} and not _ended(text):
            return {
                "busy": True, "why": why["v"], "content": text,
                "seconds": round(time.time() - t0, 2), "tokens_predicted": n, "stop": why["v"],
            }
        if stop_reason == "cap" or (n >= cap and stop_reason not in {"end", "repeat"}):
            stop_reason = "cap"
        cache_n = timings.get("cache_n") if isinstance(timings, dict) else None
        return {
            "content": text,
            "seconds": round(time.time() - t0, 2),
            "tokens_predicted": n,
            "stop": stop_reason,
            "cache_n": cache_n if isinstance(cache_n, int) else 0,
        }
    except Exception as exc:
        text = "".join(parts)
        if why["v"] in {"chat", "draft"} and not _ended(text):
            return {
                "busy": True, "why": why["v"], "content": text,
                "seconds": round(time.time() - t0, 2),
                "tokens_predicted": max(1, len(parts)) if text else 0,
                "stop": why["v"],
            }
        if why["v"] in {"end", "cap", "repeat"} or _ended(text):
            return {
                "content": text,
                "seconds": round(time.time() - t0, 2),
                "tokens_predicted": max(1, len(parts)) if text else 0,
                "stop": "end" if _ended(text) else why["v"],
            }
        return {"error": type(exc).__name__, "content": text, "seconds": round(time.time() - t0, 2)}
    finally:
        stop.set()
        try:
            conn.close()
        except Exception:
            pass


def _finish(home: Path, batch: dict, resp: dict, partial: bool) -> str:
    text = str(resp.get("content") or "")
    if resp.get("busy") and not partial:
        _requeue(batch["items"])
        _log("deepfill yield kind={0} refs={1} why={2}".format(
            batch["kind"], ",".join(_labels(batch)), resp.get("why") or ""))
        return "busy"
    body, why = _accept(text)
    if not body:
        if resp.get("busy") or "<think>" in text or "</think>" in text:
            _requeue(batch["items"])
            _log("deepfill yield kind={0} refs={1} why={2}".format(
                batch["kind"], ",".join(_labels(batch)),
                resp.get("why") or ("think" if "<think>" in text or "</think>" in text else why or "")))
            return "busy" if resp.get("busy") else "ran"
        # The source did not change. Do not run the same item again.
        if why == "repeat":
            for item in batch["items"]:
                item["repeat"] = True
                item["fp"] = item.get("fp") or _fp(str(item.get("seed") or item.get("text") or ""))
        _mark(home, batch["items"])
        _loop_done(home, batch, body)
        _log("deepfill reject kind={0} refs={1} why={2} tokens={3} stop={4}".format(
            batch["kind"], ",".join(_labels(batch)), why or "reject",
            _tokens(resp), resp.get("stop") or ""))
        try:
            _record(home, batch, resp, "", why or "reject")
        except Exception as exc:
            _log("deepfill metric {0}".format(type(exc).__name__))
        return "ran"
    _mark(home, batch["items"])
    _loop_done(home, batch, body)
    _log("deepfill ran kind={0} refs={1} n=1 tokens={2} seconds={3} stop={4} passes={5} cache={6}".format(
        batch["kind"], ",".join(_labels(batch)), _tokens(resp), resp.get("seconds"),
        resp.get("stop") or "", resp.get("passes") or 1, resp.get("cache_n") or 0))
    threading.Thread(
        target=_store_async, args=(home, batch, resp), name="idle-fill-store", daemon=True,
    ).start()
    if partial:
        _log("deepfill partial kind={0} refs={1} chars={2}".format(
            batch["kind"], ",".join(_labels(batch)), len(text)))
    return "ran"


def _verify_prompt(batch: dict, draft: str) -> str:
    item = batch["items"][0]
    ctx = _clip(item.get("context") or item.get("text") or "", 1400)
    draft = _clip(draft, 1800)
    return (
        "Same one item. Do not start another item. The draft missed SELF-CHECK.\n"
        "Write the heading SELF-CHECK and six lines. Each line is one claim, then SUPPORTED or CUT, "
        "then a short phrase from the source. If you cut a claim, add CORRECTED RESULT.\n"
        "End with a line that is exactly ENDNOTE.\n\n"
        "SOURCE\n{0}\n\nDRAFT\n{1}"
    ).format(ctx, draft)


def _merge_resp(first: dict, second: dict) -> dict:
    text = (str(first.get("content") or "").rstrip() + "\n" + str(second.get("content") or "").lstrip()).strip()
    return {
        "content": text,
        "seconds": round(float(first.get("seconds") or 0) + float(second.get("seconds") or 0), 2),
        "tokens_predicted": _tokens(first) + _tokens(second),
        "stop": second.get("stop") or first.get("stop") or "",
        "passes": 2,
    }


def _prefer(batch: dict) -> None:
    """Resume this item before the one that was prefetched."""
    with _PRE_LOCK:
        nxt = _PRE["batch"]
        _PRE["batch"] = batch
    if nxt is not None and nxt is not batch:
        _requeue(nxt["items"])



def _run_suggest(home, job: dict) -> str:
    """One suggestion draft. Does not create a work item."""
    os.environ["CHATPRIO_ROLE"] = "background"
    label = str((job.get("pending") or {}).get("source_key") or "")[:8]
    _log("suggest start key={0}".format(label))
    resp = _complete_direct(job.get("prompt") or "", int(job.get("cap") or 500), job.get("grammar"))
    _touch()
    if resp.get("busy"):
        _log("suggest yield why={0}".format(resp.get("why") or ""))
        return "busy"
    try:
        import ticket_suggest as _ts
        result = _ts.finish(home, job, str(resp.get("content") or ""))
    except Exception as exc:
        _log("suggest finish {0}".format(type(exc).__name__))
        return "error"
    _log("suggest done status={0}".format((result or {}).get("status") or ""))
    return "ran"


def _loop_done(home, batch: dict, text: str) -> None:
    if str((batch or {}).get("kind") or "") != "nextstep":
        return
    try:
        import open_ado_loop as _ol
        item = (batch.get("items") or [{}])[0]
        _ol.saved(home, item, text or "", bool((text or "").strip()))
    except Exception as exc:
        _log("openloop save {0}".format(type(exc).__name__))


def run(app) -> str:
    _touch()
    _prime_one.n = 0
    home = app.home
    if _chat_recent():
        _log("deepfill hold chat")
        return "busy"
    if _priority_waiting():
        _log("deepfill hold draft")
        return "busy"
    if _slot_busy() and _draft_connected():
        _log("deepfill hold draft")
        return "busy"
    batch = None
    try:
        import ticket_suggest as _ts
        if _ts.due(home):
            job = _ts.claim(home, force=True)
            if job:
                return _run_suggest(home, job)
    except Exception as exc:
        _log("suggest {0}".format(type(exc).__name__))
    try:
        import open_ado_loop as _ol
        nxt = _ol.claim(home)
        if nxt:
            batch = _make_batch(home, [nxt])
            if _bad_text(batch.get("prompt") or ""):
                _ol.saved(home, nxt, "", False)
                _log("openloop skip hard ref={0}".format(nxt.get("label") or ""))
                batch = None
    except Exception as exc:
        _log("openloop {0}".format(type(exc).__name__))
        batch = None
    if not batch:
        try:
            import ticket_suggest as _ts
            job = _ts.claim(home, force=True)
            if job:
                return _run_suggest(home, job)
        except Exception as exc:
            _log("suggest {0}".format(type(exc).__name__))
    if not batch:
        try:
            batch = _take_batch(home)
        except Exception as exc:
            _log("deepfill batch {0}".format(type(exc).__name__))
            return "error"
    if not batch:
        try:
            import sys
            cli = str(home / "llm-appliance/cli")
            if cli not in sys.path:
                sys.path.insert(0, cli)
            import draft_context as dc
            n = dc.prebuild_open(home, limit=2)
            _log("deepfill prime open n={0}".format(n))
        except Exception as exc:
            _log("deepfill prime {0}".format(type(exc).__name__))
        _log("deepfill empty")
        return "empty"
    os.environ["CHATPRIO_ROLE"] = "background"
    os.environ["IDLE_FILL_CAP"] = str(batch["cap"])
    threading.Thread(target=_prefetch, args=(home,), name="idle-fill-prefetch", daemon=True).start()
    _touch()
    item = batch["items"][0]
    _log("deepfill start kind={0} n=1 refs={1} prompt_chars={2} ctx={3} links={4} kinds={5} gathered={6} max={7}".format(
        batch["kind"], item.get("label") or "", len(batch["prompt"]),
        len(item.get("context") or ""), item.get("ctx_links") or 0,
        item.get("ctx_kinds") or "-", item.get("ctx_gathered") or 0, batch["cap"]))
    resp = _complete_direct(batch["prompt"], int(batch["cap"]))
    _touch()
    if resp.get("busy"):
        text = str(resp.get("content") or "")
        if _accept(text)[0]:
            return _finish(home, batch, resp, partial=True)
        _prefer(batch)
        _log("deepfill yield kind={0} refs={1} why={2}".format(
            batch["kind"], ",".join(_labels(batch)), resp.get("why") or ""))
        return "busy"
    if resp.get("error") and not str(resp.get("content") or "").strip():
        _log("deepfill error {0}".format(resp.get("error")))
        _requeue(batch["items"])
        return "error"
    n = _tokens(resp)
    if n <= 0 and not str(resp.get("content") or "").strip():
        _log("deepfill empty-response")
        _requeue(batch["items"])
        return "error"
    text = str(resp.get("content") or "")
    resp["passes"] = 1
    if not _checked(text) and resp.get("stop") != "repeat" and not _repetitive(text):
        _log("deepfill check kind={0} refs={1}".format(batch["kind"], item.get("label") or ""))
        extra = _complete_direct(_verify_prompt(batch, text), _CHECK_CAP)
        _touch()
        if extra.get("busy"):
            _prefer(batch)
            _log("deepfill yield kind={0} refs={1} why={2}".format(
                batch["kind"], item.get("label") or "", extra.get("why") or "check"))
            return "busy"
        if str(extra.get("content") or "").strip():
            resp = _merge_resp(resp, extra)
    return _finish(home, batch, resp, partial=False)


def _slot_busy():
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:18080/slots", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    rows = data if isinstance(data, list) else data.get("slots") or []
    if not rows:
        return None
    return any(bool(row.get("is_processing")) for row in rows if isinstance(row, dict))


def _watch() -> None:
    idle_since = 0.0
    log_path = _home() / "Library/Logs/claude-stack/idle-fill-watchdog.log"
    while True:
        time.sleep(10)
        _touch()
        busy = _slot_busy()
        if busy is None or busy:
            idle_since = 0.0
            continue
        if _chat_recent() or _priority_waiting():
            idle_since = 0.0
            continue
        if idle_since <= 0:
            idle_since = time.time()
            continue
        if time.time() - idle_since < 60:
            continue
        line = time.strftime("%Y-%m-%dT%H:%M:%S") + " alert slot idle >60s with nothing interactive waiting\n"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as fh:
                fh.write(line)
        except OSError:
            pass
        _log("watchdog idle>60s")
        try:
            (_home() / "claude-stack/data/idle_filler/force_run").write_text(str(time.time()))
        except OSError:
            pass
        idle_since = time.time()


def start_watchdog() -> None:
    with _LOCK:
        if _STARTED["watch"]:
            return
        _STARTED["watch"] = True
    threading.Thread(target=_watch, name="idle-fill-watch", daemon=True).start()


def _self_check() -> None:
    assert _repetitive(("this exact line repeats over and over again\n" * 12))
    fresh = "\n".join("line {0} talks about a different fact in the source".format(i) for i in range(12))
    assert not _repetitive(fresh)
    done = {"digest|ado|wi_1": {"hash": "abc", "rev": "9"}}
    hashes = _hashes_from(done)
    same = {"kind": "digest", "source": "ado", "ref": "wi_1", "hash": "abc", "rev": "9"}
    changed = {"kind": "digest", "source": "ado", "ref": "wi_1", "hash": "zzz", "rev": "10"}
    assert _already(done, hashes, same)
    assert not _already(done, hashes, changed)
    fp_done = {"digest|ado|wi_1": {"hash": "old", "rev": "1|stamp", "fp": "samefp"}}
    fp_item = {"kind": "digest", "source": "ado", "ref": "wi_1", "hash": "new", "rev": "2|stamp", "fp": "samefp"}
    assert _already(fp_done, _hashes_from(fp_done), fp_item)
    fresh = {"kind": "digest", "source": "ado", "ref": "wi_9", "hash": "new", "rev": "2|other", "fp": "other"}
    assert not _repeat_seen(fp_done, fresh)
    moved = {"kind": "digest", "source": "ado", "ref": "wi_1", "hash": "new", "rev": "2|other", "fp": "changed"}
    assert not _repeat_seen(fp_done, moved)
    legacy = {"kind": "digest", "source": "ado", "ref": "wi_1", "hash": "new", "rev": "2|other", "fp": "words"}
    assert _repeat_seen({"digest|ado|wi_1": {"hash": "old", "rev": "1|"}}, legacy)
    assert MAX_ITEMS == 1
    assert _cap_for("digest", 1) == _CAPS["digest"]
    assert _cap_for("digest", 4) == _CAPS["digest"]
    assert _CAPS["digest"] < 6000
    one = {
        "kind": "digest",
        "label": "ado-1",
        "text": "State: Active\nA pump order form is open.",
        "context": "PRIMARY\nState: Active\nA pump order form is open.",
    }
    prompt = _prompt("digest", [one])
    assert prompt.count("ITEM ") == 1
    assert "SELF-CHECK" in _SYSTEM
    assert "rline{16}" in _GRAMMAR
    assert _GRAMMAR.count("rline{12}") == 0
    rendered = _render("one item")
    assert rendered.startswith("<|im_start|>system\nFASTCTX\n")
    assert rendered.index("FASTCTX") < rendered.index("one item")
    stale_done = {"reply|ado|wi_1": {"hash": "abc", "rev": "9", "fp": "same"}}
    stale_item = {"kind": "reply", "source": "ado", "ref": "wi_1", "hash": "abc", "rev": "9", "fp": "same"}
    assert _stale(stale_done, {"items": [stale_item]})
    assert not _stale({}, {"items": [stale_item]})
    assert "style" in _KINDS
    assert "fact{6}" in _GRAMMAR
    assert "ENDNOTE" in _GRAMMAR
    assert _GRAMMAR.count("root ::=") == 1
    assert "ado-1" in prompt
    assert _checked("SELF-CHECK\nclaim SUPPORTED source")
    assert not _checked("just a brief")
    import open_ado_loop as _ol
    import ticket_suggest as _ts
    _ol.self_check()
    _ts.self_check()


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(_home() / "claude-stack/tools"))
    if "--check" in sys.argv:
        _self_check()
        print("check ok")
        raise SystemExit(0)
    if "--dry" in sys.argv:
        counts = _rebuild(_home(), persist=False)
        print("dry", " ".join("{0}={1}".format(k, counts.get(k, 0)) for k in list(_KINDS) + ["skip_done", "skip_hard"]))
        raise SystemExit(0)
    raise SystemExit(0)
