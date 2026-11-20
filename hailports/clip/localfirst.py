"""Local engine first, with a quality gate, then the premium engines.

When the saved order starts with local engines (http or ssh) and has a premium engine
(cursor, claude, codex) after them, a draft runs on the local engines only. The result
must pass the gate: the length cap, "hey <First>," then the answer, no markdown, no
number or list dumps, and "best," with the first name on email. A failed gate gets one
shorter rewrite on the same local engines. If that fails too, or the local engine is
down, busy or slow, the whole draft runs again on the premium engines. The engine that
produced each draft is written to engine-log.jsonl next to drafts.jsonl. Nothing is sent.

A figure-heavy thread skips the local engine: attachments in the thread, or lookup-context
files the thread names or refers to as attached, 3 or more figures in the thread (money, counts, percentages, ids with numbers, hours,
dates), or a table. It runs on cursor, claude, codex, then local only if they all fail, and the
reason is logged.
"""

from __future__ import annotations

import copy as _copy
import json
import os
import re
import time
from pathlib import Path

from hailports.clip import shape

PREMIUM = {"cursor", "claude", "codex"}
FIGURES_CURSOR_TIMEOUT_S = 180
THIN_FLOOR_FROM = 60  # words in their message before a reply under 25 words counts as too thin
_NUM = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?(?![\w])")
_LIST = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*•])\s+")
_MD = re.compile(r"(?m)^\s*(?:#{1,6}\s|\*\*|- \*\*|\|.*\|)")


def _via(spec: dict) -> str:
    return str((spec or {}).get("via") or "http").strip().lower()


def split(cfg: dict | None) -> tuple[dict, dict] | None:
    """(local-only route, premium-only route), or None when the order is not local-then-premium."""
    paiste = (cfg or {}).get("paiste") or {}
    models = paiste.get("models") or {}
    order = [str(n) for n in (paiste.get("order") or []) if isinstance(models.get(n), dict)]
    if not order or _via(models[order[0]]) in PREMIUM:
        return None
    local = [n for n in order if _via(models[n]) not in PREMIUM]
    premium = [n for n in order if _via(models[n]) in PREMIUM]
    if not local or not premium:
        return None

    def only(names: list[str]) -> dict:
        out = _copy.deepcopy(cfg)
        out["paiste"]["order"] = list(names)
        out["paiste"]["preferred"] = names[0]
        return out

    return only(local), only(premium)


def kind_cap(doc: dict | None) -> tuple[str, int]:
    kind = shape.surface(doc)
    incoming = shape.words(shape.newest_message(str((doc or {}).get("field_text") or "")))
    _target, cap = shape.word_cap(kind, incoming)
    return kind, cap


def incoming_words(doc: dict | None) -> int:
    return shape.words(shape.newest_message(str((doc or {}).get("field_text") or "")))


def gate(text: str, kind: str, cap: int, me: list[str] | None = None, incoming: int = 0) -> list[str]:
    """Problems that send a local draft to a rewrite, then to premium. Empty means it passes."""
    body = (text or "").strip()
    if not body:
        return ["empty"]
    core = shape.strip_close(body, me)
    n = shape.words(core)
    out = []
    if n > cap:
        out.append(f"too long: {n} words, at most {cap}")
    if kind == "email" and incoming >= THIN_FLOOR_FROM and n < 25:  # a short note may get a short reply
        out.append(f"too thin: {n} words")
    if not re.match(r"(?i)^hey\s+[A-Za-z][\w'.-]*,", body):
        out.append('does not open with "hey <first name>,"')
    if kind == "email":
        first = ((me or [""])[0].split() or [""])[0]
        if first and not re.search(r"(?i)\nbest,\n" + re.escape(first) + r"\s*$", body):
            out.append('no "best,\\n' + first + '" sign-off')
    if _MD.search(body):
        out.append("markdown")
    nums = {m.group(0) for m in _NUM.finditer(_LIST.sub("", core))}
    if len(nums) > 6:
        out.append(f"number dump: {len(nums)} figures, keep one or two key numbers")
    if len(_LIST.findall(core)) > 5:
        out.append("list dump: more than 5 list lines, keep 2 to 4 steps")
    return out


def shorter_messages(text: str, probs: list[str], kind: str, cap: int, me_first: str) -> list[dict]:
    target = min(cap, 120) if kind == "email" else min(cap, 45)
    return [
        {"role": "system", "content": shape.rules(kind, target, cap, me_first)},
        {"role": "user", "content": "Rewrite this reply so it fixes: " + "; ".join(probs)
         + f". At most {target} words. Keep the facts, the answer and the ask. Plain text, only the reply.\n\n"
         + text},
    ]


def log_path() -> Path:
    raw = os.environ.get("PAISTE_ENGINE_LOG", "")
    return Path(raw) if raw else Path.home() / ".config" / "paiste" / "engine-log.jsonl"


def log(row: dict, path: Path | None = None) -> None:
    p = path or log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **row}
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.chmod(p, 0o600)
    except OSError:
        pass


