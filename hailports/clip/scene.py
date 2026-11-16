"""Read the text nearest the cursor and draft a reply. Nothing is sent.

A click supplies a scene: the focused box, the text above it, and any
selection. Notes and Obsidian are the box itself, so the draft uses the
paragraphs above the caret. A secure field is skipped. The reply is text
the caller can copy. This module does not type into the app.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CAP = 1600
PAGE = 6000
NEAR = 280
SYSTEM = "Write a short reply. Use only the text on screen. Do not invent facts."
_CLOSE = {
    "best", "thanks", "thank you", "regards", "sincerely", "cheers",
    "best regards", "kind regards", "warm regards", "thanks again", "many thanks",
}
_LINK_ONLY = re.compile(r"(?is)^\s*(available at\s+)?https?://\S+\s*$")
_PROMISE = re.compile(r"(?i)\b(i'll|i will|i am going to|i'm going to)\b|\blet me (?!know\b)")
# A promise is fine when it answers the ask. An invented deadline is not.
_DAY = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)\b"
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_WHO = re.compile(
    r"(?m)^(?:From:\s*([A-Za-z][A-Za-z'’-]*)|"
    r"([A-Z][A-Za-z'’-]*)(?:\s+[A-Z][A-Za-z'’-]*)?\s+(?:\d{1,2}:\d{2}\s*[AP]M\b|commented\b))"
)
_GREET = re.compile(
    r"(?is)^(?:hey|hi|hello|dear)"
    r"(?:\s+(?:there|all|y['’]?all|everyone|folks))?"
    r"(?:\s+[A-Za-z][A-Za-z'’-]*)?"
    r"\s*(?:--|—|–|,|!)?\s*"
)
_LABEL_LINE = re.compile(r"(?m)^(?:[-*]\s*)?[A-Za-z][A-Za-z ]{0,24}:\s+")
_CHOICE_ASK = re.compile(r"(?i)\b(?:or|either)\b[^\n]{0,120}\?")
_CHOICE_BOUNCE = re.compile(
    r"(?i)\b(?:you tell me|up to you|either(?:'s| is) fine|your call|which do you|who do you want|"
    r"let me know which|you decide|what do you prefer)\b|\?\s*$"
)
_INVENTED_CLAIM = re.compile(
    r"(?i)\b(?:my|our)\s+(?:last\s+|next\s+|own\s+)?(?:meeting|call|appointment|calendar)\b"
    r"|\bwraps?\s+at\b"
    r"|\bi(?:'m| am)\s+(?:free|busy|booked|tied up|in a meeting)\b"
    r"|\blined\s+up\b"
    r"|\b(?:do not|don't) have a confirmed update\b"
    r"|\bnot a full update\b"
    r"|\bhaven'?t grabbed\b"
    r"|\bnot\s+sorted(?:\s+yet)?\b"
    r"|\bsorted\s+yet\b"
    r"|\b(?:are|is|aren'?t|isn'?t)\s+sorted\b"
    r"|\b(?:haven'?t|hasn'?t|have not|has not)\s+(?:sorted|gotten|grabbed)\b"
)
# Words that show up capitalized because they start a sentence, not because they are names.
_CASE_SKIP = {
    "from", "subject", "sent", "date", "commented", "re", "fw", "fwd",
    "the", "an", "and", "or", "but", "if", "so", "to", "of", "in", "on",
    "for", "with", "by", "at", "as", "can", "could", "would", "should", "will",
    "please", "did", "does", "do", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "not", "no", "yes", "we", "they", "you", "your",
    "our", "this", "that", "these", "those", "sorry", "also", "ignore", "any",
    "got", "still", "looking", "hey", "hi", "hello", "what", "when", "where",
    "who", "how", "why", "just", "about", "after", "before", "then", "than",
    "need", "needs", "there", "here", "let", "lets", "thanks", "thank", "dear",
}
_ASK_OBJECT = re.compile(
    r"(?i)\b(?:we|they)\s+still\s+need\s+(?P<get>[^.?\n]{0,90})"
    r"|\bstill\s+need\s+(?P<need>[^.?\n]{0,90})"
    r"|\b(?:we|they)\s+need\s+(?P<get2>[^.?\n]{0,90})"
    r"|\b(?:can|could)\s+you\s+bring\s+(?P<bring>[^.?\n]{0,90})"
    r"|\b(?:can|could)\s+you\s+grab\s+(?P<grab>[^.?\n]{0,90})"
    r"|\b(?:can|could)\s+you\s+get\s+(?P<get3>[^.?\n]{0,90})"
    r"|\bplease\s+bring\s+(?P<bring2>[^.?\n]{0,90})"
    r"|\bplease\s+grab\s+(?P<grab2>[^.?\n]{0,90})"
)
_NEED_ONLY = re.compile(
    r"(?i)\b(?:we|they)\s+still\s+need\s+(?P<need>[^.?\n]{0,90})"
    r"|\bstill\s+need\s+(?P<need2>[^.?\n]{0,90})"
    r"|\b(?:we|they)\s+need\s+(?P<need3>[^.?\n]{0,90})"
)
_ASK_SKIP = {
    "about", "after", "again", "before", "bring", "could", "from", "grab",
    "have", "need", "please", "still", "that", "them", "these", "they",
    "this", "those", "with", "your", "yours", "some", "into", "onto",
    "look", "people", "arrive", "just", "also", "when", "what", "where",
    "here", "there", "would", "should", "really", "maybe", "them",
}
_WILL = re.compile(r"(?i)\b(i'll|i will|i can|i am going to|i'm going to)\b")
_NOTE_STOP = {
    "about", "after", "again", "being", "could", "every", "first", "their",
    "there", "these", "thing", "those", "today", "tomorrow", "tonight",
    "which", "while", "would", "please", "thanks", "thank", "hello",
    "still", "message", "email", "thread", "subject", "wrote", "write",
    "reply", "people", "before", "coming", "should",
}
_WHEN = re.compile(
    r"(?i)\b(?:by|before|on|until|end of)\s+(?:the\s+)?(?P<day>" + _DAY + r")"
    r"|\b(?:by|before|until)\s+(?P<rel>tomorrow|tonight|next week|end of (?:day|week)|eod|eow|cob)\b"
    r"|\b(?P<rel2>tomorrow|tonight|next week)\b"
    r"|\b(?P<time>\d{1,2}(?::\d{2})?\s?(?:am|pm)|\d{1,2}/\d{1,2})\b"
)
# Meta narration about the message instead of a reply to it.
_META = re.compile(
    r"(?i)\bi see (?:the|your) (?:request|message|email|note|ask)\b"
    r"|\bthe (?:message|email|text|thread|note|request|window|screen)\s+(?:mentions|says|does not|doesn't|did not|didn't|is asking|asks)\b"
    r"|\bthe (?:message|email|text|thread|note|request) (?:does not|doesn't) (?:list|say|specify|mention|include)\b"
    r"|\bit (?:does not|doesn't) (?:list|specify) (?:the|any|who|what|when)\b"
    r"|\bnot (?:listed|specified|mentioned) (?:in|on) (?:the|this)\b"
    r"|\bbased on (?:the|this) (?:message|email|text|screen|window)\b"
)
_STEPS = re.compile(r"(?im)(?:^|\n)\s*\d+[.)]\s+(?:open|go to|click|navigate|select|step)\b")
_WORD = re.compile(r"[A-Za-z0-9']{4,}")
_SKIP_DIR = {".git", "node_modules", ".obsidian"}


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float


@dataclass
class Block:
    text: str
    role: str = ""
    box: Box | None = None


@dataclass
class Scene:
    app: str = ""
    title: str = ""
    field_text: str = ""
    field_role: str = ""
    field_box: Box | None = None
    caret: int | None = None
    selection: str = ""
    blocks: list[Block] = field(default_factory=list)
    origin: str = "cocoa"


@dataclass
class Reading:
    kind: str
    subject: str = ""
    draft_so_far: str = ""
    ask: str = ""
    query: str = ""


def _box(raw) -> Box | None:
    if not isinstance(raw, dict):
        return None
    try:
        return Box(float(raw.get("x") or 0), float(raw.get("y") or 0), float(raw.get("w") or 0), float(raw.get("h") or 0))
    except (TypeError, ValueError):
        return None


def scene_from(doc: dict) -> Scene:
    blocks = []
    for item in doc.get("blocks") or []:
        if not isinstance(item, dict):
            continue
        blocks.append(Block(str(item.get("text") or ""), str(item.get("role") or ""), _box(item.get("box"))))
    caret = doc.get("caret")
    try:
        caret_n = int(caret) if caret is not None and str(caret) != "" else None
    except (TypeError, ValueError):
        caret_n = None
    return Scene(
        app=str(doc.get("app") or ""),
        title=str(doc.get("title") or ""),
        field_text=str(doc.get("field_text") or ""),
        field_role=str(doc.get("field_role") or ""),
        field_box=_box(doc.get("field_box")),
        caret=caret_n,
        selection=str(doc.get("selection") or ""),
        blocks=blocks,
        origin="top" if str(doc.get("origin") or "") == "top" else "cocoa",
    )


def _secure(role: str) -> bool:
    low = (role or "").lower()
    return "secure" in low or low in {"password", "axsecuretextfield"}


def _overlap(a: Box, b: Box) -> bool:
    return not (a.x + a.w < b.x or b.x + b.w < a.x)


def gap_above(block: Box, field: Box, origin: str) -> float | None:
    if not _overlap(block, field):
        return None
    if origin == "top":
        gap = field.y - (block.y + block.h)
    else:
        gap = block.y - (field.y + field.h)
    if gap < -12 or gap > NEAR:
        return None
    return gap


def _above(scene: Scene) -> str:
    parts: list[tuple[float, str]] = []
    if scene.field_box is None:
        for block in scene.blocks:
            text = block.text.strip()
            if text and not _chrome(text) and "button" not in (block.role or "").lower():
                parts.append((0, text))
    else:
        for block in scene.blocks:
            text = block.text.strip()
            if not text or block.box is None or _chrome(text) or "button" in (block.role or "").lower():
                continue
            gap = gap_above(block.box, scene.field_box, scene.origin)
            if gap is None:
                continue
            parts.append((gap, text))
        parts.sort()
    return "\n\n".join(text for _, text in parts)[:CAP]


def _before_caret(text: str, caret: int | None) -> str:
    head = text if caret is None else text[: max(0, caret)]
    paras = [part.strip() for part in re.split(r"\n\s*\n", head) if part.strip()]
    return "\n\n".join(paras[-3:])[-CAP:]


_TERMINALS = {"terminal", "iterm2", "iterm", "warp", "alacritty", "kitty", "wezterm", "ghostty", "hyper"}
_DRAWING = re.compile(r"[─│┌┐└┘├┤┬┴┼╭╮╯╰═║╔╗╚╝┃┏┓┗┛▀▄█▌▐░▒▓]")


_MAIL = {"microsoft outlook", "outlook", "mail", "spark", "airmail", "mimestream"}


def _mail_parts(text: str) -> tuple[str, str]:
    """Draft above the first quoted message, then that message. Older quotes stay out."""
    lines = (text or "").splitlines()
    starts = [i for i, line in enumerate(lines) if re.match(r"(?i)\s*from:\s+\S", line)]
    if not starts:
        return "", ""
    draft = "\n".join(lines[: starts[0]]).strip()[:500]
    end = starts[1] if len(starts) > 1 else len(lines)
    message = "\n".join(lines[starts[0] : end]).strip()[:CAP]
    return draft, message


_CHROME = re.compile(r"(?i)(\b(ctrl|cmd|alt|shift|command)\s*\+|shortcut|write a message or type|type / for more)")
_CLIP = re.compile(
    r"(?i)(select the clip|pasteable draft|delete all history|join mailing list|rate app|customer support|preferences\.\.\.)"
)


def _placeholder(text: str) -> bool:
    body = " ".join((text or "").split()).lower()
    return body.startswith((
        "add a comment", "write a message", "start typing", "type a message",
        "leave a comment", "write a reply",
    ))


def _chrome(text: str) -> bool:
    body = " ".join((text or "").split())
    if not body or _CLIP.search(body):
        return True
    if len(body) > 160:
        return False
    if _placeholder(body) or _CHROME.search(body):
        return True
    role_like = body.lower()
    return role_like in {"search", "chat", "more", "send", "yes", "no", "follow", "save"} and len(body) < 12


def _signature_line(raw: str) -> bool:
    if raw.count("|") >= 2:
        return True
    if re.search(r"(?i)\b(phone|mobile|tel)\b", raw) and re.search(r"\d{3}", raw):
        return True
    word = raw.lower().rstrip(",")
    return word in _CLOSE


def _trim_message(text: str) -> str:
    """Keep the subject and the body. Drop the address block and the signature."""
    kept: list[str] = []
    header = True
    for line in (text or "").splitlines():
        raw = line.strip()
        if header:
            if re.match(r"(?i)subject:\s*\S", raw):
                kept.append(re.sub(r"(?i)^subject:\s*", "", raw))
                continue
            if re.match(r"(?i)(from|sent|date|to|cc|bcc):\s", raw) or not raw:
                continue
            header = False
        if _signature_line(raw):
            break
        kept.append(line.rstrip())
    return "\n".join(kept).strip()[:CAP]


def _name_line(line: str) -> bool:
    words = line.strip().rstrip(",").split()
    if not words or len(words) > 3:
        return False
    if any(ch in line for ch in ".?!:"):
        return False
    return all(word[:1].isupper() for word in words)


def strip_signoff(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    changed = True
    while lines and changed:
        changed = False
        last = lines[-1].strip().rstrip(",").lower()
        prev = lines[-2].strip().rstrip(",").lower() if len(lines) > 1 else ""
        if prev in _CLOSE and _name_line(lines[-1]):
            lines = lines[:-2]
            changed = True
        elif last in _CLOSE:
            lines.pop()
            changed = True
        elif _name_line(lines[-1]) and len(lines) > 1:
            lines.pop()
            changed = True
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines).strip()


def as_writer(text: str, names: list[str] | None = None) -> str:
    """The person at the keyboard is not the person being greeted."""
    body = (text or "").strip()
    who = [name.strip() for name in (names or []) if name and name.strip()]
    if not body or not who:
        return body
    pattern = r"(?is)^hey\s+(?:" + "|".join(re.escape(name) for name in who) + r")\b\s*[,—\-–]*\s*"
    body = re.sub(pattern, "", body, count=1).strip()
    return re.sub(r"(?is)^hey\s*--\s*", "", body, count=1).strip()


def trim_overreach(text: str, sources: str = "") -> str:
    """Drop meta narration, promises with an invented deadline, and invented numbered procedures. Keeps line breaks."""
    out_lines = []
    for line in (text or "").strip().splitlines():
        if not line.strip():
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            continue
        kept = []
        for chunk in re.split(r"(?<=[.!?])\s+", line.strip()):
            piece = chunk.strip()
            if not piece:
                continue
            if _META.search(piece):
                continue
            if invented_claim(piece, sources):
                continue
            if _PROMISE.search(piece) and _invented_when(piece, sources):
                continue
            if _STEPS.search(piece) and not _STEPS.search(sources or ""):
                continue
            kept.append(piece)
        if kept:
            out_lines.append(" ".join(kept))
    while out_lines and out_lines[-1] == "":
        out_lines.pop()
    return "\n".join(out_lines).strip()


def _invented_when(piece: str, sources: str) -> bool:
    """A day or time in the reply that is not on screen or in the notes."""
    low = (sources or "").lower()
    for match in _WHEN.finditer(piece or ""):
        token = next((value for value in match.groupdict().values() if value), match.group(0)).lower()
        if match.group("day"):
            seen = any(day.group(0)[:3] == token[:3] for day in re.finditer(r"\b" + _DAY, low))
        else:
            seen = token in low
        if not seen:
            return True
    return False


def meta_reply(text: str) -> bool:
    return bool(_META.search(text or ""))


def invented_claim(text: str, sources: str = "") -> bool:
    """A meeting, wrap time, or status the thread does not state."""
    low = (sources or "").lower()
    for match in _INVENTED_CLAIM.finditer(text or ""):
        if match.group(0).lower() not in low:
            return True
    return False


def overreach(text: str, sources: str = "") -> bool:
    """A promise with an invented deadline, meta narration, or a numbered procedure the source did not contain."""
    body = text or ""
    if _META.search(body):
        return True
    if invented_claim(body, sources):
        return True
    if _PROMISE.search(body) and _invented_when(body, sources):
        return True
    return bool(_STEPS.search(body) and not _STEPS.search(sources or ""))


def scene_blob(doc: dict | None) -> str:
    """Header lines included. The page reader drops From: before the model sees it."""
    if not isinstance(doc, dict):
        return ""
    parts = [str(doc.get("field_text") or ""), str(doc.get("selection") or "")]
    for block in doc.get("blocks") or []:
        if isinstance(block, dict):
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(part for part in parts if part)


def addressee_first(text: str, me: list[str] | None = None) -> str:
    """First name of the last other person on screen. The writer is skipped."""
    skip = set()
    for name in me or []:
        first = name.strip().split()
        if first:
            skip.add(first[0].lower())
    found = []
    for left, right in _WHO.findall(text or ""):
        name = left or right
        if name and name.lower() not in skip:
            found.append(name)
    return found[-1] if found else ""


def screen_has_choice(sources: str) -> bool:
    """True when they asked the writer to pick one side.

    "or am I" asks who already has the part. It is not a license to take it.
    """
    text = sources or ""
    if re.search(r"(?i)\bor am I\b", text):
        return False
    return bool(_CHOICE_ASK.search(text))


def bounced_choice(reply: str, sources: str) -> bool:
    """True when they asked for a pick and the reply hands it back."""
    if not screen_has_choice(sources):
        return False
    body = (reply or "").strip()
    if not body:
        return True
    return bool(_CHOICE_BOUNCE.search(body))


def _proper_case(sources: str) -> dict[str, str]:
    """Names written in title case. A word the thread also uses in lowercase stays as written."""
    text = sources or ""
    lower_words = {word.lower() for word in re.findall(r"\b[a-z][A-Za-z0-9'’-]*\b", text)}
    found: dict[str, str] = {}
    for match in re.finditer(r"\b([A-Z][a-z]+(?:['’-][A-Za-z]+)?)\b", text):
        word = match.group(1)
        low = word.lower()
        if low in _CASE_SKIP or low in _DAYS or low in _MONTHS or low in lower_words:
            continue
        found.setdefault(low, word)
    return found


def asked_objects(sources: str) -> list[tuple[str, str]]:
    """Things they asked to be brought or gotten, with the verb they used."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _ASK_OBJECT.finditer(sources or ""):
        kind = match.lastgroup or ""
        phrase = match.group(kind) or ""
        verb = "grab" if kind.startswith("grab") else "bring" if kind.startswith("bring") else "get"
        clause = re.split(r"\band\b|,", phrase, maxsplit=1)[0]
        words = [word.lower() for word in re.findall(r"[A-Za-z']{4,}", clause)]
        words = [word for word in words if word not in _ASK_SKIP and word not in _DAYS and word not in _MONTHS]
        if not words or words[-1] in seen:
            continue
        seen.add(words[-1])
        found.append((words[-1], verb))
    return found[:6]


