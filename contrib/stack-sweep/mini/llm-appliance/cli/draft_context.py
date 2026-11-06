"""Shared read-only context, edit lessons, and one self-check for drafts.

Nothing here sends mail, posts a comment, or writes operator.
One model call at a time. The next item's pages are looked up, not generated.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

_ALLOWED_REPOS = ("operator/operator-operator",)
_LINK_LOG = "draft_context_links.jsonl"
_LESSONS = "draft_edits.jsonl"
_GOLD = "draft_gold.jsonl"
_CHECK_LOG = "draft_selfcheck.jsonl"
_JOIN_LOG = "draft_selfcheck_join.json"
_PACK_DIR = "context_packs"
_CACHE_S = 20 * 60
_AI = re.compile(
    r"\b(openai|chatgpt|claude|anthropic|copilot|minicpm|qwen|llm|work_brain|automation|model|bot)\b",
    re.I,
)
_SECRET = re.compile(
    r"(?i)(?:password\s*(?:is|:|=)\s*\S|seed phrase|private key|\b(?:2fa|otp|passcode)\b)"
)
_HARD_PATH = re.compile(r"(?i)(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)")
_PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
_VOLATILE = re.compile(
    r"(?i)\b\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2})?(?:z|[+-]\d{2}:?\d{2})?)?\b"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b"
    r"|\b\d{10,13}\b"
)
_WI = re.compile(r"\b(?:AB#|ADO\s*#|wi_|work\s*item\s*#?)(\d{5,7})\b", re.I)
_SF = re.compile(r"\bSF-(\d{3,8})\b", re.I)
_PARENT = re.compile(r"(?m)^Parent:\s*(\d{5,7})\s*$")
_STOP = {
    "the", "and", "for", "with", "this", "that", "from", "have", "can", "you",
    "operator", "please", "thanks", "about", "would", "could", "should", "ticket",
}


def home_dir() -> Path:
    raw = os.environ.get("DRAFT_CONTEXT_HOME") or ""
    return Path(raw) if raw else Path.home()


def learning_dir(home: Path | None = None) -> Path:
    return (home or home_dir()) / "claude-stack" / "data" / "learning"


def _db(home: Path):
    import sqlite3
    path = home / "claude-stack/data/work_context.db"
    con = sqlite3.connect("file:{0}?mode=ro".format(path), uri=True, timeout=8)
    con.execute("PRAGMA query_only=ON")
    return con


def blocked(text: str) -> str:
    """Empty means the text is safe to read. A reason means hold."""
    blob = text or ""
    if _HARD_PATH.search(blob):
        return "data_line"
    if _SECRET.search(blob):
        return "data_line"
    try:
        import ask_do
        return ask_do.data_line(blob) or ""
    except Exception:
        return ""


_MAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_COMMON = {
    "earlier", "already", "still", "while", "about", "because", "before", "after",
    "there", "their", "would", "could", "should", "please", "thanks", "looking",
    "checking", "added", "using", "where", "which", "these", "those", "other",
    "stays", "stayed", "allow", "allowed", "without", "inside", "around", "today",
    "tomorrow", "yesterday", "really", "going", "something", "nothing", "anyone",
    "people", "think", "right", "again", "being", "been", "have", "with", "this",
    "that", "from", "your", "just", "into", "them", "they", "will", "make", "made",
    "does", "didnt", "dont", "cant", "wont", "wasnt", "isnt", "didn",
}


def _clip(text: str, limit: int) -> str:
    text = _PHONE.sub("[redacted]", text or "")
    text = _MAIL.sub("[redacted]", text)
    text = re.sub(r"[ \t]+", " ", text.replace("\r", ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _safe_ref(ref: str) -> str:
    ref = str(ref or "")
    if "@" in ref or _PHONE.search(ref) or _SECRET.search(ref):
        return "id-" + hashlib.sha1(ref.encode("utf-8", "replace")).hexdigest()[:12]
    return ref[:80]


def _window(text: str, needles: list, limit: int) -> str:
    raw = text or ""
    low = raw.lower()
    spots = [low.find(n.lower()) for n in needles if n and low.find(n.lower()) >= 0]
    start = max(0, (min(spots) if spots else 0) - 80)
    return raw[start:start + limit]


def _mask_log(text: str) -> str:
    text = _clip(text, 240)
    if blocked(text):
        return ""
    return text


def _append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _ids(text: str, ticket_id: str = "") -> tuple[list, list]:
    wis = []
    if str(ticket_id or "").isdigit():
        wis.append(str(ticket_id))
    for match in _WI.finditer(text or ""):
        num = match.group(1)
        if num not in wis:
            wis.append(num)
    sfs = []
    for match in _SF.finditer(text or ""):
        num = match.group(1)
        if num not in sfs:
            sfs.append(num)
    return wis[:3], sfs[:3]


def _rows(con, expr: str, source: str, limit: int) -> list:
    try:
        return con.execute(
            "select ref, ifnull(url,''), ifnull(title,''), substr(ifnull(body,''),1,1600) "
            "from docs where docs match ? and source=? limit ?",
            (expr, source, int(limit)),
        ).fetchall()
    except Exception:
        return []


def _one_ref(con, source: str, ref: str):
    try:
        row = con.execute(
            "select ref, ifnull(url,''), ifnull(title,''), substr(ifnull(body,''),1,1800) "
            "from docs where source=? and ref=? limit 1",
            (source, ref),
        ).fetchone()
    except Exception:
        return None
    return row


def _repo_ok(url: str, body: str) -> bool:
    raw = os.environ.get("DRAFT_CONTEXT_REPOS") or ""
    allowed = [p.strip() for p in raw.split(",") if p.strip()] or list(_ALLOWED_REPOS)
    blob = "{0}\n{1}".format(url or "", (body or "")[:400]).lower()
    return any(repo.lower() in blob for repo in allowed)


_LINK_CAP = 12


def _add(links: list, seen: set, kind: str, ref: str, body: str, limit: int) -> None:
    ref = _safe_ref(ref)
    key = kind + ":" + ref
    if not ref or key in seen or len(links) >= _LINK_CAP:
        return
    text = _clip(body, limit)
    if len(text) < 30 or blocked(text):
        return
    seen.add(key)
    links.append({"kind": kind, "ref": ref, "text": text})


def _runbook_hits(home: Path, query: str, limit: int = 2) -> list:
    words = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", query or ""):
        low = word.lower()
        if low in _STOP or low in words:
            continue
        words.append(low)
        if len(words) >= 4:
            break
    if len(words) < 2:
        return []
    roots = [
        home / "claude-stack/docs",
        home / "claude-stack/data/playbooks",
        home / "claude-stack/data/handoffs",
        home / "llm-appliance/docs",
    ]
    hits = []
    for root in roots:
        if not root.exists():
            continue
        files = [root] if root.is_file() else list(root.rglob("*.md"))
        for path in files:
            if len(hits) >= limit:
                return hits
            rel = str(path)
            if _HARD_PATH.search(rel) or ".bak" in path.name:
                continue
            name = path.name.lower()
            if "runbook" not in name and "playbook" not in name and not name.startswith("readme"):
                continue
            try:
                if path.stat().st_size > 200_000:
                    continue
                head = path.read_text(encoding="utf-8", errors="replace")[:1500]
            except OSError:
                continue
            low = (path.name + "\n" + head[:400]).lower()
            if words[0] in low and words[1] in low:
                hits.append((path.name, head))
    return hits


def _cache_path(home: Path, key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:20]
    return learning_dir(home) / _PACK_DIR / (digest + ".json")


def _read_cache(home: Path, key: str, fp: str) -> dict | None:
    path = _cache_path(home, key)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    age = time.time() - float(doc.get("ts") or 0)
    if age > _CACHE_S:
        return None
    if fp and doc.get("fp") and doc.get("fp") != fp:
        return None
    if not doc.get("links") and not doc.get("text"):
        return None
    return doc


def _write_cache(home: Path, key: str, pack: dict) -> None:
    path = _cache_path(home, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "ts": time.time(),
        "key": key[:80],
        "fp": pack.get("fp") or "",
        "text": pack.get("text") or "",
        "links": [{"kind": x.get("kind"), "ref": x.get("ref")} for x in pack.get("links") or []],
        "link_text": [x.get("text") or "" for x in pack.get("links") or []],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    tmp.replace(path)


def build_pack(
    *,
    channel: str = "",
    subject: str = "",
    body: str = "",
    ticket_id: str = "",
    thread=None,
    description: str = "",
    surface: str = "",
    home: Path | None = None,
    use_cache: bool = True,
) -> dict:
    """Read ticket history, comments, pull requests, records, parent, runbooks, and notes."""
    home = home or home_dir()
    blob = "\n".join([subject or "", body or "", description or ""])
    if isinstance(thread, (list, tuple)):
        blob += "\n" + "\n".join(str(x) for x in thread[:8])
    if blocked(blob):
        return {"ok": False, "blocked": "data_line", "text": "", "links": [], "kinds": [], "fp": ""}
    wis, sfs = _ids(blob, ticket_id)
    key = "{0}:{1}".format(surface or channel or "draft", wis[0] if wis else (sfs[0] if sfs else hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]))
    fp = stable_fp("thread2\n" + blob[:2000])
    if use_cache:
        cached = _read_cache(home, key, fp)
        if cached:
            links = []
            texts = cached.get("link_text") or []
            for index, row in enumerate(cached.get("links") or []):
                links.append({
                    "kind": row.get("kind"),
                    "ref": row.get("ref"),
                    "text": texts[index] if index < len(texts) else "",
                })
            text = cached.get("text") or _render(links)
            pack = {
                "ok": True, "blocked": "", "text": text, "links": links,
                "kinds": _kinds(links), "fp": cached.get("fp") or fp, "key": key, "cached": True,
            }
            pack["lessons"] = similar_block(blob, home=home)
            return pack
    links: list = []
    seen: set = set()
    latest = _latest_message(body)
    if latest and not blocked(latest):
        _add(links, seen, "thread", "latest", _scrub_numbers(latest), 1200)
    earlier = (body or "").strip()
    if latest and earlier.startswith(latest):
        earlier = earlier[len(latest):].strip()
    if earlier and len(earlier) > 40 and not blocked(earlier):
        _add(links, seen, "thread", "earlier", _scrub_numbers(earlier), 900)
    if description and not blocked(description):
        _add(links, seen, "comments", "description", description, 900)
    if thread:
        lines = []
        for row in list(thread)[-6:]:
            lines.append(_clip(str(row), 300))
        _add(links, seen, "comments", "thread", "\n".join(lines), 1200)
    try:
        con = _db(home)
    except Exception:
        con = None
    try:
        if con is not None:
            for wid in wis:
                row = _one_ref(con, "ado", "wi_" + wid)
                if row:
                    _ref, _url, title, body_text = row
                    _add(links, seen, "history", "wi_" + wid, "{0}\n{1}".format(title, body_text), 1200)
                    _extra_w, extra_s = _ids(body_text or "", "")
                    for num in extra_s:
                        if num not in sfs and len(sfs) < 3:
                            sfs.append(num)
                    parent = _PARENT.search(body_text or "")
                    if parent and parent.group(1) != wid:
                        prow = _one_ref(con, "ado", "wi_" + parent.group(1))
                        if prow:
                            _add(links, seen, "parent", "wi_" + parent.group(1), "{0}\n{1}".format(prow[2], prow[3][:500]), 500)
                note = _comment_cache(home, wid)
                if note:
                    _add(links, seen, "comments", "note-" + wid, note, 900)
                prs = _rows(con, "NEAR(AB {0}, 4)".format(wid), "github_pr", 8)
                kept = 0
                for ref, url, title, body_text in prs:
                    pr_blob = "{0}\n{1}\n{2}".format(url or "", title or "", body_text or "")
                    if "AB#{0}".format(wid) not in pr_blob and "/{0}".format(wid) not in (url or ""):
                        continue
                    if not _repo_ok(url, body_text):
                        continue
                    _add(links, seen, "pr", str(ref), "{0}\n{1}\n{2}".format(url, title, body_text[:400]), 600)
                    kept += 1
                    if kept >= 2:
                        break
            for num in sfs:
                rows = _rows(con, '"SF-{0}"'.format(num), "operator", 4)
                for ref, _url, title, body_text in rows:
                    if str(ref).upper() == "SF-" + num or "SF-{0}".format(num) in (title or ""):
                        _add(links, seen, "operator", str(ref), "{0}\n{1}".format(title, body_text[:500]), 600)
                        break
        for name, head in _runbook_hits(home, blob):
            kind = "playbook" if "playbook" in name.lower() else "runbook"
            _add(links, seen, kind, name, head, 500)
        if con is not None:
            brain_kept = 0
            ranked = []
            for wid in wis:
                for source in ("email", "work_vault", "operator", "sp_corpus", "zoom", "ado", "github_pr"):
                    for ref, url, title, body_text in _rows(con, wid, source, 6):
                        whole = "{0}\n{1}\n{2}\n{3}".format(ref, url, title, body_text)
                        if wid not in whole:
                            continue
                        if source == "ado" and str(ref) == "wi_" + wid:
                            continue
                        if _HARD_PATH.search(url or "") or _HARD_PATH.search(str(ref)) or blocked(whole):
                            continue
                        score = sum(1 for n in ("mapping", "contact", "sharing", "read only", wid) if n in whole.lower())
                        ranked.append((score, source, ref, url, title, body_text, wid))
            ranked.sort(key=lambda item: -item[0])
            used_refs = set()
            for score, source, ref, url, title, body_text, wid in ranked:
                if brain_kept >= 2:
                    break
                mark = source + ":" + str(ref)
                if mark in used_refs:
                    continue
                used_refs.add(mark)
                raw = body_text or ""
                if len(raw) <= 800:
                    snippet = raw
                else:
                    snippet = _window(raw, ["mapping", "sharing", "contact", "account", "read only", wid], 700)
                before = len(links)
                label = source
                if source in {"email", "work_vault"} or "@" in str(ref):
                    label = source + ":" + hashlib.sha1(str(ref).encode("utf-8", "replace")).hexdigest()[:12]
                else:
                    label = source + ":" + str(ref)[:40]
                _add(links, seen, "work_brain", label, snippet, 800)
                if len(links) > before:
                    brain_kept += 1
            words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{4,}", blob) if w.lower() not in _STOP][:3]
            if brain_kept < 2 and len(words) >= 2:
                expr = " ".join('"{0}"'.format(w) for w in words[:2])
                for source in ("sp_corpus", "playbooks", "zoom"):
                    if brain_kept >= 2:
                        break
                    for ref, url, title, body_text in _rows(con, expr, source, 3):
                        if _HARD_PATH.search(url or "") or _HARD_PATH.search(str(ref)) or blocked(body_text or ""):
                            continue
                        _add(links, seen, "work_brain", source + ":" + str(ref)[:40], "{0}\n{1}\n{2}".format(source, title, _window(body_text or "", words, 400)), 400)
                        brain_kept += 1
                        if brain_kept >= 2:
                            break
    finally:
        if con is not None:
            con.close()
    text = _render(links)
    pack = {
        "ok": True,
        "blocked": "",
        "text": text,
        "links": links,
        "kinds": _kinds(links),
        "fp": fp,
        "key": key,
        "cached": False,
        "channel": channel,
        "surface": surface or channel,
    }
    pack["lessons"] = similar_block(blob, home=home)
    try:
        _write_cache(home, key, pack)
    except OSError:
        pass
    return pack


def _comment_cache(home: Path, wid: str) -> str:
    path = home / "llm-appliance/runtime/work-loop/ado-comment-cache" / "{0}.json".format(wid)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(doc, dict):
        return ""
    return _clip(str(doc.get("text") or ""), 800)


def _kinds(links: list) -> list:
    out = []
    for row in links:
        kind = str(row.get("kind") or "")
        if kind and kind not in out:
            out.append(kind)
    return out


def _render(links: list) -> str:
    chunks = []
    used = 0
    for row in links:
        piece = "LINK {0} {1}\n{2}".format(row.get("kind"), row.get("ref"), row.get("text") or "")
        if used + len(piece) > 3600:
            break
        chunks.append(piece.strip())
        used += len(piece)
    return "\n\n".join(chunks)


def log_links(surface: str, key: str, links: list, home: Path | None = None) -> None:
    home = home or home_dir()
    safe_key = str(key or "")
    if "@" in safe_key or _PHONE.search(safe_key):
        safe_key = hashlib.sha1(safe_key.encode("utf-8", "replace")).hexdigest()[:12]
    _append(learning_dir(home) / _LINK_LOG, {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "surface": str(surface or "")[:24],
        "key": safe_key[:80],
        "n": len(links or []),
        "kinds": _kinds(links or []),
        "refs": ["{0}:{1}".format(x.get("kind"), x.get("ref")) for x in (links or [])][:_LINK_CAP],
    })


_STASH = {"pack": None}


def stash(pack: dict | None) -> None:
    _STASH["pack"] = pack


def current() -> dict | None:
    pack = _STASH.get("pack")
    return pack if isinstance(pack, dict) else None


def prompt_block(pack: dict | None) -> str:
    if not pack or pack.get("blocked") or not (pack.get("text") or pack.get("lessons")):
        return ""
    parts = []
    if pack.get("text"):
        parts.append("CONTEXT\nUse only these facts and the message. Do not invent a count, a path, or a status.\n" + str(pack["text"])[:3400])
    if pack.get("lessons"):
        parts.append("PRIOR EDITS\nVoice and shape only. These are not facts of this thread.\n" + str(pack["lessons"])[:900])
    return "\n\n".join(parts)


def stable_fp(text: str) -> str:
    cleaned = _VOLATILE.sub(" ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return hashlib.sha1(cleaned.encode("utf-8", "replace")).hexdigest()[:16]


def _canon(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _canon(a).split(), _canon(b).split()).ratio()


def _diff_lesson(staged: str, sent: str) -> str:
    left = [ln.strip() for ln in (staged or "").splitlines() if ln.strip()] or [_clip(staged, 180)]
    right = [ln.strip() for ln in (sent or "").splitlines() if ln.strip()] or [_clip(sent, 180)]
    bits = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete") and left[i1:i2]:
            bits.append("drop: " + _clip(" ".join(left[i1:i2]), 160))
        if tag in ("replace", "insert") and right[j1:j2]:
            bits.append("keep: " + _clip(" ".join(right[j1:j2]), 160))
        if len(bits) >= 4:
            break
    return " | ".join(bits)[:480]


def _seen_ids(path: Path) -> set:
    found = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-8000:]
    except OSError:
        return found
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("source_event_id"):
            found.add(str(row.get("source_event_id")))
        if isinstance(row, dict) and row.get("dedupe"):
            found.add(str(row.get("dedupe")))
    return found


def store_outcome(surface: str, key: str, staged: str, sent: str, home: Path | None = None) -> str:
    """Append a diff lesson, or a gold example when the sent text matches the draft."""
    home = home or home_dir()
    staged = _clip(staged or "", 1200)
    sent = _clip(sent or "", 1200)
    if len(staged) < 12 or len(sent) < 12:
        return "skip"
    if blocked(staged) or blocked(sent):
        return "skip"
    same = _ratio(staged, sent) >= 0.97
    dedupe = hashlib.sha1(_canon(surface + "|" + staged + "|" + sent).encode()).hexdigest()[:20]
    folder = learning_dir(home)
    if same:
        path = folder / _GOLD
        if dedupe in _seen_ids(path) or ("gold:" + str(key)) in _seen_ids(path):
            return "dup"
        _append(path, {
            "kind": "gold",
            "surface": surface,
            "source_event_id": "gold:" + str(key)[:64],
            "dedupe": dedupe,
            "example": sent[:500],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        return "gold"
    path = folder / _LESSONS
    lesson = _diff_lesson(staged, sent)
    if not lesson:
        return "skip"
    event_id = "lesson:" + str(key)[:64]
    if dedupe in _seen_ids(path) or event_id in _seen_ids(path):
        return "dup"
    _append(path, {
        "kind": "lesson",
        "lane": "work",
        "surface": surface,
        "source_event_id": event_id,
        "dedupe": dedupe,
        "diff": lesson,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return "lesson"


def _load_rows(path: Path, limit: int = 600) -> list:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _tokens(text: str) -> set:
    return {w.lower() for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower())}


def similar_block(query: str, k: int = 3, home: Path | None = None) -> str:
    home = home or home_dir()
    q = _tokens(query)
    if not q:
        return ""
    scored = []
    seen = set()
    folder = learning_dir(home)
    for row in _load_rows(folder / _LESSONS, 500):
        text = str(row.get("diff") or "")
        if not text:
            # Older rows keep draft and final. Use a short diff, not the whole mail.
            draft = str(row.get("draft") or "")
            final = str(row.get("final") or "")
            if draft and final and not blocked(draft) and not blocked(final):
                text = _diff_lesson(draft, final)
        if not text or blocked(text):
            continue
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
        if digest in seen:
            continue
        seen.add(digest)
        score = len(q & _tokens(text))
        if score:
            scored.append((score, "lesson", text))
    for row in _load_rows(folder / _GOLD, 300):
        text = str(row.get("example") or "")
        if len(text) < 20 or blocked(text):
            continue
        digest = hashlib.sha1(_canon(text).encode()).hexdigest()[:16]
        if digest in seen:
            continue
        seen.add(digest)
        score = len(q & _tokens(text))
        if score:
            scored.append((score + 0.2, "example", text))
    scored.sort(key=lambda item: -item[0])
    lessons = [item for item in scored if item[1] == "lesson"]
    examples = [item for item in scored if item[1] == "example"]
    picked = []
    if lessons:
        picked.append(lessons[0])
    for item in examples:
        if len(picked) >= k:
            break
        picked.append(item)
    for item in lessons[1:]:
        if len(picked) >= k:
            break
        picked.append(item)
    lines = []
    for _score, kind, text in picked[:k]:
        lines.append("{0}: {1}".format(kind, _clip(text, 220)))
    return "\n".join(lines[:k])


def harvest(home: Path | None = None, limit: int = 80) -> dict:
    """Compare staged drafts already on disk with what was sent. Read only."""
    home = home or home_dir()
    counts = {"gold": 0, "lesson": 0, "dup": 0, "skip": 0}
    events = home / "claude-stack/data/learning/draft_feedback_events.jsonl"
    taken = 0
    if events.is_file():
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            if taken >= limit:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            outcome = str(row.get("outcome") or "")
            if outcome not in {"sent_edited", "sent_unchanged"}:
                continue
            taken += 1
            status = store_outcome(
                str(row.get("surface") or "mail"),
                str(row.get("event_id") or ""),
                str(row.get("draft_text") or ""),
                str(row.get("final_text") or ""),
                home=home,
            )
            counts[status] = counts.get(status, 0) + 1
    staged = home / "llm-appliance/runtime/work-loop/ado-staged"
    if staged.is_dir():
        for path in sorted(staged.glob("*.json")):
            if taken >= limit:
                break
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(doc.get("status") or "") not in {"applied", "posted"}:
                continue
            result = doc.get("result") if isinstance(doc.get("result"), dict) else {}
            sent = str(result.get("comment") or "")
            draft = str(doc.get("comment") or "")
            if not sent or not draft:
                continue
            taken += 1
            status = store_outcome("ado", path.stem, draft, sent, home=home)
            counts[status] = counts.get(status, 0) + 1
    counts["seen"] = taken
    return counts


_GRADE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "grade",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "extracted_ask": {"type": "string"},
                "answers_ask": {"type": "boolean"},
                "pass": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["extracted_ask", "answers_ask", "pass", "reason"],
        },
    },
}

_ASK_HEADER = re.compile(
    r"(?im)^(state|area|iteration|created by|last changed by|tags|priority|parent|"
    r"changed|assigned to|story points|v2o username)\s*:"
)
_ASK_STOP = {
    "this", "that", "with", "from", "have", "been", "were", "they", "them", "your",
    "about", "into", "over", "under", "after", "before", "when", "what", "which",
    "where", "would", "could", "should", "please", "thanks", "there", "their",
    "these", "those", "task", "user", "story", "parent", "tags", "updated",
    "include", "state", "changed", "priority", "description", "outcome", "scope",
    "done", "each", "will", "just", "only", "also", "than", "then", "does", "did",
    "for", "and", "the", "not", "she", "was", "trying", "information", "active",
    "needs", "need", "item", "work", "comment", "reply",
}


def local_complete(messages: list, max_tokens: int = 180, json_mode: bool = False) -> dict:
    """One local completion. Draft priority. Chat still cuts in front."""
    if os.environ.get("DRAFT_CONTEXT_NO_MODEL") == "1":
        return {"ok": False, "text": "", "reason": "no_model"}
    payload = {
        "model": os.environ.get("LOCAL_LLM_MODEL") or "MiniCPM-V-4.7-35B-A3B",
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": 0.1,
        "stream": False,
        "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if json_mode:
        payload["response_format"] = _GRADE_FORMAT
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        os.environ.get("LOCAL_LLM_URL") or "http://127.0.0.1:8080/v1/chat/completions",
        data=raw,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=150) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        code = getattr(exc, "code", 0) or 0
        if json_mode and code in {400, 422, 500}:
            return local_complete(messages, max_tokens, json_mode=False)
        reason = "preempted" if "chat_priority" in str(exc).lower() or code == 503 else type(exc).__name__
        return {"ok": False, "text": "", "reason": reason}
    text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    text = re.sub(r"(?is)<think>.*?</think>", "", text).strip()
    return {"ok": True, "text": text, "reason": ""}


_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d")
_QUOTE_START = re.compile(
    r"(?m)^\s*(?:From:\s|-----Original Message-----|_{5,}|On .+ wrote:\s*$)"
)
_PROMISE = re.compile(
    r"(?i)\b(i'll|i will|let me|follow up|circle back|tomorrow|next week)\b"
)


def _latest_message(text: str) -> str:
    """The newest note only. Quoted history is not the ask."""
    raw = (text or "").replace("\r", "")
    parts = re.split(r"\n(?:---+|LATEST(?: MESSAGE)?)\n", raw, flags=re.I)
    blob = parts[-1].strip()
    chunks = _QUOTE_START.split(blob, maxsplit=1)
    latest = (chunks[0] if chunks else blob).strip()
    return latest or blob


def _scrub_numbers(text: str) -> str:
    return _PHONE.sub("", text or "")


def _latest_is_ack(text: str) -> bool:
    """True only when the newest note is agreeing, not asking.

    A missing question mark is not enough. A problem statement stays an ask.
    """
    latest = _latest_message(text)
    if "?" in latest:
        return False
    if re.search(
        r"(?i)\b(please|can you|could you|need you to|let me know|what |why |how |when )\b",
        latest,
    ):
        return False
    if re.search(r"(?i)\b(error|unable|cannot|can't|issue|failed|bug)\b", latest):
        return False
    return bool(re.search(
        r"(?i)\b(sounds good|thanks|thank you|got it|noted|will do|okay|\bok\b)\b",
        latest,
    ))


def extract_ask(text: str) -> str:
    """The specific question or request in the latest message."""
    blob = _latest_message(text)
    described = re.search(r"(?is)\bDescription:\s*(.*)$", blob)
    if described and len(described.group(1).strip()) >= 20:
        blob = described.group(1).strip()
    kept = []
    for line in blob.splitlines():
        if _ASK_HEADER.match(line.strip()):
            continue
        kept.append(line)
    body = "\n".join(kept).strip()
    questions = re.findall(r"[^\n?]{8,}\?", body)
    if questions:
        return _clip(" ".join(q.strip() for q in questions[-2:]), 500)
    return _clip(body, 500)


def _ask_words(text: str) -> list:
    out = []
    seen = set()
    for word in re.findall(r"[A-Za-z][A-Za-z']{3,}", text or ""):
        low = word.lower().replace("'", "")
        if len(low) < 4 or low in _ASK_STOP or low in seen:
            continue
        seen.add(low)
        out.append(low)
    return out


def answers_the_ask(ask: str, draft: str) -> tuple:
    """False when the draft does not touch the ask in the latest message."""
    extracted = extract_ask(ask)
    if len(extracted) < 12:
        return True, ""
    ask_words = _ask_words(extracted)
    if len(ask_words) < 2:
        return True, ""
    draft_stems = set()
    for word in _ask_words(draft):
        draft_stems.add(word)
        if len(word) >= 5:
            draft_stems.add(word[:5])
    for word in ask_words:
        if word in draft_stems or (len(word) >= 5 and word[:5] in draft_stems):
            return True, ""
    return False, "off_topic: " + _clip(extracted, 90)


def _grade_row(row: dict) -> dict:
    passed = row.get("pass")
    if isinstance(passed, str):
        passed = passed.strip().strip('"').lower() in {"true", "yes", "pass"}
    answers = row.get("answers_ask")
    if isinstance(answers, str):
        answers = answers.strip().strip('"').lower() in {"true", "yes", "pass"}
    reason = _clip(str(row.get("reason") or ""), 160)
    if answers is False:
        passed = False
        reason = reason or "off_topic"
    return {"pass": bool(passed), "reason": reason or ("pass" if passed else "fail")}


def _parse_grade(text: str) -> dict:
    raw = re.sub(r"(?is)^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    candidates = []
    if raw.startswith("{"):
        candidates.append(raw)
    candidates.extend(re.findall(r"\{[^{}]+\}", raw))
    for cand in candidates:
        cleaned = re.sub(r",\s*}", "}", cand)
        try:
            row = json.loads(cleaned)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and ("pass" in row or "answers_ask" in row):
            return _grade_row(row)
    match = re.search(
        r"""['"]?pass['"]?\s*[:=]\s*(true|false|yes|no|"true"|"false"|'true'|'false')""",
        raw,
        re.I,
    )
    if match:
        bit = match.group(1).strip("\"'").lower() in {"true", "yes"}
        reason_m = re.search(r"""['"]?reason['"]?\s*[:=]\s*['"]([^'"]+)""", raw)
        reason = reason_m.group(1) if reason_m else ("pass" if bit else "fail")
        answers_m = re.search(
            r"""['"]?answers_ask['"]?\s*[:=]\s*(true|false|yes|no)""",
            raw,
            re.I,
        )
        if answers_m and answers_m.group(1).lower() in {"false", "no"}:
            bit = False
            reason = reason or "off_topic"
        return {"pass": bit, "reason": _clip(reason, 160)}
    return {"pass": False, "reason": "grade_unparsed"}


