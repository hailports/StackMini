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
    """Drop sentences that promise work or invent a numbered procedure."""
    chunks = re.split(r"(?<=[.!?])\s+|\n+", (text or "").strip())
    kept = []
    for chunk in chunks:
        piece = chunk.strip()
        if not piece:
            continue
        if _PROMISE.search(piece):
            continue
        if _STEPS.search(piece) and not _STEPS.search(sources or ""):
            continue
        kept.append(piece)
    return " ".join(kept).strip()


def overreach(text: str, sources: str = "") -> bool:
    """A promise, or a numbered procedure the source did not contain."""
    body = text or ""
    if _PROMISE.search(body):
        return True
    return bool(_STEPS.search(body) and not _STEPS.search(sources or ""))


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
        if any(part in _SKIP_DIR or part.startswith(".") for part in path.parts):
            continue
        seen += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = _excerpt(path, text)
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
    r"antml:[a-z_]+)\b[^>]*>"
)
_SCAFFOLD_FENCE = re.compile(r"(?s)```[A-Za-z0-9_-]*\n?(.*?)```")
_TOOL_JSON = re.compile(
    r'(?is)\{[^{}]*"(?:name|tool|tool_name|function|arguments|parameters|action|'
    r'action_input|tool_call|query|recipient|search|read)"\s*:[^{}]*\}'
)
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
        or re.search(r'(?i)"(?:name|arguments|tool|function|query)"\s*:', inner)
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
    body = text or ""
    body = _SCAFFOLD_FENCE.sub(_unfence, body)
    body = _SCAFFOLD_TAG.sub("", body)
    body = _TOOL_JSON.sub("", body)
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
    fb = _META_NARRATION.sub(" ", _TOOL_JSON.sub("", _SCAFFOLD_TAG.sub("", _SCAFFOLD_FENCE.sub(_unfence, text or ""))))
    return re.sub(r"\n{3,}", "\n\n", fb).strip()


def plain_reply(text: str) -> str:
    """Drop a trailing thanks and markdown. The reply is what they paste."""
    body = strip_signoff(strip_scaffold(text or "")).replace("**", "").replace("__", "")
    body = re.sub(r"(?i)(--\s*)\d+[.)]\s+", r"\1", body)
    body = re.sub(r"(?i)\s*lmk if you need anything else[!?.]*", "", body)
    parts = re.split(r"(?<=[.!?])\s+", body.strip())
    while parts and re.match(r"(?i)^thanks\b", parts[-1].strip()):
        parts.pop()
    return " ".join(parts).strip()


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
