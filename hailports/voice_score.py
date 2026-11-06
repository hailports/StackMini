"""Deterministic score for a drafted reply. No model and no network.

The score is the mean of seven rule checks (0 or 1) blended with token F1
against the held-out reply. A higher score is a closer voice match.
"""
from __future__ import annotations

import re

_DAYS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}
_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
}
_STOP = {
    "the", "a", "an", "and", "or", "to", "for", "of", "on", "in", "at", "it",
    "this", "that", "is", "are", "was", "be", "you", "your", "i", "me", "my",
    "we", "our", "can", "just", "with", "from", "as", "if", "so", "do", "not",
    "no", "yes", "hey", "hi", "thanks", "thank", "please",
}
_AI = (
    "i hope this helps", "hope this helps", "great question", "certainly",
    "absolutely", "happy to help", "let me know if you have",
    "feel free to", "don't hesitate", "do not hesitate", "rest assured",
    "delve", "leverage", "robust", "seamless", "utilize", "streamline",
    "comprehensive", "furthermore", "moreover", "additionally", "in summary",
    "thank you for reaching out", "i apologize for any inconvenience",
    "i understand your frustration", "it appears that", "it seems that",
    "please note that", "it's important to note", "it's worth noting",
    "if i understand correctly",
)
_COMMIT = re.compile(
    r"(?i)\b("
    r"i'll|i will|let me|follow up|circle back|touch base|ping you|"
    r"get back to you|keep you posted|on it|will update|looking into|"
    r"i'll check|i will check|i'll look|i'll send|i will send|"
    r"tomorrow|next week|end of day|\beod\b|shortly|as soon as|"
    r"by (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r")\b"
)
_DONE = re.compile(
    r"(?i)\b("
    r"all set|taken care of|already sent|i sent it|"
    r"(?:it(?:'s| is)|that(?:'s| is)|we(?:'re| are)|i(?:'m| am)) "
    r"(?:done|fixed|complete|resolved|shipped|deployed)|"
    r"i (?:fixed|shipped|deployed|resolved)|resolved it|all done"
    r")\b"
)
_DASH = re.compile(r"—|–|--")
_NEWMAIL = re.compile(
    r"(?im)^\s*(subject\s*:|begin forwarded|-{5,}\s*original message|_{5,})"
)
_WORD = re.compile(r"[A-Za-z][A-Za-z']*")
_TOK = re.compile(r"[a-z0-9']+")
_SENT = re.compile(r"[.!?]+")


def _toks(text: str) -> list[str]:
    return [t for t in _TOK.findall((text or "").lower()) if t not in _STOP and len(t) > 2]


def token_f1(draft: str, gold: str) -> float:
    a, b = _toks(draft), _toks(gold)
    if not a or not b:
        return 0.0
    from collections import Counter
    ca, cb = Counter(a), Counter(b)
    overlap = sum(min(ca[t], cb[t]) for t in ca)
    if not overlap:
        return 0.0
    prec = overlap / max(1, sum(ca.values()))
    rec = overlap / max(1, sum(cb.values()))
    return 2 * prec * rec / (prec + rec)


def _allowed_caps(incoming: str) -> set[str]:
    allowed = set(_DAYS) | set(_MONTHS) | {"i"}
    for word in _WORD.findall(incoming or ""):
        if word[0].isupper() and not word.isupper():
            allowed.add(word.lower())
        elif word.isupper() and len(word) <= 4:
            allowed.add(word.lower())
    return allowed


def case_ok(draft: str, incoming: str) -> bool:
    allowed = _allowed_caps(incoming)
    for word in _WORD.findall(draft or ""):
        low = word.lower()
        if low in {"i", "i'm", "im", "i've", "ive", "i'd", "id"}:
            continue
        if word[0].isupper() and low not in allowed and low not in _DAYS and low not in _MONTHS:
            return False
    return True


def short_ok(draft: str) -> bool:
    words = _WORD.findall(draft or "")
    if not words or len(words) > 90:
        return False
    sents = [s for s in _SENT.split(draft or "") if s.strip()]
    return 1 <= len(sents) <= 4 or (len(sents) == 0 and len(words) <= 40)


def no_ai(draft: str) -> bool:
    low = (draft or "").lower()
    return not any(p in low for p in _AI)


def no_dash(draft: str) -> bool:
    return not _DASH.search(draft or "")


def no_commit(draft: str) -> bool:
    return not _COMMIT.search(draft or "")


def no_false_done(draft: str, incoming: str, gold: str) -> bool:
    if not _DONE.search(draft or ""):
        return True
    return bool(_DONE.search(incoming or "") or _DONE.search(gold or ""))