_INVENTED = {
    "deployed", "deployment", "deploys", "lambda", "lambdas", "kubernetes",
    "docker", "hotfix", "shipped", "shipping", "production", "midnight",
    "overnight", "fourteen", "fifteen", "thirteen", "twelve", "eleven",
    "hundred", "thousand", "million",
}


def unsupported_claims(ask: str, context: str, draft: str) -> list:
    """Numbers and concrete claim-words that are not in the ask or the context."""
    blob = "{0}\n{1}".format(ask or "", context or "").lower()
    missing = []
    for word in re.findall(r"[A-Za-z][A-Za-z']{3,}", draft or ""):
        low = word.lower().replace("'", "")
        if low in _INVENTED and low not in blob and low not in missing:
            missing.append(low)
    for num in re.findall(r"\b\d+\b", draft or ""):
        if num not in blob and num not in missing:
            missing.append(num)
    return missing[:6]


def _lower_voice(text: str) -> str:
    """operator starts in lowercase. Leave I and acronyms alone."""
    raw = (text or "").strip()
    if not raw or raw.startswith("I ") or raw.startswith("I'"):
        return raw
    lines = []
    for line in raw.splitlines():
        words = line.split(" ")
        if words and words[0] and words[0][0].isupper() and not words[0].isupper() and not words[0].startswith("I'"):
            words[0] = words[0][0].lower() + words[0][1:]
        lines.append(" ".join(words))
    return "\n".join(lines).strip()