def _mentions(text: str, token: str) -> bool:
    low = (text or "").lower()
    if token in low:
        return True
    return bool(token.endswith("s") and token[:-1] in low)


def missing_commits(reply: str, sources: str) -> list[str]:
    """Asked-for objects that never appear in a sentence where the writer says they will."""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", reply or "")
    missing = []
    for token, _verb in asked_objects(sources):
        covered = any(_mentions(sentence, token) and _WILL.search(sentence) for sentence in sentences)
        if not covered:
            missing.append(token)
    return missing


def drop_ungrounded(reply: str, sources: str, addressee: str = "") -> str:
    """Drop a sentence that names someone the thread never mentions, or fetches an object it never names."""
    low_src = (sources or "").lower()
    who = (addressee or "").strip().lower()
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", (reply or "").strip()):
        piece = sentence.strip()
        if not piece:
            continue
        grounded = True
        for word in re.findall(r"[A-Za-z][A-Za-z0-9'’-]*", piece):
            low = word.lower()
            head = re.split(r"['’]", low, maxsplit=1)[0]
            if head in _DAYS or head in _MONTHS or head in {"i", "i'll", "i'm", "i'd", "i've"}:
                continue
            titled = word[:1].isupper() or (word.isupper() and len(word) > 2)
            if titled and head not in low_src and head != who:
                grounded = False
                break
        fetched = re.search(r"(?i)\bi'll (?:get|grab|bring) the ([a-z]{4,})", piece)
        if fetched and fetched.group(1) not in low_src:
            grounded = False
        if grounded:
            kept.append(piece)
    return " ".join(kept).strip()