# Figure-heavy threads go to the premium engines first (cursor, claude, codex, then local).
_HEADER = re.compile(r"(?im)^[ \t>]*(?:from|sent|to|cc|bcc|subject|date|importance|reply-to)\s*:.*$")
_WROTE = re.compile(r"(?im)^[ \t>]*on .{6,120} wrote:\s*$")
_SIG = re.compile(r"(?im)^.*\b(?:mobile|phone|tel|cell|office|fax|direct|suite|street|avenue|blvd|road|drive)\b.*\d.*$"
                  r"|^.*\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b.*$")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\S+@\S+\.\w+")
_PHONE = re.compile(r"\+?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")
_FIG = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?\b"                       # money
    r"|\b\d+(?:\.\d+)?\s?%"                                         # percentages
    r"|\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b"                           # numeric dates
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"  # month day
    r"|\b[A-Za-z]{1,6}[-#]?\d{2,}\b"                                # ids with numbers (C18764, SR-1204)
    r"|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d{2,}(?:\.\d+)?\b|\b\d+\.\d+\b",  # counts, hours, amounts
    re.I)
_ATTACH_REF = re.compile(r"(?i)\b(?:attached|attachments?|enclosed|see the (?:file|sheet|deck|spreadsheet|doc))\b")
_FILE = re.compile(r"(?i)\b[\w() .-]{1,80}\.(?:xlsx|xlsm|xls|csv|pdf|docx?|pptx?|png|jpe?g|zip|txt)\b")


def _plain(text: str) -> str:
    out = []
    for line in (text or "").splitlines():
        if _HEADER.match(line) or _WROTE.match(line) or _SIG.match(line):
            continue
        out.append(_PHONE.sub(" ", _URL.sub(" ", line)))
    return "\n".join(out)


def figures(text: str) -> list[str]:
    seen: list[str] = []
    for m in _FIG.finditer(_plain(text)):
        v = re.sub(r"\s+", " ", m.group(0).strip().lower())
        if v not in seen:
            seen.append(v)
    return seen


def has_table(text: str) -> bool:
    lines = (text or "").splitlines()
    pipes = sum(1 for l in lines if l.count("|") >= 2)
    tabs = sum(1 for l in lines if l.count("\t") >= 2)
    return pipes >= 2 or tabs >= 2 or bool(re.search(r"(?i)<table\b", text or ""))


def _named_in(name: str, low: str) -> bool:
    """The file name (its last one to five words, with the extension) is written in the thread."""
    words = name.strip().lower().split()
    return any(" ".join(words[i:]) in low for i in range(max(0, len(words) - 5), len(words)))


def thread_text(doc: dict | None) -> str:
    doc = doc or {}
    parts = [str(doc.get(k) or "") for k in ("field_text", "selection", "window_ocr")]
    return "\n".join(p for p in parts if p.strip())


def classify(doc: dict | None, lookup_text: str = "") -> tuple[bool, list[str]]:
    """(figure-heavy, reasons). Attachments in the thread or the lookup context, 3 or more
    figures in the thread, or a table send the draft to the premium engines first."""
    from hailports.clip import attach
    doc = doc or {}
    text = thread_text(doc)
    why = []
    names = list(attach.names_in(text)) + [str(n) for n in (doc.get("attachments") or doc.get("files") or [])]
    if names:
        why.append("attachments in thread: " + ", ".join(names[:3]))
    # The lookup returns unrelated records too: its files count only when the thread names the
    # same file or refers to an attachment; its figures count only when the thread has them too.
    low = text.lower()
    refers = bool(_ATTACH_REF.search(text))
    found = sorted({m.group(0).strip() for m in _FILE.finditer(lookup_text or "")
                    if refers or _named_in(m.group(0), low)})
    if found:
        why.append("attachments in lookup context: " + ", ".join(found[:3]))
    figs = figures(text)
    body = _plain(text).lower()
    figs += [f for f in figures(lookup_text) if f not in figs and f in body]
    if len(figs) >= 3:
        why.append(f"{len(figs)} figures in thread: " + ", ".join(figs[:5]))
    if has_table(text):  # the lookup helper joins record fields with pipes; only the thread counts
        why.append("table")
    return bool(why), why


def premium_first(cfg: dict) -> dict | None:
    """The same engines, premium first in the order cursor, claude, codex, then the local ones."""
    paiste = (cfg or {}).get("paiste") or {}
    models = paiste.get("models") or {}
    order = [str(n) for n in (paiste.get("order") or []) if isinstance(models.get(n), dict)]
    rank = {"cursor": 0, "claude": 1, "codex": 2}
    prem = sorted([n for n in order if _via(models[n]) in PREMIUM], key=lambda n: (rank[_via(models[n])], order.index(n)))
    if not prem:
        return None
    out = _copy.deepcopy(cfg)
    out["paiste"]["order"] = prem + [n for n in order if n not in prem]
    out["paiste"]["preferred"] = prem[0]
    for n in prem:
        if _via(models[n]) == "cursor":
            # A thread full of figures is a long prompt: Cursor gets 180 s here (90 s elsewhere).
            out["paiste"]["models"][n]["timeout_s"] = FIGURES_CURSOR_TIMEOUT_S
    return out
