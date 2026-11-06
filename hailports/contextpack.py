"""Read-only pages, edit lessons, and one self-check for a draft.

Nothing here sends a message or writes a record. One grade, then one rewrite.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from pathlib import Path

_AI = re.compile(r"\b(openai|chatgpt|claude|anthropic|copilot|minicpm|qwen|llm|automation|model|bot)\b", re.I)
_SECRET = re.compile(r"(?i)(?:password\s*(?:is|:|=)\s*\S|seed phrase|private key|\b(?:2fa|otp|passcode)\b)")
_HARD_PATH = re.compile(r"(?i)(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)")
_VOLATILE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{10,13}\b")
_PARENT = re.compile(r"(?m)^Parent:\s*(?:card-)?(\d{3,8})\s*$", re.I)
_CARD = re.compile(r"\bcard-(\d{3,8})\b", re.I)
_STOP = {"the", "and", "for", "with", "this", "that", "from", "have", "can", "you", "please", "thanks", "about"}
_COMMON = {
    "earlier", "already", "still", "while", "about", "because", "before", "after",
    "there", "their", "would", "could", "should", "please", "thanks", "looking",
    "checking", "added", "using", "where", "which", "these", "those", "other",
    "stays", "stayed", "allow", "allowed", "without", "inside", "around", "today",
    "tomorrow", "yesterday", "really", "going", "something", "nothing", "anyone",
    "people", "think", "right", "again", "being", "been", "just", "into", "them",
    "they", "will", "make", "made", "does", "didnt", "dont", "cant",
}


def blocked(text: str) -> str:
    blob = text or ""
    if _HARD_PATH.search(blob) or _SECRET.search(blob):
        return "data_line"
    return ""


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"[ \t]+", " ", (text or "").replace("\r", ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def stable_fp(text: str) -> str:
    cleaned = _VOLATILE.sub(" ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned.lower()).strip()
    return hashlib.sha1(cleaned.encode("utf-8", "replace")).hexdigest()[:16]


def skip_unchanged(record: dict | None, item: dict) -> bool:
    """Skip a model call when this item was already done and the words are the same.

    A stored row that has a hash and no fingerprint is treated as already seen.
    A different fingerprint means the words changed, so the item is still due.
    """
    record = record or {}
    if not record:
        return False
    fp = str(item.get("fp") or "") or stable_fp(str(item.get("text") or ""))
    if record.get("fp") and record.get("fp") == fp:
        return True
    if record.get("fp") and record.get("fp") != fp:
        return False
    if record.get("hash") and not record.get("fp"):
        return True
    return False


def _repo_ok(url: str, allowed: list[str] | None) -> bool:
    if not allowed:
        return True
    blob = (url or "").lower()
    return any(repo.lower() in blob for repo in allowed if repo)


def _shares_words(ask: str, page: str) -> bool:
    words = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9]{4,}", ask or ""):
        low = word.lower()
        if low in _STOP or low in words:
            continue
        words.append(low)
        if len(words) >= 4:
            break
    if len(words) < 2:
        return False
    blob = (page or "").lower()
    return sum(1 for word in words if word in blob) >= 2


def pack_pages(text: str, pages: list[dict] | None, *, ticket_id: str = "", allowed_repos: list[str] | None = None, limit: int = 8) -> dict:
    """Attach the pages that belong to this one item. Read only."""
    if blocked(text):
        return {"ok": False, "blocked": "data_line", "text": "", "links": [], "kinds": []}
    pages = pages or []
    want = str(ticket_id or "")
    if not want:
        match = _CARD.search(text or "")
        want = match.group(1) if match else ""
    links = []
    seen = set()

    def add(kind: str, ref: str, body: str) -> None:
        key = kind + ":" + str(ref)
        if not ref or key in seen or len(links) >= limit:
            return
        clip = _clip(body, 500)
        if len(clip) < 20 or blocked(clip):
            return
        seen.add(key)
        links.append({"kind": kind, "ref": str(ref)[:80], "text": clip})

    by_ref = {str(p.get("ref") or ""): p for p in pages}
    for page in pages:
        url = str(page.get("url") or "")
        ref = str(page.get("ref") or "")
        if _HARD_PATH.search(url) or _HARD_PATH.search(ref):
            continue
        body = str(page.get("body") or "")
        title = str(page.get("title") or "")
        source = str(page.get("source") or "")
        blob = "\n".join([ref, title, body, url])
        if want and want not in blob and not _mentions_parent(page, want):
            name = "{0} {1} {2}".format(source, title, ref).lower()
            if "runbook" in name or "playbook" in name:
                if not _shares_words(text, title + "\n" + body):
                    continue
            else:
                continue
        if source == "pull" or "github.com" in url:
            if want and f"AB#{want}" not in blob and f"/{want}" not in url:
                continue
            if not _repo_ok(url, allowed_repos):
                continue
            add("pr", ref or url, f"{url}\n{title}\n{body}")
            continue
        if source == "record" or ref.lower().startswith("rec-"):
            add("record", ref, f"{title}\n{body}")
            continue
        if source in {"runbook", "playbook"} or "runbook" in title.lower() or "playbook" in title.lower():
            kind = "playbook" if "playbook" in (title + source).lower() else "runbook"
            add(kind, ref or title, f"{title}\n{body}")
            continue
        if source == "comments" or ref.lower().startswith("comments"):
            add("comments", ref, f"{title}\n{body}")
            continue
        parent = _PARENT.search(body)
        if parent and by_ref.get("card-" + parent.group(1)):
            prow = by_ref["card-" + parent.group(1)]
            add("parent", "card-" + parent.group(1), f"{prow.get('title') or ''}\n{prow.get('body') or ''}")
        if want and (ref == "card-" + want or want in ref):
            add("history", ref, f"{title}\n{body}")
            continue
        if source in {"note", "page"}:
            add("note", ref, f"{title}\n{body}")
    kinds = []
    for row in links:
        if row["kind"] not in kinds:
            kinds.append(row["kind"])
    rendered = []
    used = 0
    for row in links:
        piece = "LINK {0} {1}\n{2}".format(row["kind"], row["ref"], row["text"])
        if used + len(piece) > 3600:
            break
        rendered.append(piece.strip())
        used += len(piece)
    return {"ok": True, "blocked": "", "text": "\n\n".join(rendered), "links": links, "kinds": kinds}


def _mentions_parent(page: dict, want: str) -> bool:
    return _PARENT.search(str(page.get("body") or "") or "") is not None and want in str(page.get("body") or "")


def _canon(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, _canon(a), _canon(b)).ratio()


def _diff(staged: str, sent: str) -> str:
    left = [ln.strip() for ln in (staged or "").splitlines() if ln.strip()] or [_clip(staged, 180)]
    right = [ln.strip() for ln in (sent or "").splitlines() if ln.strip()] or [_clip(sent, 180)]
    sm = difflib.SequenceMatcher(None, left, right)
    bits = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i1 != i2:
            bits.append("drop: " + _clip(" ".join(left[i1:i2]), 160))
        if j1 != j2:
            bits.append("keep: " + _clip(" ".join(right[j1:j2]), 160))
        if len(bits) >= 4:
            break
    return " | ".join(bits)


def _append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def _seen(path: Path) -> set:
    found = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
    except OSError:
        return found
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            if row.get("dedupe"):
                found.add(str(row["dedupe"]))
            if row.get("source_event_id"):
                found.add(str(row["source_event_id"]))
    return found


def record_pair(folder: Path, surface: str, key: str, staged: str, sent: str) -> str:
    """Store a gold example when the sent text matches, else a short diff lesson."""
    folder = Path(folder)
    staged = _clip(staged or "", 1200)
    sent = _clip(sent or "", 1200)
    if len(staged) < 12 or len(sent) < 12 or blocked(staged) or blocked(sent):
        return "skip"
    dedupe = hashlib.sha1(_canon(surface + "|" + staged + "|" + sent).encode()).hexdigest()[:16]
    if _ratio(staged, sent) >= 0.97:
        path = folder / "draft_gold.jsonl"
        if dedupe in _seen(path) or ("gold:" + str(key)) in _seen(path):
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
    lesson = _diff(staged, sent)
    if not lesson:
        return "skip"
    path = folder / "draft_edits.jsonl"
    event_id = "lesson:" + str(key)[:64]
    if dedupe in _seen(path) or event_id in _seen(path):
        return "dup"
    _append(path, {
        "kind": "lesson",
        "surface": surface,
        "source_event_id": event_id,
        "dedupe": dedupe,
        "diff": lesson,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return "lesson"


def _tokens(text: str) -> set:
    return {w.lower() for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower())}


def _load(path: Path, limit: int) -> list:
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


_NAME_RUN = re.compile(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+|[A-Z]{2,}(?:\s+[A-Z]{2,})+)\b")
_ADDR = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def scrub_style(text: str) -> str:
    """Keep sentence shape. Drop names, addresses, and numbers."""
    raw = _ADDR.sub("<addr>", text or "")
    raw = re.sub(r"\d+", "<n>", raw)
    raw = _NAME_RUN.sub("<name>", raw)
    raw = re.sub(r"(?:<n>\s*){2,}", "<n> ", raw)
    raw = re.sub(r"(?:<name>\s*){2,}", "<name> ", raw)
    raw = re.sub(r"(?:<addr>\s*){2,}", "<addr> ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _exclude_keys(folder: Path) -> set:
    path = Path(folder) / "eval_frozen" / "exclude.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    banned = set()
    for raw in list(doc.get("event_ids") or []) + list(doc.get("source_keys") or []) + list(doc.get("keys") or []):
        banned.add(str(raw))
    return banned


def style_block(folder: Path, k: int = 2) -> str:
    """Up to two style shots. Raw lessons stay out once style pins exist."""
    path = Path(folder) / "pinned_fewshots.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(rows, list):
        return ""
    banned = _exclude_keys(folder)
    lines = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("style_only"):
            continue
        key = str(row.get("key") or "")
        if key and key in banned:
            continue
        before = scrub_style(str(row.get("before") or ""))
        after = scrub_style(str(row.get("after") or ""))
        if len(before) < 12 or len(after) < 12:
            continue
        if re.search(r"\d", before + after) or "@" in (before + after):
            continue
        lines.append("before: " + before)
        lines.append("after: " + after)
        if len(lines) >= min(max(int(k or 1), 1), 2) * 2:
            break
    return "\n".join(lines)


def rebuild_style_pins(folder: Path, rows: list) -> dict:
    """Write at most two style pins. Sensitive and frozen keys are skipped."""
    folder = Path(folder)
    banned = _exclude_keys(folder)
    picked = []
    skipped = {"sensitive": 0, "frozen": 0, "empty": 0, "dirty": 0}
    ordered = sorted(
        rows or [],
        key=lambda row: (0 if str(row.get("class") or "") == "used_as_is" else 1, -float(row.get("score") or 0)),
    )
    seen_surface = set()
    for row in ordered:
        if row.get("sensitive") or str(row.get("class") or "") == "skipped_sensitive":
            skipped["sensitive"] += 1
            continue
        key = str(row.get("key") or "")
        if key and key in banned:
            skipped["frozen"] += 1
            continue
        surface = str(row.get("surface") or "mail")
        if surface in seen_surface:
            continue
        before = scrub_style(str(row.get("before") or row.get("draft") or ""))
        after = scrub_style(str(row.get("after") or row.get("sent") or ""))
        if len(before) < 12 or len(after) < 12:
            skipped["empty"] += 1
            continue
        if re.search(r"\d", before + after) or "@" in (before + after):
            skipped["dirty"] += 1
            continue
        picked.append({
            "style_only": True,
            "surface": surface,
            "key": key,
            "before": before,
            "after": after,
        })
        seen_surface.add(surface)
        if len(picked) >= 2:
            break
    if picked:
        (folder / "pinned_fewshots.json").write_text(json.dumps(picked, indent=2) + "\n", encoding="utf-8")
    return {"pins": len(picked), "skipped": skipped}


def similar_text(folder: Path, query: str, k: int = 3) -> str:
    folder = Path(folder)
    styled = style_block(folder, k=min(int(k or 2), 2))
    if styled:
        return styled
    q = _tokens(query)
    if not q:
        return ""
    scored = []
    seen = set()
    for row in _load(folder / "draft_edits.jsonl", 400):
        text = str(row.get("diff") or "")
        if not text or blocked(text):
            continue
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
        if digest in seen:
            continue
        seen.add(digest)
        score = len(q & _tokens(text))
        if score:
            scored.append((score, "lesson", text))
    for row in _load(folder / "draft_gold.jsonl", 200):
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
    return "\n".join("{0}: {1}".format(kind, _clip(text, 220)) for _score, kind, text in picked[:k])


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


def _shape_voice(text: str, ask: str) -> str:
    """Strip dashes, promises, and stiff capitals. Fail open to the old lowercasing."""
    try:
        from hailports.voice_score import shape_reply
        shaped = shape_reply(text or "", ask or "", "outlook")
    except Exception:
        shaped = ""
    if shaped and len(shaped.split()) >= 3:
        return _lower_voice(shaped)
    return _lower_voice(text)


def _lower_voice(text: str) -> str:
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


_ASK_HEADER = re.compile(
    r"(?im)^(state|area|iteration|tags|priority|parent|changed|assigned to)\s*:"
)
_ASK_STOP = {
    "this", "that", "with", "from", "have", "been", "were", "they", "them", "your",
    "about", "into", "over", "after", "before", "when", "what", "which", "where",
    "would", "could", "should", "please", "thanks", "there", "their", "task",
    "user", "story", "parent", "tags", "updated", "include", "state", "changed",
    "priority", "description", "only", "also", "then", "does", "for", "and", "the",
}


_QUOTE_START = re.compile(
    r"(?m)^\s*(?:From:\s|-----Original Message-----|_{5,}|On .+ wrote:\s*$)"
)
_PROMISE = re.compile(r"(?i)\b(i'll|i will|let me|follow up|circle back|tomorrow|next week)\b")


def _latest_message(text: str) -> str:
    """The newest note only. Quoted history is not the ask."""
    raw = (text or "").replace("\r", "")
    parts = re.split(r"\n(?:---+|LATEST(?: MESSAGE)?)\n", raw, flags=re.I)
    blob = parts[-1].strip()
    chunks = _QUOTE_START.split(blob, maxsplit=1)
    latest = (chunks[0] if chunks else blob).strip()
    return latest or blob


def _latest_is_ack(text: str) -> bool:
    """True only when the newest note is agreeing, not asking."""
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
    kept = [line for line in blob.splitlines() if not _ASK_HEADER.match(line.strip())]
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


def answers_the_ask(ask: str, draft: str) -> tuple[bool, str]:
    """False when the draft does not touch the ask in the latest message."""
    extracted = extract_ask(ask)
    if len(extracted) < 12:
        return True, ""
    ask_words = _ask_words(extracted)
    if len(ask_words) < 2:
        return True, ""
    stems = set()
    for word in _ask_words(draft):
        stems.add(word)
        if len(word) >= 5:
            stems.add(word[:5])
    for word in ask_words:
        if word in stems or (len(word) >= 5 and word[:5] in stems):
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
    candidates = [raw] if raw.startswith("{") else []
    candidates.extend(re.findall(r"\{[^{}]+\}", raw))
    for cand in candidates:
        try:
            row = json.loads(re.sub(r",\s*}", "}", cand))
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and ("pass" in row or "answers_ask" in row):
            return _grade_row(row)
    match = re.search(
        r"""['"]?pass['"]?\s*[:=]\s*(true|false|yes|no|"true"|"false")""",
        raw,
        re.I,
    )
    if not match:
        return {"pass": False, "reason": "grade_unparsed"}
    bit = match.group(1).strip('"').lower() in {"true", "yes"}
    reason_m = re.search(r"""['"]?reason['"]?\s*[:=]\s*['"]([^'"]+)""", raw)
    reason = reason_m.group(1) if reason_m else ("pass" if bit else "fail")
    return {"pass": bit, "reason": _clip(reason, 160)}