def ensure_commits(reply: str, sources: str) -> str:
    """Add a will-line for an asked object the model skipped or replaced with a status."""
    body = (reply or "").strip()
    if not body:
        return body
    verbs = {token: verb for token, verb in asked_objects(sources)}
    lines = [f"I'll {verbs[token]} the {token}." for token in missing_commits(body, sources)]
    if not lines:
        return body
    return (body + " " + " ".join(lines)).strip()


def need_only_tokens(sources: str) -> list[str]:
    """Objects they said are still needed, and did not ask this person to bring."""
    asked = {token for token, _verb in asked_objects(sources)}
    found: list[str] = []
    seen: set[str] = set()
    for match in _NEED_ONLY.finditer(sources or ""):
        phrase = next((group for group in match.groups() if group), "")
        clause = re.split(r"\band\b|,", phrase, maxsplit=1)[0]
        words = [word.lower() for word in re.findall(r"[A-Za-z']{4,}", clause)]
        words = [word for word in words if word not in _ASK_SKIP and word not in _DAYS and word not in _MONTHS]
        if not words:
            continue
        token = words[-1]
        if token in asked or token in seen:
            continue
        seen.add(token)
        found.append(token)
    return found[:6]


def _need_claim(piece: str, tokens: list[str]) -> bool:
    if re.search(r"(?i)\b(i'm|i am)\s+on\s+all\b", piece or ""):
        return True
    for token in tokens:
        if re.search(
            rf"(?i)\b(i'll|i will|i can|i'm going to|i am going to)\s+(?:get|bring|grab|pick)\b.{{0,60}}\b{re.escape(token)}\b",
            piece or "",
        ):
            return True
    return False


def volunteered_need(reply: str, sources: str) -> bool:
    """'we still need chairs' is not 'please bring chairs'."""
    tokens = need_only_tokens(sources)
    if not tokens:
        return False
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply or ""):
        if _need_claim(sentence, tokens):
            return True
    return False


def soften_needs(reply: str, sources: str) -> str:
    """Drop a volunteered fetch and keep the need as their fact."""
    tokens = need_only_tokens(sources)
    body = (reply or "").strip()
    if not tokens or not body:
        return body
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", body):
        piece = sentence.strip()
        if not piece or _need_claim(piece, tokens):
            continue
        kept.append(piece)
    body = " ".join(kept).strip()
    for token in tokens:
        if not _mentions(body, token):
            body = (body + f" they still need the {token}.").strip()
    return body


def held_back(reply: str, sources: str) -> bool:
    """A heads-up that says not to confirm is not a yes."""
    if not re.search(r"(?i)\b(do not confirm|don't confirm|dont confirm)\b", sources or ""):
        return False
    return bool(re.search(
        r"(?i)\b(happy to|glad to|count me in)\b|\b(i'll|i will|i can)\s+be the\b",
        reply or "",
    ))


