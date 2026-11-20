"""Redraft keeps your edits: sentence locks.

When paisty's earlier draft is still in the reply box, each sentence there is compared with
what paisty placed. Sentences the user edited or wrote are locked: the redraft must keep them
word for word, in place, and only rewrites the rest. Punctuation, spacing and case edits count
as approval, so the user's version is locked too. Untouched paisty sentences stay redraftable.

    split(text)                       -> [Piece]   sentences with the whitespace after each
    diff(placed, original)            -> [dict]    each current sentence: text, kind, locked
    lock_plan(field, draft, pins, fresh) -> dict   what the popup shows and what the draft keeps
    prompt_block(plan)                -> str       the must-keep instruction for the model
    missing(text, locks)              -> [str]     locks not in the reply verbatim
    splice(text, plan)                -> str       put missing locks back in order and position
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from hailports.clip import owndraft

PAISTY = "paisty"      # untouched paisty sentence: redraftable
COSMETIC = "cosmetic"  # punctuation, spacing or case changed: approved, locked as the user has it
EDITED = "edited"      # words changed: locked
USER = "user"          # the user wrote it: locked

# Words that end with a period but do not end a sentence.
_ABBR = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "e.g", "i.e", "eg", "ie", "approx",
    "inc", "ltd", "co", "corp", "dept", "est", "fig", "no", "nos", "vol", "ext", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "mon", "tue", "tues", "wed", "thu", "thurs", "fri",
    "sat", "sun", "a.m", "p.m", "u.s", "cf", "al",
}
_TIME = {"a.m", "p.m"}
_BOUNDARY = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s)")
_BULLET = re.compile(r"^\s*(?:[-*•–]|\d{1,3}[.)])\s+")


@dataclass
class Piece:
    text: str       # the sentence, no surrounding whitespace
    tail: str = ""  # whitespace that followed it in the source
    start: int = 0  # offset of text in the source


def _ends_sentence(line: str, end: int) -> bool:
    """`end` is just past the punctuation run. Decide if it really ends a sentence."""
    head = line[:end]
    rest = line[end:].lstrip()
    if not rest:
        return True
    m = re.search(r"(\S+)$", head)
    word = (m.group(1) if m else "").rstrip("\"'”’)]")
    if not word.endswith("."):
        return True                       # ! ? … always end
    if word.endswith("..."):
        return not rest[0].islower()      # "so... yeah" carries on
    bare = word[:-1].lstrip("(\"'“‘").lower()
    if bare in _TIME:
        return rest[0].isupper()          # "at 3 p.m. tomorrow" carries on
    if bare == "etc":
        return not rest[0].islower()      # "etc." usually ends one, unless the next word is lowercase
    if bare in _ABBR:
        return False
    if re.fullmatch(r"[a-z]", bare):      # an initial: "J. Park"
        return False
    if re.fullmatch(r"\d{1,3}", bare) and head.strip() == word:
        return False                      # "1. first item" list marker
    return True


def _split_line(line: str, base: int) -> list[Piece]:
    out: list[Piece] = []
    start = 0
    for m in _BOUNDARY.finditer(line):
        end = m.end()
        if end <= start or not _ends_sentence(line, end):
            continue
        chunk = line[start:end]
        lead = len(chunk) - len(chunk.lstrip())
        if chunk.strip():
            gap = re.match(r"\s*", line[end:]).group(0)
            out.append(Piece(chunk.strip(), gap, base + start + lead))
            start = end + len(gap)
    chunk = line[start:]
    if chunk.strip():
        lead = len(chunk) - len(chunk.lstrip())
        out.append(Piece(chunk.strip(), "", base + start + lead))
    return out


def split(text: str) -> list[Piece]:
    """Sentences in reading order. A line break always ends a piece, so bullets stay whole."""
    text = (text or "").replace("\r\n", "\n").replace(" ", " ")
    pieces: list[Piece] = []
    pos = 0
    for line in text.split("\n"):
        found = _split_line(line, pos)
        if found:
            found[-1].tail = found[-1].tail + "\n"
            pieces.extend(found)
        elif pieces:
            pieces[-1].tail += "\n"       # a blank line belongs to the gap before the next piece
        pos += len(line) + 1
    if pieces:
        tail_ws = re.search(r"\s*$", text).group(0)
        pieces[-1].tail = tail_ws
    return pieces


def join(pieces: list[Piece]) -> str:
    return "".join(p.text + p.tail for p in pieces).strip()


def _key(text: str) -> str:
    """Words only: punctuation, spacing and case do not count."""
    return " ".join(re.findall(r"[\w$%@]+", (text or "").lower()))


def _flat(text: str) -> str:
    return " ".join((text or "").replace(" ", " ").split())


def _similar(a: str, b: str) -> float:
    wa, wb = _key(a).split(), _key(b).split()
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def diff(placed: str, original: str) -> list[dict]:
    """Classify each sentence now in the reply box against paisty's original draft.

    Matching is by content, not position, so reordered sentences keep their kind and the
    user's order is the order returned."""
    orig = [p.text for p in split(original)]
    used = [False] * len(orig)
    rows: list[dict] = []
    current = split(placed)
    # Pass 1: identical sentences claim their original first, so a duplicate edit can't steal it.
    kinds: list[str | None] = [None] * len(current)
    for i, piece in enumerate(current):
        for j, text in enumerate(orig):
            if not used[j] and text == piece.text:  # spacing inside a sentence counts as an edit
                used[j] = True
                kinds[i] = PAISTY
                break
    for i, piece in enumerate(current):
        if kinds[i]:
            continue
        key = _key(piece.text)
        hit = next((j for j, t in enumerate(orig) if not used[j] and key and _key(t) == key), None)
        if hit is not None:
            used[hit] = True
            kinds[i] = COSMETIC
            continue
        best, score = None, 0.0
        for j, text in enumerate(orig):
            if used[j]:
                continue
            s = _similar(piece.text, text)
            if s > score:
                best, score = j, s
        if best is not None and score >= 0.5:
            used[best] = True
            kinds[i] = EDITED
        else:
            kinds[i] = USER
    for i, piece in enumerate(current):
        rows.append({"text": piece.text, "kind": kinds[i], "locked": kinds[i] != PAISTY, "index": i})
    return rows