def _shape_voice(text: str, ask: str, surface: str) -> str:
    """Strip dashes, promises, and stiff capitals. Fail open to the old lowercasing."""
    try:
        import voice_score
        shaped = voice_score.shape_reply(text or "", ask or "", surface or "")
    except Exception:
        shaped = ""
    if shaped and len(shaped.split()) >= 3:
        return _lower_voice(shaped)
    return _lower_voice(text)


_DANGLING = {
    "the", "a", "an", "to", "of", "and", "or", "for", "with", "in", "on", "at", "from", "that", "this",
}
_HEY = re.compile(r"(?i)\b(?:hey|hi|hello|dear)\s+([A-Za-z][a-z']{2,})\b")


def _cutoff(draft: str) -> str:
    """Cut off mid-sentence. A short reply with no period is still a finished line."""
    text = (draft or "").strip()
    if not text:
        return "cutoff"
    if text.endswith(("...", "…")) or text[-1] in ",:;":
        return "cutoff"
    last = re.split(r"\s+", text)[-1].lower().strip(".,;:!?")
    if last in _DANGLING:
        return "cutoff"
    if len(text) > 180 and text[-1] not in ".?!)" and not re.search(r"(?i)\bHOLD\b", text):
        return "cutoff"
    return ""


def _invented_recipient(ask: str, context: str, draft: str) -> str:
    blob = "{0}\n{1}".format(ask or "", context or "").lower()
    for email in _MAIL.findall(draft or ""):
        if email.lower() not in blob:
            return "invented_recipient"
    match = _HEY.search(draft or "")
    if match:
        name = match.group(1).lower()
        if name not in {"there", "all", "team", "operator"} and name not in blob:
            return "invented_recipient"
    return ""