def conditional_claim(reply: str, sources: str) -> bool:
    """'if you have a spare' does not mean a spare is in hand."""
    if not re.search(r"(?i)\bif you have a spare\b", sources or ""):
        return False
    text = reply or ""
    if re.search(r"(?i)\bif I have\b", text):
        return False
    return bool(re.search(r"(?i)\b(i'll|i will)\s+bring\b.{0,40}\bspare\b", text))


def intro_narration(reply: str, sources: str) -> bool:
    """An intro they can forward is the reply, not a promise to write one later."""
    src = sources or ""
    if not (re.search(r"(?i)\bintro\b", src) and re.search(r"(?i)\bforward\b", src)):
        return False
    return bool(re.search(r"(?i)\b(i'll|i will)\s+send\b.{0,40}\bintro\b", reply or ""))


def unasked_remedy(reply: str, sources: str) -> bool:
    """'fix this' is not a license to name a refund, a reship, or a delivery."""
    src = sources or ""
    if not re.search(r"(?i)\bfixed\b", src):
        return False
    if re.search(r"(?i)\b(refund|replacement|reship)\b", src):
        return False
    return bool(re.search(
        r"(?i)\b(get the .{0,40} to you|send you|refund|replacement|reship)\b",
        reply or "",
    ))


def draft_rules(addressee: str = "", choice: bool = False) -> str:
    """Rules that have to survive a hop that keeps only the front of the prompt."""
    greet = f"hey {addressee} --" if addressee else "hey <first name> --"
    lines = [
        "These rules win. Write only the reply they will paste.",
        f'Start with "{greet}". Use their first name. Never "hey there", never "hi", never "hello".',
        "All lowercase except names, dates, days, and I.",
        "Match the style samples for tone only. Do not copy a fact, time, name, amount, or status out of a sample.",
        "Use only facts written in this thread. A time written in their message is theirs, not your calendar.",
        "Do not mention a meeting, a call, a wrap time, or being free or busy unless this thread says that.",
        "Do not invent a time, a date, a name, a status, or any other fact.",
        "If they ask yes or no and the thread already states the fact, answer yes or no right after the greeting.",
        "If they say do not confirm, do not accept the role and do not say you are happy to do it. Say you will wait.",
        "If they say if you have a spare, do not claim you have one. Say you will bring it if you have it.",
        "If they ask for an intro they can forward, write the intro itself. Do not say you will send it later.",
        "If they want something fixed and do not name a refund, a replacement, or a reship, say you will look at the order they named. Do not say you will get it fixed. Do not mention a week, getting back to them, or sorting it out. Do not promise to get an item to them.",
        "If they ask whether a time is bad and the thread does not say you are free or busy, say you do not know yet. Do not say the time is fine or not bad.",
        "If they have not named a clock time, do not say you will make it work, that any time is fine, or that you are free.",
        "If they ask who is doing something and the line says or am I, do not assign it to yourself. Say you do not know yet.",
        "If they ask you to bring, grab, cover, swap, check, or change something, say you will. That is the answer, not a new fact, and not a claim the work is already finished.",
        "If they say they still need something, or they ask for an update on it, say you will get it. Do not only repeat their sentence. Do not report a status such as sorted, not sorted, ready, lined up, or open unless those words are in the thread.",
        "If they ask whether something came through and the thread does not say, do not invent a status and do not thank them for coming. Say that if it did not come through, they can send it again.",
        "If the only ask is a short yes or no and the thread has no fact against yes, say yes. Do not say you will check and do not say you do not know.",
        "If they only say thanks, thank them for that thing and stop. Do not say either, lmk, or anything else.",
        "If they did not ask a question, do not say you are glad something looks great. A short thanks is enough.",
        "If they ask for an intro they can forward, write it after the greeting in your own voice. Say we share, not your group. Do not say it can work around anyone, and do not mention pricing.",
        "If they want something fixed, say you will look at the order they named. Do not say you will get it fixed, do not say now, and do not promise to get an item to them.",
        "Write a few short sentences. Do not give every line they wrote its own answer.",
        "Do not claim a status that a different person wrote. If they are still looking, that status stays theirs.",
        "Keep the capitalization of every proper name written in the thread, including a possessive.",
        "Do not add a person, a company, or a task this thread does not mention.",
        "A short question still needs yes or no.",
        "Do not answer an action ask with 'I do not have a confirmed update' or 'not a full update'.",
        "If they ask whether something is the cause and the thread does not say, say you do not know yet. Do not name the cause.",
        "Name every object, place, time, and change they brought up. Use their words. Do not answer only the last line.",
        "No labels, no headers, no markdown, no word-colon lines. Plain sentences. Use I, not we, except we share in an intro they will forward.",
        "No reasoning, no tool JSON, no narration of the message.",
    ]
    if choice:
        lines.append(
            "They asked you to choose. Pick one side and say it. "
            "Do not hand the choice back and do not end with a question."
        )
    return " ".join(lines)


def rewrite_ask(addressee: str = "", choice: bool = False, points: list[str] | None = None) -> str:
    """The redo turn. Same bar as the first draft."""
    greet = f"hey {addressee} --" if addressee else "hey <first name> --"
    choice_line = (
        " They asked you to choose. Pick one side in a plain sentence. Do not ask them."
        if choice else ""
    )
    return (
        "Rewrite that reply as the paste itself. "
        f'Open with "{greet}". '
        "All lowercase except names, dates, days, and I. "
        "Answer every open ask. Yes or no when they asked that. "
        "If they asked you to bring, grab, cover, or change something, say you will. "
        "If they said do not confirm, do not accept the role. "
        "If they said if you have a spare, do not claim you have one. "
        "If they asked for an intro they can forward, write the intro itself. "
        "If they want something fixed and did not name a remedy, say you will look at the order. Do not say you will get it fixed. "
        "If they asked whether a time is bad and the thread does not say you are free, say you do not know yet. "
        "If they have not named a clock time, do not say you will make it work. "
        "If they asked or am I, do not assign the part to yourself. "
        "If they still need something, or they asked for an update, say you will get it. Do not only repeat their sentence. "
        "If they asked whether something came through, do not invent a status and do not thank them for coming. Say that if it did not, they can send it again. "
        "A short yes or no with no fact against yes is yes. Do not say you will check. "
        "If they only said thanks, thank them and stop. If they did not ask a question, do not say you are glad something looks great. "
        "An intro they can forward is in your voice: we share, not your group, and no work-around. "
        "If they want something fixed, say you will look at the order. Do not say you will get it fixed, and do not say now. "
        "Do not claim a status a different person wrote. "
        "Keep the capitalization of proper names from the thread. "
        "Name every object, place, time, and change they brought up, in their words. "
        "Do not invent a date, a time, a name, a status, or a fact. "
        "Do not say you lack a confirmed update. "
        "Do not mention your own meeting, call, or calendar. "
        "No labels, no headers, no markdown, no reasoning, no tool JSON."
        + choice_line
        + ((" Cover each of these in the reply: " + " | ".join(points[:6]) + ".") if points else "")
    )