def lock_plan(field_text: str, draft: str, pins: list[str] | None = None, fresh: bool = False) -> dict:
    """Which sentences the redraft must keep.

    status: "ok" the draft is in the box; "gone" it was deleted (fresh, no locks);
            "fresh" the user asked for a fresh redraft; "empty" there is no draft to compare.
    pins:   None means decide from the edits. A list (even empty) is the user's explicit choice:
            exactly those sentences are kept, if they are still in the box."""
    draft = (draft or "").strip()
    if not draft:
        return {"status": "empty", "sentences": [], "locks": [], "explicit": False}
    spot = owndraft.locate(field_text or "", draft)
    if not spot.found:
        return {"status": "gone", "sentences": [], "locks": [], "explicit": False}
    rows = diff(spot.placed, draft)
    explicit = pins is not None
    if explicit:
        wanted = [_flat(p) for p in pins if _flat(p)]
        for row in rows:
            flat = _flat(row["text"])
            row["locked"] = flat in wanted
            if row["locked"]:
                wanted.remove(flat)
    if fresh:
        for row in rows:
            row["locked"] = False
    return {
        "status": "fresh" if fresh else "ok",
        "sentences": rows,
        "locks": [r["text"] for r in rows if r["locked"]],
        "explicit": explicit or fresh,
        "placed": spot.placed,
    }


def popup_rows(plan: dict) -> list[dict]:
    """What the Redraft popup lists: each sentence and whether it starts pinned."""
    return [{"text": r["text"], "kind": r["kind"], "pinned": bool(r["locked"])} for r in plan.get("sentences") or []]


def prompt_block(plan: dict | None) -> str:
    """The must-keep instruction. Empty when nothing is locked."""
    if not plan or not plan.get("locks"):
        return ""
    lines = [
        "KEEP THESE SENTENCES WORD FOR WORD. The user wrote or approved them. Copy each one exactly: "
        "same words, same punctuation, same case. Keep them in this order and in the same place in the reply. "
        "Rewrite only the sentences marked REWRITE, around them. Do not repeat what a kept sentence already says.",
        "The reply now, sentence by sentence:",
    ]
    for row in plan.get("sentences") or []:
        lines.append(("KEEP: " if row["locked"] else "REWRITE: ") + row["text"])
    lines.append("Kept sentences, exactly as they must appear:")
    lines.extend(f'- "{text}"' for text in plan["locks"])
    return "\n".join(lines)


def missing(text: str, locks: list[str]) -> list[str]:
    """Locks that are not in the reply verbatim (line breaks and runs of spaces may differ)."""
    body = _flat(text)
    return [lock for lock in locks or [] if _flat(lock) and _flat(lock) not in body]


def splice(text: str, plan: dict | None) -> str:
    """Put every missing lock back, deterministically, in the user's order and near its old place.

    A reply sentence that is a reworded copy of a missing lock is replaced by the lock. Otherwise
    the lock goes after the kept sentence that came before it, or at its share of the way through."""
    if not plan or not plan.get("locks"):
        return text
    rows = plan.get("sentences") or []
    gone = set(missing(text, plan["locks"]))
    if not gone:
        return text
    out = split(text)
    total = max(1, len(rows))
    cursor = 0
    for pos, row in enumerate(rows):
        if not row["locked"]:
            continue
        lock = row["text"]
        flat = _flat(lock)
        here = next((i for i, p in enumerate(out) if flat in _flat(p.text)), None)
        if lock not in gone or here is not None:
            if here is not None and here >= cursor:
                cursor = here + 1
            continue
        # A paraphrase of the lock after the cursor: swap it for the user's words.
        twin = max(range(cursor, len(out)), key=lambda i: _similar(out[i].text, lock), default=None)
        if twin is not None and _similar(out[twin].text, lock) >= 0.6:
            out[twin] = Piece(lock, out[twin].tail, 0)
            cursor = twin + 1
            continue
        if pos == 0:
            at = 0
        else:
            at = max(cursor, min(len(out), round(pos * len(out) / total)))
        tail = " "
        if at > 0 and at <= len(out) and not out[at - 1].tail:
            out[at - 1].tail = " "
        if at == len(out):
            tail = ""
        out.insert(at, Piece(lock, tail, 0))
        cursor = at + 1
    return join(out)


def enforce(text: str, plan: dict | None) -> str:
    """Last step of every redraft: a reply that dropped a lock gets it spliced back."""
    if not plan or not plan.get("locks") or not (text or "").strip():
        return text
    return splice(text, plan) if missing(text, plan["locks"]) else text