def in_thread(draft: str, incoming: str, gold: str, channel: str) -> bool:
    if not (draft or "").strip():
        return False
    if _NEWMAIL.search(draft):
        return False
    if (channel or "") == "outlook" and re.search(r"(?m)^>", draft):
        return False
    have = set(_toks(draft))
    src = set(_toks(incoming)) | set(_toks(gold))
    if not have:
        return False
    return len(have & src) >= 1


def score_reply(draft: str, incoming: str, gold: str, channel: str = "outlook") -> dict:
    draft = draft or ""
    checks = {
        "case": case_ok(draft, incoming),
        "short": short_ok(draft),
        "no_ai": no_ai(draft),
        "no_dash": no_dash(draft),
        "no_commit": no_commit(draft),
        "no_false_done": no_false_done(draft, incoming, gold),
        "in_thread": in_thread(draft, incoming, gold, channel),
    }
    rule = sum(1 for v in checks.values() if v) / len(checks)
    f1 = token_f1(draft, gold)
    total = 0.55 * rule + 0.45 * f1
    if not draft.strip():
        total = 0.0
        f1 = 0.0
        rule = 0.0
    return {
        "score": round(total, 4),
        "rule": round(rule, 4),
        "f1": round(f1, 4),
        "checks": checks,
    }


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _fix_caps(line: str, allowed: set[str]) -> str:
    bits = []
    for chunk in re.split(r"(\s+)", line):
        match = re.match(r"([A-Za-z][A-Za-z']*)(.*)$", chunk)
        if not match or not chunk.strip():
            bits.append(chunk)
            continue
        stem, rest = match.group(1), match.group(2)
        low = stem.lower()
        if low == "i":
            stem = "I"
        elif low in {"i'm", "im"}:
            stem = "I'm"
        elif low in {"i've", "ive"}:
            stem = "I've"
        elif low in {"i'd", "id"} and rest == "":
            stem = "I'd"
        elif stem[0].isupper() and low not in allowed and low not in _DAYS and low not in _MONTHS:
            stem = low
        bits.append(stem + rest)
    return "".join(bits)


def _strip_commit(sent: str) -> str:
    parts = re.split(r"\s*,\s*", sent)
    kept = [part for part in parts if part.strip() and not _COMMIT.search(part)]
    if not kept:
        return ""
    text = ", ".join(kept).strip()
    text = re.sub(r"\s+", " ", text)
    if text and text[-1] not in ".!?":
        text += "."
    return text


def shape_reply(draft: str, incoming: str, channel: str = "outlook") -> str:
    """Drop dashes, filler, promises, and unsupported done-claims. Keep a real answer."""
    allowed = _allowed_caps(incoming)
    lines = []
    for line in (draft or "").replace("—", ", ").replace("–", ", ").replace("--", ", ").splitlines():
        if re.match(r"\s*>", line):
            break
        if re.match(r"(?i)\s*(?:-{5,}\s*original message|_{5,}|from:\s+\S)", line):
            break
        if (channel or "") == "outlook" and re.match(r"(?i)\s*subject\s*:", line):
            continue
        lines.append(_fix_caps(line, allowed))
    text = "\n".join(lines).strip()
    incoming_done = bool(_DONE.search(incoming or ""))
    kept = []
    for sent in _sentences(text):
        low = sent.lower()
        if any(phrase in low for phrase in _AI):
            continue
        if _DONE.search(sent) and not incoming_done:
            continue
        sent = _strip_commit(sent)
        if not sent or not _WORD.search(sent):
            continue
        kept.append(sent)
    # A wipe scores as an empty draft. Keep the dash and case fix instead.
    if not kept:
        kept = _sentences(text)
    shaped = re.sub(r"\s+", " ", " ".join(kept)).strip()
    shaped = re.sub(r"\s+([,.;!?])", r"\1", shaped)
    # A sign-off or a fifth sentence is not how a short reply reads.
    sents = _sentences(shaped)
    cleaned = []
    for sent in sents:
        low = re.sub(r"[^a-z ]", "", sent.lower()).strip()
        if low in {
            "best", "thanks", "thank you", "regards", "best regards",
            "thanks Pat", "thank you Pat",
        }:
            continue
        cleaned.append(sent)
    if not cleaned:
        cleaned = sents
    words = []
    out = []
    for sent in cleaned[:4]:
        pieces = sent.split()
        if words and len(words) + len(pieces) > 90:
            break
        out.append(sent)
        words.extend(pieces)
    if not out:
        return shaped
    return " ".join(out)


def mean_score(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "score": 0.0, "rule": 0.0, "f1": 0.0}
    n = len(rows)
    return {
        "n": n,
        "score": round(sum(r["score"] for r in rows) / n, 4),
        "rule": round(sum(r["rule"] for r in rows) / n, 4),
        "f1": round(sum(r["f1"] for r in rows) / n, 4),
    }