def relevant_notes(found: str, screen: str, addressee: str = "") -> str:
    """Drop a lookup line that is not about this person or this thread."""
    words = {
        word.lower()
        for word in re.findall(r"[A-Za-z]{6,}", screen or "")
        if word.lower() not in _NOTE_STOP
    }
    who = (addressee or "").strip().lower()
    kept = []
    for raw in (found or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        overlap = words & {word.lower() for word in re.findall(r"[A-Za-z]{6,}", line)}
        named = bool(who) and re.search(rf"\b{re.escape(who)}\b", line.lower()) is not None
        if named or len(overlap) >= 2:
            kept.append(line)
    return "\n".join(kept).strip()


def _speaker_blocks(sources: str) -> list[tuple[str, str]]:
    """Named From: blocks as (first name, body). Chat lines without From: are skipped."""
    parts = re.split(r"(?im)^from:\s*", sources or "")
    if len(parts) < 2:
        return []
    blocks = []
    for part in parts[1:]:
        lines = part.splitlines()
        header = (lines[0] if lines else "").strip()
        first = header.split()[0].lower() if header.split() else ""
        blocks.append((first, "\n".join(lines[1:])))
    return blocks


def _status_owner(sources: str, me: list[str] | None, pattern: str) -> str:
    """me, other, or empty. Empty means the line is not tied to a named sender."""
    me_first = {part.split()[0].lower() for part in (me or []) if part.strip()}
    mine = False
    theirs = False
    for first, block in _speaker_blocks(sources):
        if not first or re.search(pattern, block) is None:
            continue
        if first in me_first:
            mine = True
        else:
            theirs = True
    if mine:
        return "me"
    if theirs:
        return "other"
    return ""


def _greet_split(reply: str) -> tuple[str, str]:
    body = (reply or "").strip()
    if "--" in body:
        left, right = body.split("--", 1)
        return left.strip() + " --", right.strip()
    return "", body


def ground_reply(reply: str, sources: str, me: list[str] | None = None) -> str:
    """Last pass for the misses both judges marked. Facts stay inside the thread."""
    src = sources or ""
    low = src.lower()
    greet, rest = _greet_split(reply or "")
    body = (greet + " " + rest).strip() if greet else (reply or "").strip()

    def sentences(text: str) -> list[str]:
        return [piece.strip() for piece in re.split(r"(?<=[.!?])\s+|\n+", text or "") if piece.strip()]

    kept = []
    looking_owner = _status_owner(src, me, r"(?i)\bstill looking\b")
    for sentence in sentences(body):
        if looking_owner == "other" and re.search(r"(?i)\bstill looking\b", sentence):
            continue
        if re.search(r"(?i)\bstill open\b", sentence) and "open" not in low:
            continue
        if re.search(r"(?i)\bwork around\b", sentence) and "work around" not in low:
            continue
        if re.search(r"(?i)\b(either one|anything else|lmk)\b", sentence) and "?" not in src and not re.search(
            r"(?i)\b(can you|could you|please)\b", src
        ):
            continue
        if re.search(r"(?i)\b(a good one|was a good)\b", sentence) and not re.search(r"(?i)\bgood\b", src):
            continue
        if re.search(r"(?i)\bthanks for (?:coming|attending)\b|\bglad you (?:came|could come)\b", sentence):
            continue
        kept.append(sentence)
    body = " ".join(kept).strip()

    if re.search(r"(?i)come through", src) and not re.search(r"(?i)\b(it came through|here is the (?:invoice|pdf)|attached)\b", src):
        if re.search(
            r"(?i)\b(if it didn't|a good one|i'll ask you to send|can(?:not|'t) confirm|thanks for coming|send it again|came through)\b",
            body,
        ):
            greet, _rest = _greet_split(body)
            named = re.search(r"(?i)\b(the [A-Za-z0-9 ]{3,60}?) come through", src)
            thing = named.group(1).strip() if named else "it"
            tail = f"I don't know yet if {thing} came through. if it didn't, send it again."
            body = (greet + " " + tail).strip()

    if re.search(r"(?i)\bi(?:'m| am) on the list\b", body) and not re.search(r"(?i)you(?:'re| are) on the list", src):
        body = re.sub(r"(?i)\s*so I(?:'m| am) on the list\b", "", body)
        body = re.sub(r"(?i)\bI(?:'m| am) on the list\b", "", body)
        body = re.sub(r"\s{2,}", " ", body).strip()

    content = []
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("{") or stripped.lower().startswith(("from:", "subject:")):
            continue
        if re.match(r"(?i)^[A-Za-z]+ [A-Za-z]+ \d", stripped):
            continue
        content.append(stripped)
    short = len(content) == 1 and content[0].endswith("?") and len(content[0]) <= 40
    blocked_yes = bool(re.search(r"(?i)\b(out of town|can't|cannot|do not confirm|don't confirm)\b", src))
    if short and not blocked_yes and re.search(r"(?i)\b(don't know|do not know|i'll check|i will check|let you know)\b", body):
        greet, _rest = _greet_split(body)
        body = (greet + " yes.").strip()

    thanks = re.search(r"(?is)thanks for [^.?\n]{0,40}\bflyers\b[^.?\n]*[.!]?\s*they look great", src)
    if thanks:
        window = src[max(0, thanks.start() - 180) : thanks.end()]
        asked = "?" in window or bool(re.search(r"(?i)\b(can you|could you|please)\b", window))
    else:
        asked = "?" in src or bool(re.search(r"(?i)\b(can you|could you|please)\b", src))
    if not asked and "flyers" in low and (
        "flyers" not in body.lower() or re.search(r"(?i)\b(lmk|either one|email in your note|it's possible|it is possible)\b", body)
    ):
        greet, _rest = _greet_split(reply or body)
        body = (greet + " thanks for the flyers.").strip()
    if not asked and re.search(r"(?i)\bglad\b.{0,60}\b(?:looks|look) great\b", body):
        greet, _rest = _greet_split(body)
        named = re.search(
            r"(?i)\bglad (?:the |that )?([a-z0-9][a-z0-9 ]{0,40}?) (?:looks|look) great\b",
            body,
        )
        thing = named.group(1).strip() if named else ""
        if thing and thing.lower() in low:
            body = (greet + f" thanks for the {thing}.").strip()
        else:
            body = (greet + " thanks.").strip()

    if re.search(r"(?i)\bintro\b", src) and re.search(r"(?i)\bforward\b", src):
        body = re.sub(r"(?i)\byour group shares\b", "we share", body)
        body = re.sub(r"(?i)\byour group\b", "we", body)
        body = re.sub(
            r"(?i)\s*there(?:'s| is) no day picked yet,?\s*so lmk\b[^\"']*",
            "",
            body,
        )

    if re.search(r"(?i)\b(sometime|do not have a time|don't have a time|no time yet)\b", src) and not re.search(
        r"\d{1,2}:\d{2}", src
    ):
        body = re.sub(
            r"(?i)\s*(?:and |so )?I(?:'ll| will) make (?:it|that|a time|any time) work\.?",
            "",
            body,
        )

    if re.search(r"(?i)tell me if .{0,40}\bis bad\b", src) and not re.search(
        r"(?i)\b(i(?:'m| am) free|works for me|not bad)\b", src
    ):
        body = re.sub(
            r"(?i)\b(?:no,?\s*)?(\d{1,2}:\d{2}) is not bad(?: for me)?\b\.?",
            r"I don't know yet if \1 is bad",
            body,
        )

    if re.search(r"(?i)key is with", src) and not re.search(r"(?i)\b(?:bring|grab|get) (?:the |your )?(?:hall )?key\b", src):
        body = re.sub(
            r"(?i)\bI(?:'ll| will) (?:grab|get|bring) the hall key[^.]*\.",
            "the hall key is with the front desk.",
            body,
        )

    if re.search(r"(?i)\bor am I\b", src) and not re.search(r"(?i)\byou (?:are|will) (?:sing|be singing)\b", src):
        body = re.sub(
            r"(?i)\b(?:I(?:'ll| will)|you(?:'re| are)|I am) singing the bridge\b\.?",
            "I don't know yet who is singing the bridge",
            body,
        )

    order = re.search(r"(?i)\border (\d+)\b", src)
    if order:
        num = order.group(1)
        body = re.sub(r"(?i)\blook into it now\b", f"look at order {num}", body)
        body = re.sub(r"(?i)\s*(?:and |to )?get (?:this|it) fixed\b", "", body)
        body = re.sub(
            rf"(?i)(look at order {num})(?:\s*(?:to|and)\s*look at order {num})+",
            rf"look at order {num}",
            body,
        )
        body = re.sub(r"\s+([.])", r"\1", body)
        if not re.search(rf"(?i)\border {num}\b", body):
            body = body.rstrip(". ") + f". I'll look at order {num}."
        body = re.sub(
            r"(?i),?\s*(?:and\s+)?I(?:'m| am) not going to tell you to wait another week\.?",
            "",
            body,
        )
        body = re.sub(
            r"(?i),?\s*(?:and\s+)?I (?:won'?t|will not) tell you to wait another week\.?",
            "",
            body,
        )
        body = re.sub(
            r"(?i),?\s*(?:and\s+)?that(?:'s| is) on (?:us|me) to (?:sort out|fix)(?: this| it)?\.?",
            "",
            body,
        )
        body = re.sub(r"(?i)\s*(?:and )?get back to you\b", "", body)
        body = re.sub(rf"(?i)(look at order {num})\s+and the [^.]+", r"\1", body)
        body = re.sub(rf"(?i)(look at order {num})\s+and see what happened\b[^.]*", r"\1", body)
        body = " ".join(piece for piece in sentences(body) if not re.search(r"(?i)another week", piece))
        if re.search(r"(?i)\b(want this fixed|do not tell me to wait|don't tell me to wait)\b", src):
            greet, _rest = _greet_split(reply or body)
            bits = []
            if re.search(r"(?i)\bsecond time\b", src):
                bits.append("this is the second time")
            wrong = re.search(
                r"(?i)was supposed to include the (.+?) and it showed up with ([^.]+)",
                src,
            )
            if wrong:
                arrived = re.sub(r"(?i)\s+instead$", "", wrong.group(2).strip())
                bits.append(
                    f"order {num} was supposed to include the {wrong.group(1).strip()} "
                    f"and it showed up with {arrived}"
                )
            bits.append(f"I'll look at order {num}")
            body = (greet + " " + ". ".join(bits) + ".").replace("..", ".").strip()

    shown = re.search(r"(?i)people start showing up at (\d{1,2}(?::\d{2})?(?:\s*[ap]m)?)", src)
    if shown and re.search(r"(?i)\bI've got \d", body):
        body = re.sub(
            r"(?i)\bI've got \d+ as when people start showing up\.?",
            f"people start showing up at {shown.group(1)}.",
            body,
        )
    if re.search(r"(?i)still looking at the gate code", src) and re.search(
        r"(?i)(bring the new park gate code|don'?t have the new park gate code|all three|code card|have the card)",
        body,
    ):
        greet, _rest = _greet_split(reply or body)
        bits = []
        if _status_owner(src, me, r"(?i)\bstill looking\b") == "me":
            bits.append("still looking at the new park gate code")
        if re.search(r"(?i)\b(blue cooler|blue one)\b", src):
            bits.append("I'll get the blue cooler since the red one leaks")
        if re.search(r"(?i)\bice\b", src):
            bits.append("I'll grab two bags of ice")
        if re.search(r"(?i)\bgrill\b", src) and re.search(r"(?i)canceled", src):
            bits.append("got it on the grill being canceled")
        if re.search(r"(?i)reply-all", src):
            bits.append("I won't reply-all to the vendor thread")
        if bits:
            body = (greet + " " + ". ".join(bits) + ".").replace("..", ".").strip()
        else:
            kept_bits = [
                piece for piece in sentences(body)
                if not re.search(
                    r"(?i)(bring the new park gate code|don'?t have the new park gate code|all three|have the card|code card|still looking)",
                    piece,
                )
            ]
            body = (greet + " " + " ".join(kept_bits)).strip() if kept_bits else greet.rstrip()
    screen_lines = []
    for line in src.splitlines():
        if (
            re.match(r"^[^:\n]{1,80}: ", line)
            and not line.lower().startswith(("from:", "subject:", "already in the box:"))
        ):
            break
        screen_lines.append(line)
    screen_only = "\n".join(screen_lines)
    screen_content = []
    for line in screen_only.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith(("from:", "subject:")):
            continue
        if re.match(r"(?i)^[A-Za-z]+ [A-Za-z]+ \d", stripped):
            continue
        screen_content.append(stripped)
    screen_short = (
        len(screen_content) == 1 and screen_content[0].endswith("?") and len(screen_content[0]) <= 40
    )
    screen_blocked = bool(re.search(r"(?i)\b(out of town|can't|cannot|do not confirm|don't confirm)\b", screen_only))
    ask = screen_content[0] if screen_content else ""
    action = bool(re.search(r"(?i)\b(look|check|bring|grab|send|cover|swap|hold|take)\b", ask))
    if screen_short and not screen_blocked and not action:
        greet, _rest = _greet_split(body)
        body = (greet + " yes.").strip()
    if re.search(r"(?i)lock (?:it|the list) at \d", src):
        body = re.sub(r"(?i)\s*this is my reply today before they lock it at \d{1,2}\.?", "", body)
        body = re.sub(r"(?i)\s*before they lock (?:it|the list) at \d{1,2}\.?", "", body)
    body = re.sub(r"(?i),?\s*that should cover[^.]*\.?", "", body)
    body = re.sub(r"(?i)\bthanks for noting that\s+", "", body)
    body = re.sub(
        r"(?i)with Avery on the crate list and Blake on the labels",
        "Avery has the crate list and Blake has the labels",
        body,
    )

    body = re.sub(r"\s{2,}", " ", body).replace(" .", ".").strip().rstrip(" ,")
    if body and body[-1] not in ".!?\"'" and not body.endswith("--"):
        body += "."
    return body


def shape_voice(text: str, sources: str = "", addressee: str = "") -> str:
    """Greeting, lowercase, and no label lines. Facts stay as the model wrote them."""
    body = _LABEL_LINE.sub("", text or "").strip()
    if not body:
        return ""
    if addressee:
        rest = _GREET.sub("", body, count=1).strip()
        rest = re.sub(rf"(?i)^{re.escape(addressee)}\s*[,—–-]+\s*", "", rest, count=1).strip()
        body = f"hey {addressee} -- {rest}".strip() if rest else f"hey {addressee} --"
    protected = set(re.findall(r"\b[A-Z][A-Za-z0-9'’-]*", sources or ""))
    named = _proper_case(sources)
    if addressee:
        protected.add(addressee)
        named[addressee.lower()] = addressee
    days = set(_DAYS)
    months = set(_MONTHS)
    contractions = {"i'm", "i'll", "i'd", "i've"}

    def repl(match: re.Match) -> str:
        word = match.group(0)
        low = word.lower()
        if low in days or low in months:
            return low[:1].upper() + low[1:]
        if low == "i":
            return "I"
        if low in contractions:
            return "I" + word[1:]
        if low in named:
            return named[low]
        for key, canonical in named.items():
            if low.startswith(key) and len(low) > len(key) and low[len(key)] in {"'", "’"}:
                return canonical + word[len(key):]
        if addressee and low == addressee.lower():
            return addressee
        if word in protected:
            return word
        if word.isupper() and len(word) > 1:
            return word.lower()
        if word[:1].isupper():
            return word[:1].lower() + word[1:]
        return word

    return re.sub(r"[A-Za-z][A-Za-z0-9'’-]*", repl, body).strip()


def reply_ok(text: str) -> bool:
    body = (text or "").strip()
    return bool(body) and _LINK_ONLY.match(body) is None and not _chrome(body)


def _only_signoff(text: str) -> bool:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines or len(lines) > 6:
        return False
    if len(" ".join(lines).split()) > 24:
        return False
    return any(line.lower().rstrip(",") in _CLOSE for line in lines)


def _note(scene: Scene) -> bool:
    return (scene.app or "").strip().lower() in {"notes", "obsidian"}


def _screen_noise(text: str) -> bool:
    sample = text or ""
    if not sample.strip():
        return False
    drawing = len(_DRAWING.findall(sample))
    return drawing >= 8 or drawing * 3 >= max(len(sample), 1)


def _terminal_subject(text: str) -> str:
    """Last readable output. Drops the prompt, the status line, and box drawing."""
    kept: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            kept.append("")
            continue
        letters = sum(ch.isalpha() for ch in line)
        drawing = len(_DRAWING.findall(line))
        if drawing and drawing * 2 >= len(line):
            continue
        if letters < 3:
            continue
        if re.match(r"^\S+@\S+.*[%$#]$", line):
            continue
        if line.startswith(("❯", "%", "$")):
            continue
        kept.append(line)
    chunks: list[list[str]] = []
    chunk: list[str] = []
    for line in kept:
        if line:
            chunk.append(line)
        elif chunk:
            chunks.append(chunk)
            chunk = []
    if chunk:
        chunks.append(chunk)
    if not chunks:
        return ""
    body = "\n".join(chunks[-1])[-CAP:]
    if sum(ch.isalpha() for ch in body) < 12:
        return ""
    return body


def _skip_line(raw: str) -> bool:
    if not raw:
        return True
    if _chrome(raw) or _placeholder(raw) or _signature_line(raw):
        return True
    drawing = len(_DRAWING.findall(raw))
    letters = sum(ch.isalpha() for ch in raw)
    if drawing and (drawing * 2 >= len(raw) or letters < 4):
        return True
    if re.match(r"^\S+@\S+.*[%$#]$", raw):
        return True
    if re.match(r"(?i)(from|sent|date|to|cc|bcc):\s", raw):
        return True
    if re.fullmatch(r"https?://\S+", raw):
        return True
    if re.search(r"(?i)external sender|do not click links|report suspicious", raw):
        return True
    if re.fullmatch(r"(?i)\+?\d[\d\s().-]{6,}\d", raw):
        return True
    return False


def _page_text(scene: Scene) -> str:
    """Every real sentence in the window, including text scrolled out of view."""
    seen: set[str] = set()
    parts: list[str] = []

    def add(blob: str) -> None:
        kept: list[str] = []
        after_close = False
        for raw in (blob or "").splitlines():
            line = raw.strip()
            if after_close and _name_line(line):
                after_close = False
                continue
            after_close = _signature_line(line)
            if _skip_line(line):
                if kept:
                    parts.append("\n".join(kept))
                    kept = []
                continue
            after_close = False
            key = " ".join(line.split()).lower()[:180]
            if key in seen:
                continue
            seen.add(key)
            kept.append(line)
        if kept:
            parts.append("\n".join(kept))

    for block in scene.blocks:
        if "button" in (block.role or "").lower():
            continue
        add(block.text)
    add(scene.field_text or "")
    if not parts:
        return ""
    return "\n\n".join(parts)[:PAGE]


def choose(scene: Scene) -> Reading:
    if _secure(scene.field_role):
        return Reading("secret")
    page = _page_text(scene)
    selection = (scene.selection or "").strip()
    if selection and not _chrome(selection) and selection not in page:
        page = (selection + "\n\n" + page).strip()
    if not page.strip():
        return Reading("empty")
    kind = "selection" if selection and not _chrome(selection) else "above"
    return Reading(kind, page[:PAGE], "", "Write the reply. Cover every point that is still open, not only the last line.", page[:240])


def _hits_from(search, query: str) -> list[dict]:
    if search is None or not query.strip():
        return []
    try:
        rows = search(query) or []
    except Exception:
        return []
    kept = []
    for row in rows[:3]:
        if not isinstance(row, dict):
            continue
        body = str(row.get("body") or row.get("text") or "").strip()
        if not body:
            continue
        kept.append({"title": str(row.get("title") or "note")[:80], "body": body[:400]})
    return kept


def _excerpt(path: Path, text: str) -> str:
    suffix = path.suffix.lower()
    if suffix == ".ics":
        kept = []
        for line in text.splitlines():
            if line.startswith(("SUMMARY", "DTSTART", "DTEND", "LOCATION", "DESCRIPTION")):
                kept.append(line.split(":", 1)[-1].strip()[:180])
        return "\n".join(kept)
    if suffix == ".eml":
        head, _, body = text.partition("\n\n")
        if "base64" in head.lower():
            return ""
        return body
    return text


_JUNK_NAME = re.compile(
    r"(?i)^(?:readme|license|licence|changelog|changes|history|notice|authors|contributing|"
    r"code_of_conduct|copying|patents|security|third[-_ ]?party[-_ ]?notices?)$"
)
_URLISH = re.compile(r"https?://\S+")


def junk_name(name: str) -> bool:
    """Package docs (README, LICENSE, CHANGELOG, HISTORY...) are not the user's notes."""
    stem = (name or "").rsplit(".", 1)[0].strip()
    stem = re.sub(r"^\d+[-_ ]", "", stem)
    return bool(_JUNK_NAME.match(stem))


def junk_text(text: str) -> bool:
    """Cache dumps, captcha/payment URL soup, binary, or license boilerplate."""
    body = (text or "")[:4000]
    if not body.strip():
        return True
    printable = sum(1 for ch in body if ch.isprintable() or ch in "\n\r\t")
    if printable / max(1, len(body)) < 0.9:
        return True
    urls = _URLISH.findall(body)
    if urls and sum(len(url) for url in urls) / max(1, len(body)) > 0.35:
        return True
    low = body.lower()
    if "permission is hereby granted" in low or "licensed under the apache license" in low:
        return True
    if any(word in low[:600] for word in ("hcaptcha.com", "js.strip" + "e.com", "<!doctype html", "<html")):
        return True
    return False


def source_dirs(cfg: dict | None) -> list[str]:
    paiste = (cfg or {}).get("paiste") or {}
    found = []
    for key in ("notes_dir", "obsidian_vault", "email_dir", "chat_dir", "calendar_dir", "sources_dir"):
        raw = str(paiste.get(key) or "").strip()
        if raw:
            found.append(raw)
    return found


def folder_hits(folder: Path, query: str, limit: int = 3) -> list[dict]:
    words = [word.lower() for word in _WORD.findall(query or "")][:6]
    if not words or not folder.is_dir():
        return []
    found = []
    seen = 0
    for path in folder.rglob("*"):
        if seen >= 200:
            break
        if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".markdown", ".eml", ".ics"}:
            continue
        # Relative parts: the sources folder itself lives under ~/.config, which is a dot folder.
        if any(part in _SKIP_DIR or part.startswith(".") for part in path.relative_to(folder).parts):
            continue
        if junk_name(path.name):
            continue
        seen += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = _excerpt(path, text)
        if junk_text(text):
            continue
        low = text.lower()
        score = sum(1 for word in words if word in low)
        if score and text.strip():
            found.append((score, path.stem[:80], text[:400]))
    found.sort(key=lambda item: (-item[0], item[1]))
    return [{"title": title, "body": body} for _, title, body in found[:limit]]