def _hold_mismatch(ask: str, context: str, draft: str) -> str:
    blob = "{0}\n{1}".format(ask or "", context or "")
    must = bool(re.search(r"(?i)\b(?:reply|answer with|say)\s+HOLD\b", blob))
    is_hold = bool(re.match(r"(?i)\s*HOLD\b", draft or ""))
    if must and not is_hold:
        return "hold_mismatch"
    if is_hold and _latest_is_ack(ask):
        return "hold_mismatch"
    return ""


def _missing_facts(ask: str, context: str, draft: str) -> str:
    missing = unsupported_claims(ask, context, draft)
    if missing:
        return "missing_facts: " + ", ".join(missing)
    if "?" not in (draft or ""):
        return ""
    blob = "{0}\n{1}".format(ask or "", context or "").lower()
    for question in re.findall(r"[^?\n]{8,}\?", draft or "")[-2:]:
        words = _ask_words(question)
        if len(words) < 3:
            continue
        if not any(word in blob for word in words):
            return "missing_facts"
    return ""


def _miss_reason(ask: str, context: str, draft: str) -> str:
    on_topic, why = answers_the_ask(ask, draft)
    if not on_topic:
        return why if str(why).startswith("off_topic") else "off_topic"
    return (
        _cutoff(draft)
        or _invented_recipient(ask, context, draft)
        or _hold_mismatch(ask, context, draft)
        or _missing_facts(ask, context, draft)
    )


