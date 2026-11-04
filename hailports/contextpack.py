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


def similar_text(folder: Path, query: str, k: int = 3) -> str:
    folder = Path(folder)
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


def extract_ask(text: str) -> str:
    """The specific question or request in the latest message."""
    raw = (text or "").replace("\r", "")
    parts = re.split(r"\n(?:---+|LATEST(?: MESSAGE)?)\n", raw, flags=re.I)
    blob = parts[-1].strip()
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


def apply_check(ask: str, context: str, draft: str, *, complete) -> dict:
    """Grade once. Retry a bad parse once. Rewrite once. A second miss returns empty text."""
    draft = (draft or "").strip()
    if not draft:
        return {"ok": False, "text": "", "status": "fail", "reason": "empty", "attempts": 1}

    def grade(text: str) -> dict:
        extracted = extract_ask(ask)
        messages = [
            {"role": "system", "content": (
                "Grade one draft. Reply with one JSON object and nothing else: "
                '{"extracted_ask":"the question","answers_ask":true,"pass":true,"reason":"short"}. '
                "First put the specific question or request from the latest message into extracted_ask. "
                "answers_ask is true only when the draft directly answers that question. "
                "A tag change, a date, or a field update is not an answer when the ask asks for something else. "
                "Pass only when answers_ask is true and every concrete claim is written in the ask or the context, "
                "the words are plain, and it names no tool, model, or bot. Outside knowledge is a fail."
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
        missing = unsupported_claims(ask, context, text)
        if missing:
            return {"pass": False, "reason": "not_in_context: " + ", ".join(missing)}
        on_topic, why = answers_the_ask(ask, text)
        if not on_topic:
            return {"pass": False, "reason": why}
        return _parse_grade(got.get("text") or "")

    first = grade(draft)
    if first.get("reason") == "preempted":
        return {"ok": False, "text": "", "status": "fail", "reason": "preempted", "attempts": 1}
    if first.get("reason") == "grade_unparsed":
        first = grade(draft)
        if first.get("reason") == "grade_unparsed":
            return {"ok": False, "text": "", "status": "fail", "reason": "grade_unparsed", "attempts": 2}
    if first.get("pass") and not _AI.search(draft):
        return {"ok": True, "text": _lower_voice(draft), "status": "pass", "reason": "", "attempts": 1}
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
    if not rewritten:
        return {"ok": False, "text": "", "status": "fail", "reason": first.get("reason") or "rewrite_empty", "attempts": 2}
    second = grade(rewritten)
    if second.get("pass") and not _AI.search(rewritten):
        return {"ok": True, "text": _lower_voice(rewritten), "status": "rewrite_pass", "reason": "", "attempts": 2}
    return {"ok": False, "text": "", "status": "fail", "reason": second.get("reason") or "failed_twice", "attempts": 2}
