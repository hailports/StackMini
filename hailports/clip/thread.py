"""Read an email chain the way a person does: who wrote what, in which order, and what is still open.

The window text of a reply holds the draft so far, then every earlier message under a
From/Sent/To/Cc/Subject header (Outlook) or an "On <date>, <name> wrote:" line (Mail, Gmail).
The newest message from someone else is the one to answer. Asks that were already
handled by a later message from me are context, not work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_FIELD = r"(?:From|Sent|Date|To|Cc|CC|Bcc|Subject)"
_OUTLOOK = re.compile(
    r"(?im)^[ \t>]*From:[ \t]*(?P<from>[^\n]+)\n"
    r"(?P<rest>(?:[ \t>]*(?:Sent|Date|To|Cc|CC|Bcc|Subject):[^\n]*\n(?:[ \t]+[^\n]*\n)?){1,6})"
)
_WROTE = re.compile(r"(?im)^[ \t>]*On (?P<date>[^\n]{6,80}?),? (?P<from>[^\n,]{2,80}?) (?:<[^>\n]+>)? ?wrote:[ \t]*$")
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_SIG_START = re.compile(
    r"(?im)^\s*(?:thanks!?|thank you!?|thx!?|best,?|best regards,?|regards,?|cheers,?|sincerely,?|--)\s*$"
)
_QUESTION = re.compile(r"[^.!?\n]*\?")
_ASK_VERB = re.compile(
    r"(?i)\b(?:can you|could you|would you|will you|please|pls|let me know|send|confirm|check|"
    r"does (?:your|the|it)|is (?:it|this|that|there)|are (?:we|you|they)|do (?:we|you)|what|when|where|which|how|why)\b"
)
_ADDRESSED = re.compile(r"^\s*@?([A-Z][a-z]+)\s*(?:[-–—,:]|--)")


@dataclass
class Message:
    sender: str = ""
    email: str = ""
    date: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    mine: bool = False

    @property
    def first(self) -> str:
        return first_name(self.sender)


@dataclass
class Thread:
    draft: str = ""
    messages: list[Message] = field(default_factory=list)  # newest first
    me: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.messages)

    def newest_other(self) -> Message | None:
        for msg in self.messages:
            if not msg.mine:
                return msg
        return None

    def open_messages(self) -> list[Message]:
        """Messages from others that came after my last message, newest first."""
        out = []
        for msg in self.messages:
            if msg.mine:
                if holding(msg):
                    continue  # "sure, looking now" does not answer anything
                break
            out.append(msg)
        return out

    def settled(self) -> list[Message]:
        opened = self.open_messages()
        return [msg for msg in self.messages if msg not in opened and not (msg.mine and holding(msg))]


_HOLD = re.compile(r"(?i)\b(?:looking (?:now|into it)|checking|on it|will (?:check|look|get back|follow up)|let me (?:check|look)|one sec|give me a (?:sec|minute)|sure|ok|okay|got it|will do)\b")


def holding(msg: "Message") -> bool:
    body = " ".join((msg.body or "").split())
    return bool(body) and len(body) < 120 and bool(_HOLD.search(body)) and "?" not in body


def person(raw: str) -> tuple[str, str]:
    """"Person, Matt <address>" or "Matt Person <address>" -> ("Matt Person", "address")."""
    text = (raw or "").strip().strip('"').strip()
    email = ""
    found = _EMAIL.search(text)
    if found:
        email = found.group(0)
        text = (text[: found.start()] + text[found.end():])
    text = re.sub(r"[<>\[\]()]|mailto:", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;\"'")
    # Outlook directory status: "Person, Matt, Offline"
    text = re.sub(r",\s*(?:Offline|Available|Busy|Away|Out of Office|Do not disturb)\s*$", "", text, flags=re.I)
    if "," in text:
        last, _, rest = text.partition(",")
        rest = rest.strip()
        if rest and " " not in last.strip() or (rest and len(last.split()) <= 2):
            text = (rest + " " + last.strip()).strip()
    if not text and email:
        text = email.split("@")[0].replace(".", " ").title()
    return text, email


def first_name(name: str) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p and not p.endswith(".")]
    if not parts:
        return ""
    head = parts[0]
    return head if len(head) > 1 else (parts[1] if len(parts) > 1 else head)


def _people(raw: str) -> list[str]:
    out = []
    for chunk in re.split(r";|\n", raw or ""):
        name, email = person(chunk)
        if name or email:
            out.append(name or email)
    return out


def _is_me(name: str, email: str, me: list[str]) -> bool:
    hay = (name + " " + email).lower()
    for item in me:
        item = item.strip().lower()
        if not item:
            continue
        if " " in item and item in hay:
            return True
        if "@" in item and item in hay:
            return True
    # "Sam van Dyke" also matches "van Dyke, Sam"
    full = [m for m in me if " " in m.strip()]
    for item in full:
        toks = [t for t in re.split(r"\W+", item.lower()) if len(t) > 2]
        if toks and all(t in hay for t in toks):
            return True
    return False


def _clean_body(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").replace("\r", "").split("\n")]
    out: list[str] = []
    blank = 0
    for line in lines:
        bare = line.strip().lstrip(">").strip()
        if bare in {"", "\u00a0", "\ufffc"}:
            blank += 1
            if blank <= 1 and out:
                out.append("")
            continue
        blank = 0
        out.append(bare)
    text = "\n".join(out).strip()
    return text


def _cut_signature(body: str) -> tuple[str, str]:
    """Body and the signature block. The signature is kept for facts but not shown as the ask."""
    match = _SIG_START.search(body)
    if match and match.start() > 20:
        return body[: match.start()].strip(), body[match.start():].strip()
    lines = body.split("\n")
    for idx, line in enumerate(lines):
        if idx > 1 and re.search(r"\|\s*(?:Sr\.|Senior|Director|Manager|Lead|Engineer|VP|Head|Commercial)", line):
            return "\n".join(lines[: max(idx - 1, 1)]).strip(), "\n".join(lines[idx - 1:]).strip()
    return body, ""


def _header_fields(rest: str) -> dict:
    out: dict[str, str] = {}
    key = ""
    for line in rest.split("\n"):
        m = re.match(r"^[ \t>]*(Sent|Date|To|Cc|CC|Bcc|Subject):[ \t]*(.*)$", line)
        if m:
            key = m.group(1).lower()
            out[key] = m.group(2).strip()
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def parse(text: str, me: list[str] | None = None, top_sender: str = "", top_to: list[str] | None = None,
          top_cc: list[str] | None = None, subject: str = "") -> Thread:
    me = [m.strip() for m in (me or []) if m and m.strip()]
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    marks = []
    for m in _OUTLOOK.finditer(raw):
        fields = _header_fields(m.group("rest"))
        if not ({"sent", "date"} & set(fields)) and "subject" not in fields:
            continue
        marks.append((m.start(), m.end(), m.group("from"), fields))
    for m in _WROTE.finditer(raw):
        marks.append((m.start(), m.end(), m.group("from"), {"date": m.group("date")}))
    marks.sort(key=lambda item: item[0])
    thread = Thread(me=me)
    if not marks:
        return thread
    thread.draft = _clean_body(raw[: marks[0][0]])
    for idx, (start, end, sender_raw, fields) in enumerate(marks):
        stop = marks[idx + 1][0] if idx + 1 < len(marks) else len(raw)
        name, email = person(sender_raw)
        body, _sig = _cut_signature(_clean_body(raw[end:stop]))
        msg = Message(
            sender=name,
            email=email,
            date=(fields.get("sent") or fields.get("date") or "").strip(),
            to=_people(fields.get("to", "")),
            cc=_people(fields.get("cc", "")) + _people(fields.get("bcc", "")),
            subject=(fields.get("subject") or subject or "").strip(),
            body=body,
        )
        msg.mine = _is_me(name, email, me)
        thread.messages.append(msg)
    return thread


def asks(msg: Message) -> list[str]:
    """Questions and requests in one message, in order."""
    out = []
    body = re.sub(r"\s+", " ", msg.body or "")
    for q in _QUESTION.findall(body):
        q = q.strip(" -–—")
        if len(q) > 6:
            out.append(q)
    for sentence in re.split(r"(?<=[.!])\s+", body):
        s = sentence.strip()
        if s and s not in out and not s.endswith("?") and re.match(
                r"(?i)^(?:@?\w+[,:]?\s+)?(?:please|pls|can you|could you|would you|send|confirm|let me know|kindly)\b", s):
            out.append(s)
    return out[:8]


def refers_to_hidden(msg: Message) -> list[str]:
    """Words that point at something a text read cannot see: an image, a table, an attachment."""
    hits = []
    body = msg.body or ""
    for pat, label in (
        (r"(?i)\b(?:below|beneath|see (?:the )?(?:image|screenshot|picture|chart|table)|screenshot|snip|pictured)\b", "an image or table inside the message"),
        (r"(?i)\b(?:attached|attachment|enclosed|the (?:file|sheet|spreadsheet|workbook|deck|pdf|doc))\b", "an attached file"),
        (r"(?i)[\"“”']?asterisk[\"“”']?|\bfootnote\b|\bfine print\b", "a footnote or asterisk rule"),
    ):
        if re.search(pat, body):
            hits.append(label)
    return hits


def addressees(thread: Thread) -> list[str]:
    """First names to answer: the newest sender, plus anyone in the open messages with their own ask."""
    names: list[str] = []
    for msg in thread.open_messages():
        if msg.first and msg.first not in names and asks(msg):
            names.append(msg.first)
    newest = thread.newest_other()
    if newest and newest.first and newest.first not in names:
        names.insert(0, newest.first)
    return names


def render(thread: Thread, limit: int = 9000) -> str:
    """The thread as a briefing block for the model."""
    if not thread.ok:
        return ""
    newest = thread.newest_other()
    lines = ["EMAIL THREAD (newest first). Reply to the NEWEST message from someone else."]
    opened = thread.open_messages()
    if newest is not None:
        lines.append("")
        lines.append("NEWEST MESSAGE (answer this):")
        lines.append(_head(newest))
        lines.append(newest.body.strip()[:3500])
        hidden = refers_to_hidden(newest)
        if hidden:
            lines.append("NOTE: this message points at " + ", ".join(hidden)
                         + ". Use the ATTACHMENTS, SCREEN TEXT and NOTES blocks for it. If none of them show it, "
                           "say what you checked; do not guess what it says and do not claim it is missing.")
        q = asks(newest)
        if q:
            lines.append("Their asks: " + " | ".join(q))
    for msg in opened[1:]:
        lines.append("")
        lines.append("ALSO OPEN (sent after my last message):")
        lines.append(_head(msg))
        lines.append(msg.body.strip()[:1500])
        q = asks(msg)
        if q:
            lines.append("Their asks: " + " | ".join(q))
    settled = thread.settled()
    if settled:
        lines.append("")
        lines.append("EARLIER IN THE THREAD (context only; these were already handled by a later message. "
                     "Do not answer them again and do not repeat old requests):")
        for msg in settled[:12]:
            body = re.sub(r"\s+", " ", msg.body).strip()
            lines.append("- " + ("me" if msg.mine else msg.sender) + (" (" + msg.date + ")" if msg.date else "")
                         + ": " + body[:420])
    names = addressees(thread)
    if names:
        lines.append("")
        lines.append("People to answer, in order: " + ", ".join(names)
                     + ". Do not address anyone else on the thread unless the newest message asks you to.")
    text = "\n".join(lines).strip()
    return text[:limit]


def _head(msg: Message) -> str:
    bits = ["from " + (msg.sender or msg.email or "?")]
    if msg.to:
        bits.append("to " + ", ".join(msg.to[:8]))
    if msg.cc:
        bits.append("cc " + ", ".join(msg.cc[:8]))
    if msg.date:
        bits.append(msg.date)
    return "[" + "; ".join(bits) + "]"


_CHAT_HEAD = re.compile(r"(?m)^\s*(?:\[?(?P<t1>\d{1,2}:\d{2}(?:\s?[AP]M)?)\]?\s+)?(?P<name>[A-Z][\w.'-]+(?: [A-Z][\w.'-]+){0,3})(?:\s{2,}|\s+[-–·]\s+|:\s*$|\s+)(?P<t2>\d{1,2}:\d{2}\s?(?:[AP]M)?)?\s*$")


def parse_chat(text: str, me: list[str] | None = None) -> Thread:
    """Slack/Teams/Zoom style: a "Name  10:02 AM" line, then the message lines. Oldest first on screen."""
    me = [m.strip() for m in (me or []) if m and m.strip()]
    raw = (text or "").replace("\r", "")
    heads = [m for m in _CHAT_HEAD.finditer(raw) if (m.group("t1") or m.group("t2"))]
    thread = Thread(me=me)
    if len(heads) < 1:
        return thread
    msgs = []
    for idx, m in enumerate(heads):
        stop = heads[idx + 1].start() if idx + 1 < len(heads) else len(raw)
        body = _clean_body(raw[m.end():stop])
        name = m.group("name").strip()
        msg = Message(sender=name, date=(m.group("t1") or m.group("t2") or ""), body=body)
        msg.mine = _is_me(name, "", me) or any(name.lower() == x.lower() for x in me)
        msgs.append(msg)
    thread.messages = list(reversed(msgs))
    return thread
