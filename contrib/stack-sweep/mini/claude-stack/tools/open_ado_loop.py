#!/usr/bin/env python3
"""Eternal lap over open work items assigned to the operator.

No model call lives here. The filler asks for one changed item. An unchanged
item is skipped. After the last item the next lap starts at the first.
Searches are read only. A operator or tracker change is only written into
the local next-step file, and it waits for a person to say go.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

CLOSED = ("resolved", "closed", "removed", "done")
OPERATOR = ("operator", "operator")
HARD = re.compile(
    r"(?i)(legal|finance|lawyer|attorney|accountant|\bcpa\b|bank|"
    r"password|passcode|seed phrase|verification code|\b2fa\b|\bmfa\b|\botp\b)"
)
HARD_PATH = re.compile(r"(?i)(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)")
_STATE_LINE = re.compile(r"(?m)^State:\s*(.+)$")
_ASSIGNED = re.compile(r"(?m)^Assigned to:\s*(.+)$")
_CHANGED = re.compile(r"(?m)^Changed:\s*(.+)$")
_PARENT = re.compile(r"(?m)^Parent:\s*(\d{3,8})\s*$")
_SF = re.compile(r"\bSF-(\d{3,8})\b", re.I)
_WI = re.compile(r"\b(?:AB#|wi_)?(\d{5,8})\b")
_REPO = re.compile(r"operator/operator-operator", re.I)
MAIL_CAP = 10


def home_of(home: Path | None = None) -> Path:
    return home if home is not None else Path.home()


def state_path(home: Path) -> Path:
    return home / "claude-stack/data/idle_filler/open_loop_state.json"


def artifact_path(home: Path, wid: str) -> Path:
    return home / "claude-stack/data/idle_filler/next_steps" / "{0}.json".format(wid)


def log_path(home: Path) -> Path:
    return home / "claude-stack/data/idle_filler/open_loop.jsonl"


def hard_blocked(text: str, url: str = "") -> bool:
    blob = "{0}\n{1}".format(url or "", text or "")
    if HARD_PATH.search(blob):
        return True
    return bool(HARD.search(blob))


def base_state(body: str) -> str:
    match = _STATE_LINE.search(body or "")
    raw = match.group(1).strip() if match else ""
    return raw.split("(", 1)[0].strip()


def is_closed(state: str) -> bool:
    return base_state(state).lower() in CLOSED or (state or "").split("(", 1)[0].strip().lower() in CLOSED


def assigned_name(body: str) -> str:
    match = _ASSIGNED.search(body or "")
    return match.group(1).strip() if match else ""


def is_operator(text: str, tokens: tuple = OPERATOR) -> bool:
    blob = (text or "").casefold()
    return any(tok in blob for tok in tokens)


def changed_stamp(body: str) -> str:
    match = _CHANGED.search(body or "")
    return (match.group(1).strip() if match else "")[:40]


def select_open(rows: list, tokens: tuple = OPERATOR) -> list:
    """rows: dicts with id, title, body, state optional."""
    out = []
    seen = set()
    for row in rows or []:
        body = str(row.get("body") or "")
        state = str(row.get("state") or base_state(body))
        if is_closed(state):
            continue
        if hard_blocked(body, str(row.get("url") or "")):
            continue
        who = assigned_name(body) or str(row.get("assigned") or "")
        # Created-by alone is not enough. Assigned, a real mention, or an
        # active/testing item the operator last changed.
        active = state.lower().startswith("active") or state.lower().startswith("testing")
        last = bool(re.search(r"(?im)^Last changed by:.*(" + "|".join(tokens) + ")", body))
        keep = is_operator(who, tokens) or _mention_line(body, tokens) or (active and last)
        if not keep:
            continue
        wid = str(row.get("id") or "")
        if not wid.isdigit() or wid in seen:
            continue
        seen.add(wid)
        out.append({
            "id": wid,
            "title": str(row.get("title") or "")[:180],
            "state": state,
            "body": body[:4000],
            "url": str(row.get("url") or ""),
            "changed": changed_stamp(body),
        })
    out.sort(key=lambda item: item["id"])
    return out


def _mention_line(body: str, tokens: tuple) -> bool:
    for line in (body or "").splitlines():
        if re.match(r"(?i)^(created by|last changed by):", line):
            continue
        if is_operator(line, tokens):
            return True
    return False


def fingerprint(parts: dict) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]


def step_cursor(state: dict, order: list, fp_of) -> dict:
    """Walk until one changed item, or finish the lap.

    fp_of(wid) -> fingerprint string. Unchanged items advance. A changed
    item stays on the cursor until the caller records the fingerprint.
    """
    state = dict(state or {})
    order = [str(x) for x in (order or [])]
    if not order:
        state["cursor"] = 0
        return {"action": "empty", "state": state, "skips": 0}
    start = int(state.get("cursor") or 0) % len(order)
    fps = dict(state.get("fp") or {})
    skips = 0
    for offset in range(len(order)):
        idx = (start + offset) % len(order)
        wid = order[idx]
        current = str(fp_of(wid) or "")
        if fps.get(wid) == current and current:
            skips += 1
            continue
        state["cursor"] = idx
        state["pending"] = wid
        state["fp"] = fps
        return {"action": "work", "id": wid, "fp": current, "state": state, "skips": skips}
    state["cursor"] = 0
    state["pending"] = ""
    state["fp"] = fps
    state["laps"] = int(state.get("laps") or 0) + 1
    return {"action": "lap", "state": state, "skips": skips}


def remember_fp(state: dict, wid: str, fp: str) -> dict:
    state = dict(state or {})
    fps = dict(state.get("fp") or {})
    fps[str(wid)] = str(fp)
    state["fp"] = fps
    order_len = int(state.get("n") or 0)
    if order_len:
        state["cursor"] = (int(state.get("cursor") or 0) + 1) % order_len
    state["pending"] = ""
    state["served"] = int(state.get("served") or 0) + 1
    return state


def material(prev: dict | None, new: dict) -> bool:
    if not prev:
        return False
    old_refs = {str(x.get("ref")) for x in (prev.get("found") or [])}
    new_refs = {str(x.get("ref")) for x in (new.get("found") or [])}
    if new_refs - old_refs:
        return True
    if str(prev.get("revision") or "") != str(new.get("revision") or ""):
        return True
    if str(prev.get("state") or "") != str(new.get("state") or ""):
        return True
    return False


def mail_choice(prev: dict | None, artifact: dict, sent_today: int, cap: int = MAIL_CAP) -> str:
    """baseline writes the first packet and does not mail.

    A later lap mails once per tracker revision when something material
    showed up. The same revision only updates the packet.
    """
    if not prev:
        return "baseline"
    revision = str(artifact.get("revision") or "")
    if revision and revision == str(prev.get("emailed_rev") or ""):
        return "update"
    if not material(prev, artifact):
        return "update"
    if sent_today >= cap:
        return "cap"
    return "email"


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def blocking_line(body: str, hits: list) -> str:
    for line in (body or "").splitlines():
        if re.search(r"(?i)\b(block|waiting|cannot|can't|need|unblocked)\b", line):
            cleaned = _clip(line, 180)
            if cleaned and not hard_blocked(cleaned):
                return cleaned
    state = base_state(body)
    if hits:
        return "Open ({0}). A related note is in {1}.".format(state or "open", hits[0].get("source") or "the index")
    return "Open ({0}). No newer note in the index on this lap.".format(state or "open")


def next_action(body: str, hits: list) -> str:
    if hits:
        hit = hits[0]
        return "Use {0} {1} on the open question, then update this item.".format(
            hit.get("source") or "the note", hit.get("ref") or ""
        )
    parent = _PARENT.search(body or "")
    if parent:
        return "Check parent {0} for the decision this item is waiting on.".format(parent.group(1))
    return "The description is unchanged. The next action is the open question already written on the item."


def prepared_change(hits: list, action: str) -> dict:
    blob = " ".join(str(h.get("note") or "") for h in hits[:3]).lower()
    if any(h.get("source") == "pull" for h in hits):
        kind = "pr_plan"
    elif "operator" in blob or any(h.get("source") == "record" for h in hits):
        kind = "sf_prepare"
    elif any(h.get("source") in ("mail", "chat") for h in hits):
        kind = "reply"
    else:
        kind = "note"
    return {"kind": kind, "needs_go": True, "text": _clip(action, 240)}


def build_artifact(wid: str, body: str, hits: list, revision: str, fp: str, note: str = "") -> dict:
    action = next_action(body, hits)
    return {
        "id": str(wid),
        "revision": revision,
        "state": base_state(body),
        "blocking": blocking_line(body, hits),
        "found": [
            {"source": h.get("source"), "ref": h.get("ref"), "note": _clip(h.get("note") or "", 160)}
            for h in hits[:6]
        ],
        "next_action": action,
        "prepared": prepared_change(hits, action),
        "fp": fp,
        "note": _clip(note, 400),
        "at": int(time.time()),
    }


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    tmp.replace(path)


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def _db(home: Path):
    import sqlite3
    path = home / "claude-stack/data/work_context.db"
    con = sqlite3.connect("file:{0}?mode=ro".format(path), uri=True, timeout=10)
    con.execute("PRAGMA query_only=ON")
    return con


def _rows(con, expr: str, source: str, limit: int = 6) -> list:
    try:
        return con.execute(
            "select ref, url, title, substr(body,1,400), ts from docs "
            "where docs match ? and source=? order by ts desc limit ?",
            (expr, source, int(limit)),
        ).fetchall()
    except Exception:
        return []


def collect_hits(con, wid: str, body: str) -> list:
    """Read-only related notes. Hard-line rows are dropped, not returned."""
    hits = []
    seen = set()

    def add(source: str, ref: str, note: str, url: str = "") -> None:
        key = source + "|" + str(ref)
        if key in seen or not ref:
            return
        if hard_blocked(note, url):
            return
        seen.add(key)
        hits.append({"source": source, "ref": str(ref)[:80], "note": _clip(note, 180)})

    expr = '"{0}"'.format(wid)
    for ref, url, title, text, _ts in _rows(con, expr, "github_pr", 6):
        blob = "{0}\n{1}\n{2}".format(url or "", title or "", text or "")
        if not _REPO.search(blob):
            continue
        if wid not in blob and "AB#{0}".format(wid) not in blob:
            continue
        add("pull", str(ref), "{0} {1}".format(title or "", _clip(text, 120)), url or "")
    for ref, url, title, text, _ts in _rows(con, expr, "ado", 6):
        if str(ref) in ("wi_" + wid, "comments_" + wid):
            continue
        add("tracker", str(ref), "{0}".format(title or ""), url or "")
    for num in _SF.findall(body or "")[:3]:
        for ref, url, title, text, _ts in _rows(con, '"SF-{0}"'.format(num), "operator", 3):
            add("record", str(ref), "{0}".format(title or ""), url or "")
    for source, label in (("email", "mail"), ("zoom", "chat"), ("sp_corpus", "work_brain"), ("playbooks", "runbook")):
        for ref, url, title, text, _ts in _rows(con, expr, source, 3):
            add(label, str(ref)[:40], "{0}".format(title or ""), url or "")
    parent = _PARENT.search(body or "")
    if parent:
        add("tracker", "wi_" + parent.group(1), "parent " + parent.group(1))
    comment = home_comment(con, wid)
    if comment:
        add("comment", "comments_" + wid, comment)
    return hits[:8]


def home_comment(con, wid: str) -> str:
    row = None
    try:
        row = con.execute(
            "select substr(body,1,240) from docs where source='ado' and ref=?",
            ("comments_" + wid,),
        ).fetchone()
    except Exception:
        row = None
    if row and row[0] and not hard_blocked(row[0]):
        return _clip(row[0], 180)
    return ""


def load_open_items(home: Path) -> list:
    con = _db(home)
    try:
        rows = con.execute(
            "select ref, title, url, substr(body,1,2500) from docs "
            "where source='ado' and ref like 'wi_%' "
            "and body not like '%State: Resolved%' "
            "and body not like '%State: Closed%' "
            "and body not like '%State: Removed%'"
        ).fetchall()
    finally:
        con.close()
    parsed = []
    for ref, title, url, body in rows:
        wid = str(ref)[3:] if str(ref).startswith("wi_") else str(ref)
        if not wid.isdigit():
            continue
        parsed.append({"id": wid, "title": title or "", "url": url or "", "body": body or ""})
    return select_open(parsed, OPERATOR)


def _comment_fp(home: Path, wid: str) -> str:
    path = home / "llm-appliance/runtime/work-loop/ado-comment-cache" / "{0}.json".format(wid)
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if hard_blocked(raw[:400].decode("utf-8", "replace")):
        return "blocked"
    return hashlib.sha1(raw).hexdigest()[:12]


_HIT_CACHE = {}
_DIRTY = set()


def cheap_sig(home: Path, item: dict) -> str:
    """Cheap change check. A hit search is not repeated while this stays the same."""
    wid = str(item.get("id") or "")
    path = home / "llm-appliance/runtime/work-loop/ado-comment-cache" / "{0}.json".format(wid)
    try:
        st = path.stat()
        comment = "{0}:{1}".format(getattr(st, "st_mtime_ns", int(st.st_mtime)), st.st_size)
    except OSError:
        comment = ""
    return fingerprint({
        "id": wid,
        "state": item.get("state") or "",
        "changed": item.get("changed") or "",
        "title": item.get("title") or "",
        "comment": comment,
    })


def _hits_for(home: Path, wid: str, item: dict, sig: str) -> list:
    cached = _HIT_CACHE.get(wid)
    now = time.time()
    if cached and cached.get("cheap") == sig and now - float(cached.get("at") or 0) < 600:
        return list(cached.get("hits") or [])
    try:
        con = _db(home)
        try:
            hits = collect_hits(con, wid, item.get("body") or "")
        finally:
            con.close()
    except Exception:
        hits = []
    _HIT_CACHE[wid] = {"at": time.time(), "hits": hits, "cheap": sig}
    return hits


def upcoming(home: Path) -> dict | None:
    """Next card claim() will work. Unchanged stamps are skipped."""
    home = home_of(home)
    state = _load(state_path(home))
    items = state.get("items") or []
    if not items:
        return None
    fps = state.get("fp") or {}
    cheap = state.get("cheap") or {}
    cursor = int(state.get("cursor") or 0) % len(items)
    start = (cursor + 1) % len(items)
    for offset in range(len(items)):
        nxt = items[(start + offset) % len(items)]
        if not isinstance(nxt, dict):
            continue
        wid = str(nxt.get("id") or "")
        if not wid:
            continue
        sig = cheap_sig(home, nxt)
        known_fp = str(fps.get(wid) or "")
        known_sig = str(cheap.get(wid) or "")
        if known_fp and wid not in _DIRTY and (known_sig == sig or not known_sig):
            continue
        return nxt
    return None


def warm_next(home: Path) -> dict | None:
    """Cache hits for the next card that will be claimed."""
    home = home_of(home)
    nxt = upcoming(home)
    if not nxt:
        return None
    wid = str(nxt.get("id") or "")
    if not wid:
        return None
    _hits_for(home, wid, nxt, cheap_sig(home, nxt))
    return nxt


def refresh_one(home: Path) -> str:
    """Re-read one card while a model call is running. Returns the id or ''."""
    home = home_of(home)
    state = _load(state_path(home))
    items = state.get("items") or []
    if not items:
        return ""
    slot = int(state.get("served") or 0) % len(items)
    nxt = items[slot]
    if not isinstance(nxt, dict):
        return ""
    wid = str(nxt.get("id") or "")
    if not wid:
        return ""
    sig = cheap_sig(home, nxt)
    try:
        con = _db(home)
        try:
            hits = collect_hits(con, wid, nxt.get("body") or "")
        finally:
            con.close()
    except Exception:
        return ""
    _HIT_CACHE[wid] = {"at": time.time(), "hits": hits, "cheap": sig}
    new_fp = fingerprint_item(home, nxt, hits)
    old_fp = str((state.get("fp") or {}).get(wid) or "")
    if old_fp and new_fp != old_fp:
        _DIRTY.add(wid)
        return wid
    return ""


def fingerprint_item(home: Path, item: dict, hits: list) -> str:
    parts = {
        "id": item["id"],
        "state": item.get("state") or "",
        "changed": item.get("changed") or "",
        "title": item.get("title") or "",
        "comment": _comment_fp(home, item["id"]),
        "hits": sorted((h.get("source"), h.get("ref")) for h in hits),
    }
    return fingerprint(parts)


def claim(home: Path) -> dict | None:
    """Return one changed open item, or None when the lap is unchanged."""
    home = home_of(home)
    path = state_path(home)
    state = _load(path)
    now = time.time()
    items = state.get("items") if isinstance(state.get("items"), list) else None
    if not items or now - float(state.get("built") or 0) > 900:
        try:
            items = load_open_items(home)
        except Exception:
            items = items or []
        state["items"] = [
            {"id": it["id"], "title": it["title"], "state": it["state"], "changed": it["changed"], "body": it["body"][:1500]}
            for it in items
        ]
        state["built"] = now
        state["n"] = len(state["items"])
    order = [it["id"] for it in state.get("items") or []]
    by_id = {it["id"]: it for it in state.get("items") or []}

    cheap_updates = {}

    def fp_of(wid: str) -> str:
        item = by_id.get(wid) or {"id": wid, "body": "", "state": "", "changed": "", "title": ""}
        sig = cheap_sig(home, item)
        known_fp = (state.get("fp") or {}).get(wid)
        known_sig = (state.get("cheap") or {}).get(wid)
        dirty = wid in _DIRTY
        if known_fp and not dirty and (known_sig == sig or not known_sig):
            if not known_sig:
                cheap_updates[wid] = sig
            cached = _HIT_CACHE.get(wid)
            item["_hits"] = list(cached.get("hits") or []) if cached and cached.get("cheap") == sig else []
            return known_fp
        hits = _hits_for(home, wid, item, sig)
        item["_hits"] = hits
        cheap_updates[wid] = sig
        _DIRTY.discard(wid)
        return fingerprint_item(home, item, hits)

    # A card already seen this lap is a stat check. The hit search runs once.
    result = step_cursor(state, order, fp_of)
    state = result["state"]
    cheap = dict(state.get("cheap") or {})
    cheap.update(cheap_updates)
    # Keep the cheap stamps across a lap. A full hit search here sits in
    # front of the next model call. A changed stamp still searches, and the
    # prefetch thread can mark one card dirty while a call is running.
    state["cheap"] = cheap
    held_hits = []
    if result.get("action") == "work":
        held_hits = list((by_id.get(result["id"]) or {}).get("_hits") or [])
    for row in state.get("items") or []:
        if isinstance(row, dict):
            row.pop("_hits", None)
    _save(path, state)
    if result["action"] == "work":
        item = by_id.get(result["id"]) or {"id": result["id"], "body": "", "title": ""}
        hits = held_hits
        _append(log_path(home), {
            "ts": int(now), "action": "work", "id": result["id"],
            "skips": result["skips"], "found": len(hits), "lap": int(state.get("laps") or 0),
        })
        seed = "{0}\nState: {1}\n{2}\n\nRELATED\n{3}".format(
            item.get("title") or "",
            item.get("state") or "",
            (item.get("body") or "")[:900],
            "\n".join("- {0} {1}: {2}".format(h["source"], h["ref"], h["note"]) for h in hits) or "- none",
        )
        return {
            "kind": "nextstep",
            "source": "ado",
            "ref": "wi_" + str(result["id"]),
            "rev": str(item.get("changed") or result["fp"]),
            "label": "ado-" + str(result["id"]),
            "hash": result["fp"][:16],
            "fp": result["fp"],
            "text": seed[:1100],
            "seed": seed[:3500],
            "loop_id": str(result["id"]),
            "loop_hits": hits,
            "loop_revision": str(item.get("changed") or ""),
            "loop_body": item.get("body") or "",
        }
    if result["action"] == "lap":
        _append(log_path(home), {
            "ts": int(now), "action": "lap", "skips": result["skips"],
            "lap": int(state.get("laps") or 0), "n": len(order),
        })
    elif result["skips"]:
        _append(log_path(home), {"ts": int(now), "action": "skip", "skips": result["skips"]})
    return None


def saved(home: Path, item: dict, note: str, accepted: bool) -> dict:
    """Store the fingerprint and the next-step packet. Mail only when new."""
    home = home_of(home)
    wid = str(item.get("loop_id") or "")
    if not wid and str(item.get("label") or "").startswith("ado-"):
        wid = str(item.get("label"))[4:]
    fp = str(item.get("fp") or "")
    path = state_path(home)
    state = _load(path)
    if wid and fp:
        state = remember_fp(state, wid, fp)
        state["n"] = int(state.get("n") or len(state.get("items") or []) or 1)
        _save(path, state)
    if not wid:
        return {"ok": False, "error": "no_id"}
    hits = item.get("loop_hits") or []
    body = str(item.get("loop_body") or item.get("seed") or "")
    artifact = build_artifact(wid, body, hits, str(item.get("loop_revision") or ""), fp, note if accepted else "")
    dest = artifact_path(home, wid)
    prev = _load(dest)
    choice = mail_choice(prev if prev else None, artifact, _sent_today(home))
    artifact["mail"] = choice
    if choice == "email":
        artifact["emailed_rev"] = artifact["revision"]
    elif prev.get("emailed_rev"):
        artifact["emailed_rev"] = prev.get("emailed_rev")
    _save(dest, artifact)
    _append(log_path(home), {
        "ts": int(time.time()), "action": "saved", "id": wid,
        "mail": choice, "found": len(artifact["found"]), "accepted": bool(accepted),
        "blocking": artifact["blocking"][:120],
    })
    if choice == "email":
        _send_progress(home, artifact)
    return {"ok": True, "mail": choice, "id": wid}


def _sent_today(home: Path) -> int:
    day = time.strftime("%Y%m%d")
    path = home / "claude-stack/data/idle_filler" / "open-loop-mail-{0}.count".format(day)
    try:
        return int(path.read_text().strip() or "0")
    except (OSError, ValueError):
        return 0


def _bump_today(home: Path) -> None:
    day = time.strftime("%Y%m%d")
    path = home / "claude-stack/data/idle_filler" / "open-loop-mail-{0}.count".format(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(_sent_today(home) + 1))


def _send_progress(home: Path, artifact: dict) -> None:
    """One self-note to the operator. No tracker write."""
    try:
        import sys
        cli = str(home / "llm-appliance/cli")
        if cli not in sys.path:
            sys.path.insert(0, cli)
        import ops_work_digest as owd
    except Exception:
        return
    wid = artifact["id"]
    lines = [
        "<p>Work item {0}</p>".format(wid),
        "<p><b>Blocking.</b> {0}</p>".format(_esc(artifact.get("blocking") or "")),
        "<p><b>Found.</b></p><ul>",
    ]
    for hit in artifact.get("found") or []:
        lines.append("<li>{0} {1}: {2}</li>".format(
            _esc(hit.get("source") or ""), _esc(hit.get("ref") or ""), _esc(hit.get("note") or "")
        ))
    if not artifact.get("found"):
        lines.append("<li>No new related note.</li>")
    lines.append("</ul>")
    lines.append("<p><b>Next.</b> {0}</p>".format(_esc(artifact.get("next_action") or "")))
    prep = artifact.get("prepared") or {}
    if prep.get("text"):
        lines.append("<p><b>Prepared ({0}).</b> Not applied. {1}</p>".format(
            _esc(prep.get("kind") or "note"), _esc(prep.get("text") or "")
        ))
    html_doc = "<html><body style='font-family:Segoe UI,sans-serif;max-width:680px'>{0}</body></html>".format(
        "".join(lines)
    )
    if re.search(r"(?i)\b(ai|llm|qwen|grok|claude|gpt|bot)\b", html_doc):
        return
    folder = home / "claude-stack/data/idle_filler/next_steps"
    folder.mkdir(parents=True, exist_ok=True)
    html_path = folder / "mail-{0}.html".format(wid)
    html_path.write_text(html_doc)
    subject = "{0} next step".format(wid)
    try:
        result = owd.send_self_alert(subject=subject, html_path=html_path)
    except Exception:
        result = {"ok": False}
    if result.get("ok"):
        _bump_today(home)


def _esc(text: str) -> str:
    return (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def self_check() -> None:
    rows = [
        {"id": "1", "title": "Pump form", "body": "State: Active\nAssigned to: operator, operator\nChanged: 2026-11-01"},
        {"id": "2", "title": "Closed one", "body": "State: Closed\nAssigned to: operator, operator"},
        {"id": "3", "title": "Other", "body": "State: New\nAssigned to: Someone Else"},
        {"id": "5", "title": "Mentioned", "body": "State: New\nAssigned to: Someone Else\nCreated by: operator, operator\nPlease loop in operator, operator on the dealer file."},
        {"id": "4", "title": "Secret", "body": "State: New\nAssigned to: operator, operator\npassword: hunter2"},
    ]
    picked = select_open(rows)
    assert [r["id"] for r in picked] == ["1", "5"]
    assert hard_blocked("wire the password reset", "")
    assert hard_blocked("notes", "Legal/contract.pdf")
    state = {"cursor": 0, "fp": {"1": "aaa"}}
    calls = {"n": 0}

    def fp_of(wid):
        calls["n"] += 1
        return {"1": "aaa", "2": "bbb"}[wid]

    lap = step_cursor(state, ["1", "2"], fp_of)
    assert lap["action"] == "work" and lap["id"] == "2" and lap["skips"] == 1
    done = step_cursor({"cursor": 0, "fp": {"1": "aaa", "2": "bbb"}}, ["1", "2"], fp_of)
    assert done["action"] == "lap" and done["skips"] == 2
    first = build_artifact("9", "State: New\nAssigned to: operator, operator", [], "r1", "fp1")
    assert mail_choice(None, first, 0) == "baseline"
    again = dict(first)
    assert mail_choice(first, again, 0) == "update"
    richer = dict(first)
    richer["found"] = [{"source": "work_brain", "ref": "doc-1", "note": "a runbook line"}]
    richer["revision"] = "r2"
    assert mail_choice(first, richer, 0) == "email"
    assert mail_choice(first, richer, 10) == "cap"
    remembered = {"emailed_rev": "r2"}
    richer2 = dict(richer)
    assert mail_choice(remembered, richer2, 0) == "update"
    assert "needs_go" in first["prepared"] and first["prepared"]["needs_go"] is True


if __name__ == "__main__":
    self_check()
    print("open loop check ok")