def _grade(complete, ask: str, context: str, draft: str, proof=None, work=None) -> dict:
    import fact_claim as _fc
    if not _fc.allowed(draft, context=context, proof=proof, work=work):
        return {"pass": False, "reason": "unproven_work"}
    miss = _miss_reason(ask, context, draft)
    if miss:
        return {"pass": False, "reason": miss}
    extracted = extract_ask(ask)
    messages = [
        {"role": "system", "content": (
            "Grade one draft. Reply with one JSON object and nothing else: "
            '{"extracted_ask":"the question","answers_ask":true,"pass":true,"reason":"short"}. '
            "First put the specific question or request from the latest message into extracted_ask. "
            "answers_ask is true only when the draft directly answers that question. "
            "Use only the ASK and the CONTEXT. Outside knowledge is a fail. "
            "When pass is false, reason must be one of: off_topic, missing_facts, invented_recipient, hold_mismatch, cutoff. "
            "off_topic: the draft does not answer the latest ask. "
            "missing_facts: a fact or a request in the draft is not in the ask or the context. "
            "invented_recipient: the draft names a person or an address that is not in the ask or the context. "
            "hold_mismatch: the draft says HOLD when the ask can be answered from the context, or it answers when the context says HOLD. "
            "cutoff: the draft stops mid-sentence. "
            "A short reply that stays inside the ask and the context passes. Do not fail it for an older quoted question. "
            "Do not fail a draft for not claiming that a record was changed. "
            "A draft that says the change is still pending passes when the context has no verified write."
        )},
        {"role": "user", "content": "EXTRACTED ASK\n{0}\n\nASK\n{1}\n\nCONTEXT\n{2}\n\nDRAFT\n{3}".format(
            _clip(extracted, 500), _clip(ask, 900), _clip(context, 1800), _clip(draft, 900)
        )},
    ]
    try:
        got = complete(messages, 80, json_mode=True)
    except TypeError:
        got = complete(messages, 80)
    if not got.get("ok"):
        return {"pass": False, "reason": got.get("reason") or "grade_error"}
    parsed = _parse_grade(got.get("text") or "")
    if not parsed.get("pass") and _fc.demands_false_confirmation(str(parsed.get("reason") or "")):
        if not _fc.completion_claims(draft):
            return {"pass": True, "reason": "no_false_confirm"}
        return {"pass": False, "reason": "unproven_work"}
    if parsed.get("pass") and not _fc.allowed(draft, context=context, proof=proof, work=work):
        return {"pass": False, "reason": "unproven_work"}
    if parsed.get("pass") or not _latest_is_ack(ask) or _PROMISE.search(draft or ""):
        return parsed
    if str(parsed.get("reason") or "").startswith("not_in_context"):
        return parsed
    return {"pass": True, "reason": "latest_note"}