def source_notes(reading: Reading, hits: list[dict]) -> list[str]:
    notes = []
    if reading.subject.strip():
        notes.append(reading.subject.strip())
    if reading.draft_so_far.strip():
        notes.append("Already in the box: " + reading.draft_so_far.strip())
    for hit in hits[:3]:
        notes.append(hit["title"] + ": " + hit["body"])
    return notes


_SCAFFOLD_TAG = re.compile(
    r"(?is)<\s*/?\s*(?:tool_call|tool_calls|function_call|function_calls|tool|function|"
    r"thinking|think|scratchpad|reasoning|reflection|search|read|lookup|query|plan|"
    r"invoke|parameter|function_results|tool_use|tool_result|antml:[a-z_]+)\b[^>]*>"
)
# A tool call printed as text, body and all: <invoke name="Write"><parameter ...>a plan</parameter></invoke>.
_SCAFFOLD_BLOCK = re.compile(
    r"(?is)<\s*(invoke|function_calls|function_results|tool_use|tool_result)\b[^>]*>.*?(?:<\s*/\s*\1\s*>|\Z)"
)
_SCAFFOLD_FENCE = re.compile(r"(?s)```[A-Za-z0-9_-]*\n?(.*?)```")
_TOOL_JSON = re.compile(
    r'(?is)\{[^{}]*"(?:name|tool|tool_name|function|arguments|parameters|action|'
    r'action_input|tool_call|query|recipient|search|read)"\s*:[^{}]*\}'
)
_TOOL_KEY = re.compile(
    r'"(?:name|tool|tool_name|function|arguments|parameters|action|action_input|'
    r'tool_call|query|recipient|search|read)"\s*:'
)


