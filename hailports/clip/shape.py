"""The shape of a reply: length, greeting, list, ask, sign-off.

Email replies are 80 to 180 words, scaled to the message they answer; chat is shorter.
They open with "hey <First>," (a comma, not a dash), answer the question first with at
most one or two key numbers, use a short numbered list for steps or a fix, end with a
one-line ask or next step, and on email sign "best,\\n<First>" (the mail app adds the
signature block under it). Chat and Teams replies have no sign-off.

Pure functions; nothing here calls a model.
"""

from __future__ import annotations

import re

_CHAT_APPS = ("teams", "slack", "zoom", "chatter", "messages", "whatsapp", "discord", "telegram", "signal", "sand")
_MAIL_APPS = ("outlook", "mail", "gmail", "superhuman", "spark", "airmail", "thunderbird")
_CLOSE = {"best", "thanks", "thank you", "regards", "cheers", "best regards", "kind regards", "thx", "ty"}


def surface(doc: dict | None) -> str:
    """'email' or 'chat' from the app and window title."""
    doc = doc or {}
    blob = (str(doc.get("app") or "") + " " + str(doc.get("title") or "") + " " + str(doc.get("bundle") or "")).lower()
    if any(word in blob for word in ("teams", "slack", "zoom", "chatter", "whatsapp", "discord", "telegram", "signal")):
        return "chat"
    if any(word in blob for word in _MAIL_APPS):
        return "email"
    if any(word in blob for word in _CHAT_APPS):
        return "chat"
    text = str(doc.get("field_text") or "")
    if re.search(r"(?im)^[ \t>]*From:[^\n]+\n[ \t>]*(?:Sent|Date|To):", text):
        return "email"
    return "chat"


def words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def newest_message(text: str) -> str:
    """The newest message from someone else in a pasted mail thread (the first quoted block)."""
    body = text or ""
    head = re.search(r"(?im)^[ \t>]*From:[^\n]+\n", body)
    if not head:
        return body[-1500:]
    rest = body[head.end():]
    nxt = re.search(r"(?im)^[ \t>]*From:[^\n]+\n[ \t>]*(?:Sent|Date|To):", rest)
    return rest[: nxt.start()] if nxt else rest[:4000]


def word_cap(kind: str, incoming: int) -> tuple[int, int]:
    """(target, hard cap) in words, scaled to the message being answered."""
    if kind == "email":
        target = max(80, min(180, int(incoming * 0.6) + 40))
        return target, 180
    target = max(15, min(60, int(incoming * 0.5) + 10))
    return target, 70


def first_name(name: str) -> str:
    """'Hale, Morgan j' -> 'Morgan'; 'Morgan Hale' -> 'Morgan'."""
    raw = (name or "").strip().strip('"')
    raw = re.sub(r"<[^>]*>", "", raw).strip()
    if "," in raw:
        raw = raw.split(",", 1)[1].strip()
    part = raw.split()[0] if raw.split() else ""
    return part[:1].upper() + part[1:] if part else ""


def rules(kind: str, target: int, cap: int, me_first: str = "") -> str:
    sign = ("End with a one-line ask or next step. Do not write the sign-off or a name; "
            '"best," and my first name are added after, and the mail app adds the signature block.'
            if kind == "email" else
            "End with a one-line ask or next step. No sign-off, no name at the end.")
    return f"""REPLY SHAPE (these win over any style sample or older rule):
- About {target} words, never more than {cap}. Scale to their message: a short note gets a short reply.
- Open with "hey <their first name>," (a comma, not a dash), then the point.
- Answer their direct question first, in one or two short sentences.
- Use at most one or two key numbers. No per-row, per-country or per-account dumps, no totals tables, unless they asked for that breakdown.
- When there are steps or a fix, write a short line like "fix is:" and then a numbered list of 2 to 4 short items ("1. ...").
- No jargon or internal ids unless they used them. Plain words.
- {sign}
- Lowercase, casual, direct, like the voice sample: lowercase except names, acronyms, ids, and I."""


_GREET = re.compile(r"(?i)^\s*(hey|hi|hello)\s+([A-Za-z][\w'.-]*)\s*(?:--|—|–|-|,|:|!)?\s*")


def fix_greeting(text: str, kind: str = "email") -> str:
    """'hey Morgan --' / 'Hi Morgan,' -> 'hey Morgan,'. Email puts the body on its own paragraph."""
    body = (text or "").lstrip()
    m = _GREET.match(body)
    if not m:
        return body
    name = m.group(2)
    rest = body[m.end():].lstrip(" \t").lstrip("\n")
    if not rest:
        return f"hey {name},"
    return f"hey {name},\n\n{rest}" if kind == "email" else f"hey {name}, {rest}"


def strip_close(text: str, me: list[str] | None = None) -> str:
    lines = (text or "").rstrip().split("\n")
    names = {m.strip().lower() for m in (me or []) if m.strip()} | {m.split()[0].lower() for m in (me or []) if m.split()}
    changed = True
    while lines and changed:
        changed = False
        last = lines[-1].strip().rstrip(",.!-– ").lower()
        if not last:
            lines.pop()
            changed = True
        elif last in _CLOSE or last in names:
            lines.pop()
            changed = True
    return "\n".join(lines).rstrip()


def finish(text: str, kind: str, me: list[str] | None = None) -> str:
    """Greeting with a comma, sign-off on email only."""
    body = fix_greeting(text, kind)
    body = strip_close(body, me)
    if kind == "email":
        first = (me or [""])[0].split()[0] if (me and (me[0] or "").split()) else ""
        if first:
            body = body + "\n\nbest,\n" + first
    return body.strip()


def issues(text: str, kind: str, cap: int) -> list[str]:
    out = []
    n = words(strip_close(text))
    if n > cap:
        out.append(f"too long: {n} words. Rewrite it in at most {cap} words: answer first, one or two key numbers, "
                   "a short numbered list if there is a fix, one-line ask. Cut data dumps and background.")
    return out


def load_examples(paiste: dict | None, limit: int = 2400) -> str:
    """Real sent replies, kept on this Mac only (never in a repo): paiste.examples_file or
    ~/.config/paiste/examples.md. Separate examples with a line of three dashes."""
    from pathlib import Path
    raw = str((paiste or {}).get("examples_file") or "").strip()
    path = Path(raw).expanduser() if raw else Path.home() / ".config" / "paiste" / "examples.md"
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    picked, size = [], 0
    for chunk in re.split(r"(?m)^\s*---+\s*$", text):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#") and "\n" not in chunk:
            continue
        if size + len(chunk) > limit:
            break
        picked.append(chunk)
        size += len(chunk)
    return "\n---\n".join(picked[:4])