def _rewrite(complete, ask: str, context: str, draft: str, reason: str) -> str:
    messages = [
        {"role": "system", "content": (
            "Rewrite the reply in plain words. Output only the reply. "
            "1 to 3 short sentences. Lowercase except names, dates, and I. "
            "Use only the ask and the context. Do not invent a fact. "
            "Do not mention a tool, a model, or a bot. No sign-off."
        )},
        {"role": "user", "content": "ASK\n{0}\n\nCONTEXT\n{1}\n\nWHY THE DRAFT FAILED\n{2}\n\nDRAFT\n{3}".format(
            _clip(ask, 900), _clip(context, 1800), _clip(reason, 160), _clip(draft, 700)
        )},
    ]
    got = complete(messages, 220)
    text = str(got.get("text") or "").strip()
    text = re.sub(r"^```(?:\w+)?\n?|\n?```$", "", text).strip()
    if _AI.search(text) or blocked(text):
        return ""
    return text


def _ref_hash(ref: str) -> str:
    if not ref:
        return ""
    return hashlib.sha1(str(ref).encode("utf-8", "replace")).hexdigest()[:10]


def _log_check(surface: str, status: str, reason: str, attempt: int, home: Path | None, ref: str = "") -> None:
    home = home or home_dir()
    _append(learning_dir(home) / _CHECK_LOG, {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "surface": str(surface or "")[:24],
        "status": status,
        "reason": _clip(reason, 160),
        "attempt": attempt,
        "ref_hash": _ref_hash(ref),
    })