_DANGLING = {"the", "a", "an", "to", "of", "and", "or", "for", "with", "in", "on", "at", "from", "that", "this"}
_HEY = re.compile(r"(?i)\b(?:hey|hi|hello|dear)\s+([A-Za-z][a-z']{2,})\b")


def _cutoff(draft: str) -> str:
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
    for addr in _ADDR.findall(draft or ""):
        if addr.lower() not in blob:
            return "invented_recipient"
    match = _HEY.search(draft or "")
    if match:
        name = match.group(1).lower()
        if name not in {"there", "all", "team"} and name not in blob:
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
    return ""


def _miss_reason(ask: str, context: str, draft: str) -> str:
    on_topic, why = answers_the_ask(ask, draft)
    if not on_topic:
        return why if str(why).startswith("off_topic") else "off_topic"
    return _cutoff(draft) or _invented_recipient(ask, context, draft) or _hold_mismatch(ask, context, draft) or _missing_facts(ask, context, draft)


def _ref_hash(ref: str) -> str:
    if not ref:
        return ""
    return hashlib.sha1(str(ref).encode("utf-8", "replace")).hexdigest()[:10]


def join_checks(folder: Path) -> dict:
    """Counts only: self-check status joined to a later class. No draft text."""
    folder = Path(folder)
    classes = {}
    outcomes = folder / "draft_outcomes.jsonl"
    if outcomes.is_file():
        for row in _load(outcomes, 100000):
            key = str(row.get("key_hash") or "")
            if key:
                classes[key] = str(row.get("class") or "")
    matched = unmatched = total = 0
    by = {}
    for row in _load(folder / "draft_selfcheck.jsonl", 100000):
        total += 1
        ref = str(row.get("ref_hash") or "")
        cls = classes.get(ref) if ref else ""
        if cls:
            matched += 1
            bucket = by.setdefault(str(row.get("status") or ""), {})
            bucket[cls] = bucket.get(cls, 0) + 1
        else:
            unmatched += 1
    out = {"selfcheck_rows": total, "matched": matched, "unmatched": unmatched, "by_status_class": by}
    (folder / "draft_selfcheck_join.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def apply_check(ask: str, context: str, draft: str, *, complete, proof=None, work=None) -> dict:
    """Grade once. Retry a bad parse once. Rewrite once. A second miss returns empty text."""
    from hailports.factclaim import (
        allowed,
        completion_claims,
        demands_false_confirmation,
        needs_pending,
        pending_voice,
        settle,
    )

    draft = (draft or "").strip()
    if not draft:
        return {"ok": False, "text": "", "best_text": "", "status": "fail", "reason": "empty", "attempts": 1}
    settled = settle(draft, context=context, proof=proof, work=work)
    if settled["action"] == "replace":
        return {"ok": True, "text": _lower_voice(settled["text"]), "status": "rewrite_pass", "reason": "prepare_only", "attempts": 1}
    if settled["action"] == "block":
        return {"ok": False, "text": "", "best_text": draft, "status": "fail", "reason": "unproven_work", "attempts": 1}

    def grade(text: str) -> dict:
        if not allowed(text, context=context, proof=proof, work=work):
            return {"pass": False, "reason": "unproven_work"}
        miss = _miss_reason(ask, context, text)
        if miss:
            return {"pass": False, "reason": miss}
        extracted = extract_ask(ask)
        messages = [
            {"role": "system", "content": (
                "Grade one draft. Reply with one JSON object and nothing else: "
                '{"extracted_ask":"the question","answers_ask":true,"pass":true,"reason":"short"}. '
                "First put the specific question or request from the latest message into extracted_ask. "
                "answers_ask is true only when the draft directly answers that question. "
                "Use only the ask and the context. Outside knowledge is a fail. "
                "When pass is false, reason must be one of: off_topic, missing_facts, invented_recipient, hold_mismatch, cutoff. "
                "Do not fail a draft for not claiming that a record was changed. "
                "A draft that says the change is still pending passes when the context has no verified write."
            )},
            {"role": "user", "content": "EXTRACTED ASK\n{0}\n\nASK\n{1}\n\nCONTEXT\n{2}\n\nDRAFT\n{3}".format(
                _clip(extracted, 500), _clip(ask, 900), _clip(context, 1800), _clip(text, 900)
            )},
        ]
        try:
            got = complete(messages, 80, json_mode=True)
        except TypeError:
            got = complete(messages, 80)
        if not got.get("ok"):
            return {"pass": False, "reason": got.get("reason") or "grade_error"}
        parsed = _parse_grade(got.get("text") or "")
        if not parsed.get("pass") and demands_false_confirmation(str(parsed.get("reason") or "")):
            if not completion_claims(text):
                return {"pass": True, "reason": "no_false_confirm"}
            return {"pass": False, "reason": "unproven_work"}
        if parsed.get("pass") and not allowed(text, context=context, proof=proof, work=work):
            return {"pass": False, "reason": "unproven_work"}
        if parsed.get("reason") == "grade_unparsed" or parsed.get("pass"):
            return parsed
        if (
            _latest_is_ack(ask)
            and not _PROMISE.search(text or "")
            and not str(parsed.get("reason") or "").startswith("not_in_context")
        ):
            return {"pass": True, "reason": "latest_note"}
        return parsed

    first = grade(draft)
    if first.get("reason") == "preempted":
        return {"ok": False, "text": "", "best_text": draft, "status": "fail", "reason": "preempted", "attempts": 1}
    if first.get("reason") == "grade_unparsed":
        first = grade(draft)
        if first.get("reason") == "grade_unparsed":
            return {"ok": False, "text": "", "best_text": draft, "status": "fail", "reason": "grade_unparsed", "attempts": 2}
    if first.get("pass") and not _AI.search(draft):
        return {"ok": True, "text": _shape_voice(draft, ask), "status": "pass", "reason": "", "attempts": 1}
    why = str(first.get("reason") or "")
    if why == "unproven_work" or demands_false_confirmation(why):
        if not completion_claims(draft):
            return {"ok": True, "text": _shape_voice(draft, ask), "status": "pass", "reason": "no_false_confirm", "attempts": 1}
        if needs_pending(work):
            return {"ok": True, "text": _lower_voice(pending_voice(work)), "status": "rewrite_pass", "reason": "prepare_only", "attempts": 1}
        return {"ok": False, "text": "", "best_text": draft, "status": "fail", "reason": "unproven_work", "attempts": 1}
    messages = [
        {"role": "system", "content": (
            "Rewrite the reply in plain words. Output only the reply. "
            "1 to 3 short sentences. Use only the ask and the context. "
            "Do not invent a fact. Do not mention a tool, a model, or a bot."
        )},
        {"role": "user", "content": "ASK\n{0}\n\nCONTEXT\n{1}\n\nWHY\n{2}\n\nDRAFT\n{3}".format(
            _clip(ask, 900), _clip(context, 1800), _clip(first.get("reason") or "", 160), _clip(draft, 700)
        )},
    ]
    rewritten = str((complete(messages, 180) or {}).get("text") or "").strip()
    if _AI.search(rewritten) or blocked(rewritten):
        rewritten = ""
    if rewritten and not allowed(rewritten, context=context, proof=proof, work=work):
        if needs_pending(work):
            return {"ok": True, "text": _lower_voice(pending_voice(work)), "status": "rewrite_pass", "reason": "prepare_only", "attempts": 2}
        return {"ok": False, "text": "", "best_text": rewritten, "status": "fail", "reason": "unproven_work", "attempts": 2}
    if not rewritten:
        return {"ok": False, "text": "", "best_text": draft, "status": "fail", "reason": first.get("reason") or "rewrite_empty", "attempts": 2}
    second = grade(rewritten)
    if second.get("pass") and not _AI.search(rewritten):
        return {"ok": True, "text": _shape_voice(rewritten, ask), "status": "rewrite_pass", "reason": "", "attempts": 2}
    kept = rewritten or draft
    return {
        "ok": False,
        "text": "",
        "best_text": kept,
        "status": "fail",
        "reason": second.get("reason") or "failed_twice",
        "attempts": 2,
    }