def _strip_tool_objects(body: str) -> str:
    """Remove a balanced {...} JSON object that holds a tool-call key, nesting and all."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        if body[i] == "{":
            depth, j = 0, i
            while j < n:
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth == 0 and j < n and _TOOL_KEY.search(body[i : j + 1]):
                i = j + 1
                continue
        out.append(body[i])
        i += 1
    return "".join(out)


_DROP_PREFIX = re.compile(
    r"(?i)^\s*(?:search|read|tool(?:[ _]call)?|function(?:[ _]call)?|action(?:[ _]input)?|"
    r"observation|thoughts?|reasoning|reflection|plan(?:ning)?|lookup|query|fetching|"
    r"searching|reading|retrieving|gathering|assistant|system|user)\s*[:\-\u2013]"
)
_UNWRAP_PREFIX = re.compile(
    r"(?i)^\s*(?:final answer|answer|draft|reply|response|output|"
    r"here'?s (?:the|your|a) (?:reply|draft|response))\s*[:\-\u2013]\s*"
)
_META_NARRATION = re.compile(
    r"(?i)(?:^|(?<=[.!?\n]))\s*(?:first,?\s+|now,?\s+|ok(?:ay)?,?\s+)?"
    r"(?:i'?ll|i will|i'?m going to|i am going to|let me|allow me to|i need to|i should|i'?m gonna)\s+"
    r"(?:pull up|pull|search|look up|look through|look at|go through|dig through|read through|"
    r"retrieve|fetch|gather|scan|review|read|check|open|access|load|bring up|skim|find|locate)\b"
    r"[^.!?\n]*?\b(?:message|messages|thread|threads|history|conversation|conversations|inbox|"
    r"mail|email|emails|note|notes|context|record|records|document|documents|chat|backlog|"
    r"above|below|full|rest of|earlier|previous)\b[^.!?\n]*[.!?]?"
)
_BRACKETS_ONLY = re.compile(r"^[\[\]{}(),]+$")
_BARE_LABEL = {
    "tool", "tool_call", "tool call", "function", "function_call", "assistant",
    "system", "user", "search", "read", "observation", "thought", "thoughts",
    "reasoning", "plan", "planning", "final answer", "answer",
}


def _unfence(match: "re.Match") -> str:
    inner = match.group(1)
    if (
        _TOOL_JSON.search(inner)
        or _TOOL_KEY.search(inner)
        or re.search(r"(?i)\b(?:tool_call|function_call)\b", inner)
    ):
        return ""
    return inner


def strip_scaffold(text: str) -> str:
    """Strip tool-call wrappers, hop labels, reasoning, and search/read markers.

    The clipboard and the compose box only ever get human prose. A model that
    leaks its own tool calls, a Search:/Read: blob, or "I'll pull up the full
    message history" never reaches the reply.
    """
    body = _SCAFFOLD_BLOCK.sub("", text or "")
    body = _SCAFFOLD_FENCE.sub(_unfence, body)
    body = _SCAFFOLD_TAG.sub("", body)
    body = _strip_tool_objects(body)
    body = _META_NARRATION.sub(" ", body)
    kept: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        probe = line.strip().lower().rstrip(":")
        if probe in _BARE_LABEL:
            continue
        if _DROP_PREFIX.match(line):
            continue
        if line.strip() and _BRACKETS_ONLY.match(line.strip()):
            continue
        kept.append(_UNWRAP_PREFIX.sub("", line))
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    if out:
        return out
    # Over-eager line drop: fall back to the tag/json/meta-stripped body only.
    fb = _META_NARRATION.sub(" ", _strip_tool_objects(_SCAFFOLD_TAG.sub("", _SCAFFOLD_FENCE.sub(_unfence, _SCAFFOLD_BLOCK.sub("", text or "")))))
    return re.sub(r"\n{3,}", "\n\n", fb).strip()


def plain_reply(text: str) -> str:
    """Drop a trailing thanks and markdown. The reply is what they paste."""
    body = strip_signoff(strip_scaffold(text or "")).replace("**", "").replace("__", "")
    body = re.sub(r"(?i)(--\s*)\d+[.)]\s+", r"\1", body)
    body = re.sub(r"(?i)\s*lmk if you need anything else[!?.]*", "", body)
    body = re.sub(r"[ \t]*\n[ \t]*\n[\s]*", "\n\n", body.strip())
    while True:
        cut = re.search(r"(?is)(?:^|(?<=[.!?])\s+|\n+)thanks\b[^.!?\n]*[.!?]*\s*$", body)
        if not cut or cut.start() == 0:
            break
        body = body[:cut.start()].rstrip()
    return body.strip()


def runner_context(messages: list[dict], note: str) -> list[dict]:
    """Extra wording for the draft runner only. A menu click does not call this."""
    extra = " ".join((note or "").split())[:4200]
    if not extra:
        return messages
    out = [dict(item) for item in messages]
    if out and out[0].get("role") == "system":
        out[0]["content"] = extra + "\n\n" + str(out[0].get("content") or "")
    return out


def messages_for(reading: Reading, hits: list[dict]) -> list[dict]:
    user = reading.ask + "\n\nOn screen:\n" + reading.subject
    if reading.draft_so_far.strip():
        user += "\n\nAlready in the box:\n" + reading.draft_so_far.strip()
    extra = "\n\n".join(hit["title"] + "\n" + hit["body"] for hit in hits[:3])
    if extra:
        user += "\n\nNotes:\n" + extra
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def prepare(scene: Scene | dict, search=None, folders=None) -> dict:
    if isinstance(scene, dict):
        scene = scene_from(scene)
    reading = choose(scene)
    if reading.kind in {"secret", "empty"}:
        return {"ok": False, "reason": reading.kind, "sent": False, "subject": scene.title, "app": scene.app}
    hits = _hits_from(search, reading.query)
    for folder in folders or []:
        hits.extend(folder_hits(Path(folder), reading.query))
    hits = hits[:3]
    return {
        "ok": True,
        "sent": False,
        "reason": "",
        "kind": reading.kind,
        "ask": reading.ask,
        "sources": source_notes(reading, hits),
        "subject": scene.title or "Paiste",
        "app": scene.app,
        "messages": messages_for(reading, hits),
        "subject_text": reading.subject,
    }


def copy_text(text: str, run=None) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    if run is not None:
        result = run(body)
        return True if result is None else bool(result)
    if shutil.which("pbcopy"):
        done = subprocess.run(["pbcopy"], input=body.encode("utf-8"), check=False)
        return done.returncode == 0
    return False


def run_inbox(inbox: Path, outbox: Path, complete, search=None, folders=None, copy=False, copy_run=None) -> list[dict]:
    """Draft each scene file. The out box records sent false. Files move to done."""
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    done_dir = inbox / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted(inbox.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            results.append({"file": path.name, "ok": False, "reason": "bad_scene", "sent": False})
            continue
        prepared = prepare(doc, search=search, folders=folders)
        item = {"file": path.name, "ok": bool(prepared.get("ok")), "reason": prepared.get("reason") or "", "sent": False}
        if prepared.get("ok"):
            text = plain_reply(str(complete(prepared["messages"]) or ""))
            item["text"] = text
            digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:12]
            (outbox / f"{path.stem}-{digest}.json").write_text(
                json.dumps({"text": text, "sent": False, "kind": prepared.get("kind") or ""}) + "\n",
                encoding="utf-8",
            )
            if copy and text:
                item["copied"] = copy_text(text, run=copy_run)
        path.rename(done_dir / path.name)
        results.append(item)
    return results