def apply_check(ask: str, context: str, draft: str, *, surface: str = "", complete=None, home: Path | None = None, ref: str = "", proof=None, work=None) -> dict:
    """Grade the draft. Rewrite once if it fails. A second failure is not staged."""
    def _default_complete(messages, max_tokens=180, json_mode=False):
        try:
            return local_complete(messages, max_tokens, json_mode=json_mode)
        except TypeError:
            return local_complete(messages, max_tokens)
    complete = complete or _default_complete
    home = home or home_dir()
    draft = (draft or "").strip()
    if not draft:
        _log_check(surface, "fail", "empty", 1, home, ref)
        return {"ok": False, "text": "", "status": "fail", "reason": "empty", "attempts": 1}
    import fact_claim as _fc
    settled = _fc.settle(draft, context=context, proof=proof, work=work)
    if settled["action"] == "replace":
        _log_check(surface, "pass", "prepare_only", 1, home, ref)
        return {"ok": True, "text": _lower_voice(settled["text"]), "status": "rewrite_pass", "reason": "prepare_only", "attempts": 1}
    if settled["action"] == "block":
        _log_check(surface, "fail", "unproven_work", 1, home, ref)
        return {"ok": False, "text": "", "best_text": draft, "status": "fail", "reason": "unproven_work", "attempts": 1}
    first = _grade(complete, ask, context, draft, proof=proof, work=work)
    if first.get("reason") == "preempted":
        _log_check(surface, "fail", "preempted", 1, home, ref)
        return {"ok": False, "text": "", "status": "fail", "reason": "preempted", "attempts": 1}
    if first.get("reason") == "grade_unparsed":
        # One more grade. A format miss is not a pass, and it is not a rewrite.
        first = _grade(complete, ask, context, draft, proof=proof, work=work)
        if first.get("reason") == "grade_unparsed":
            _log_check(surface, "fail", "grade_unparsed", 2, home, ref)
            return {"ok": False, "text": "", "best_text": draft, "status": "fail", "reason": "grade_unparsed", "attempts": 2}
    if first.get("pass") and not _AI.search(draft):
        _log_check(surface, "pass", first.get("reason") or "pass", 1, home, ref)
        return {"ok": True, "text": _shape_voice(draft, ask, surface), "status": "pass", "reason": "", "attempts": 1}
    _log_check(surface, "fail", first.get("reason") or "fail", 1, home, ref)
    _why = str(first.get("reason") or "")
    if _why == "unproven_work" or _fc.demands_false_confirmation(_why):
        if not _fc.completion_claims(draft):
            _log_check(surface, "pass", "no_false_confirm", 1, home, ref)
            return {"ok": True, "text": _shape_voice(draft, ask, surface), "status": "pass", "reason": "no_false_confirm", "attempts": 1}
        if _fc.needs_pending(work):
            _log_check(surface, "pass", "prepare_only", 1, home, ref)
            return {"ok": True, "text": _lower_voice(_fc.pending_voice(work)), "status": "rewrite_pass", "reason": "prepare_only", "attempts": 1}
        _log_check(surface, "fail", "unproven_work", 1, home, ref)
        return {"ok": False, "text": "", "best_text": draft, "status": "fail", "reason": "unproven_work", "attempts": 1}
    rewritten = _rewrite(complete, ask, context, draft, first.get("reason") or "")
    if rewritten and not _fc.allowed(rewritten, context=context, proof=proof, work=work):
        if _fc.needs_pending(work):
            _log_check(surface, "pass", "prepare_only", 2, home, ref)
            return {"ok": True, "text": _lower_voice(_fc.pending_voice(work)), "status": "rewrite_pass", "reason": "prepare_only", "attempts": 2}
        _log_check(surface, "fail", "unproven_work", 2, home, ref)
        return {"ok": False, "text": "", "best_text": rewritten, "status": "fail", "reason": "unproven_work", "attempts": 2}
    if not rewritten:
        reason = first.get("reason") or "rewrite_empty"
        _log_check(surface, "fail", reason, 2, home, ref)
        return {"ok": False, "text": "", "best_text": draft, "status": "fail", "reason": reason, "attempts": 2}
    second = _grade(complete, ask, context, rewritten, proof=proof, work=work)
    if second.get("pass") and not _AI.search(rewritten):
        _log_check(surface, "pass", "rewrite", 2, home, ref)
        return {"ok": True, "text": _shape_voice(rewritten, ask, surface), "status": "rewrite_pass", "reason": "", "attempts": 2}
    reason = second.get("reason") or "failed_twice"
    _log_check(surface, "fail", reason, 2, home, ref)
    return {"ok": False, "text": "", "best_text": rewritten or draft, "status": "fail", "reason": reason, "attempts": 2}


def guard(result, *, ask: str, surface: str, log=None, home: Path | None = None, ref: str = ""):
    """Run the self-check on a draft dict. A double fail clears the text."""
    if not isinstance(result, dict):
        return result
    if result.get("self_checked") or os.environ.get("DRAFT_CONTEXT_OFF") == "1":
        return result
    text = str(result.get("text") or "").strip()
    if not text:
        return result
    pack = current() or {}
    if pack.get("blocked"):
        result = dict(result)
        result["ok"] = False
        result["text"] = ""
        result["error"] = "hold:data_line"
        result["never_send"] = True
        result["sent"] = False
        return result
    context = str(pack.get("text") or "")
    work = result.get("work") if isinstance(result.get("work"), dict) else None
    proof = result.get("write_proof") if isinstance(result.get("write_proof"), dict) else work
    checked = apply_check(ask, context, text, surface=surface, home=home, ref=ref, proof=proof, work=work)
    result = dict(result)
    result["self_checked"] = True
    result["self_check"] = checked.get("status")
    result["context_links"] = _kinds(pack.get("links") or [])
    result["context_n"] = len(pack.get("links") or [])
    result["never_send"] = True
    result["sent"] = False
    if not checked.get("ok"):
        reason = str(checked.get("reason") or "self_check")
        if log:
            try:
                log("draft_hold {0} {1}".format(surface, reason[:80]))
            except Exception:
                pass
        result["best_text"] = str(checked.get("best_text") or text)[:2000]
        result["ok"] = False
        result["text"] = ""
        result["error"] = "hold:" + reason[:120]
        return result
    result["text"] = checked.get("text") or ""
    result["ok"] = True if result.get("ok") is not False else result.get("ok")
    return result


def prebuild_item(item: dict, home: Path | None = None) -> dict:
    """Cache one context pack. No model call."""
    home = home or home_dir()
    label = str(item.get("label") or "")
    text = str(item.get("seed") or item.get("text") or "")
    ticket = ""
    if label.startswith("ado-") and label[4:].isdigit():
        ticket = label[4:]
    pack = build_pack(
        channel=str(item.get("source") or ""),
        subject=label,
        body=text,
        ticket_id=ticket,
        surface="prime",
        home=home,
        use_cache=False,
    )
    return {"ok": bool(pack.get("text") or pack.get("links")), "n": len(pack.get("links") or []), "blocked": pack.get("blocked") or ""}


def prebuild_open(home: Path | None = None, limit: int = 3) -> int:
    """Prime a few open tickets so the next real draft is ready. No model call."""
    home = home or home_dir()
    try:
        con = _db(home)
    except Exception:
        return 0
    try:
        rows = con.execute(
            "select ref, substr(ifnull(title,''),1,120), substr(ifnull(body,''),1,800) from docs "
            "where source='ado' and ref like 'wi_%' and ("
            "body like '%State: New%' or body like '%State: Active%' or body like '%State: Testing%') limit ?",
            (int(limit),),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        con.close()
    n = 0
    for ref, title, body in rows:
        wid = str(ref)[3:] if str(ref).startswith("wi_") else str(ref)
        prebuild_item({"label": "ado-" + wid, "source": "ado", "text": "{0}\n{1}".format(title, body), "seed": body}, home=home)
        n += 1
    return n


def join_selfcheck(home: Path | None = None) -> dict:
    """Weekly counts: self-check pass/fail joined to the ledger class. No draft text."""
    home = home or home_dir()
    learn = learning_dir(home)
    classes = {}
    outcomes = learn / "draft_outcomes.jsonl"
    if outcomes.is_file():
        for line in outcomes.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(row.get("key_hash") or "")
            if key:
                classes[key] = str(row.get("class") or "")
    matched = 0
    unmatched = 0
    by = {}
    total = 0
    path = learn / _CHECK_LOG
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            status = str(row.get("status") or "")
            ref = str(row.get("ref_hash") or "")
            cls = classes.get(ref) if ref else ""
            if cls:
                matched += 1
                bucket = by.setdefault(status, {})
                bucket[cls] = bucket.get(cls, 0) + 1
            else:
                unmatched += 1
    out = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "selfcheck_rows": total,
        "matched": matched,
        "unmatched": unmatched,
        "by_status_class": by,
    }
    dest = learn / _JOIN_LOG
    dest.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out
