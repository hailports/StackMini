"""The full-context draft: thread, attachments, notes, computed facts, voice, then a check.

This is the path for email threads, chat threads with files, and screenshots. It gives the
model what a careful person would have open before answering:

  thread       who wrote what, newest first, what is still open, who to answer
  attachments  the files the thread names or carries, read and summarised
  notes        notes-helper hits and the rule or formula the ask is about (code, docs)
  data facts   numbers worked out from the attachment with that rule
  screen text  anything else on screen (window OCR, inline images)

The reply is then checked: every number has to trace back to one of those blocks, a claim
that something is missing has to be backed by them, settled asks stay settled, everyone
with an open ask gets an answer, and when the data has the answer the reply uses it.
A reply that fails is rewritten with the problems listed. Nothing is ever sent.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from hailports.clip import attach, datafacts, owndraft, rulenotes, shape
from hailports.clip import thread as threadmod

QUALITY_RULES = """How to write this reply:
1. Answer the NEWEST message from someone else. Asks from earlier in the thread that a later message already handled are background. Do not answer them again or repeat old requests.
2. Lead with the direct answer to their question (yes / no / the number), then the reason in one line.
3. Use the DATA FACTS, ATTACHMENTS and NOTES blocks for the facts. Quote one or two key figures, not every figure; give one short worked example only when they asked how a rule or calculation works. Do not stay vague when the facts are there, and do not dump them either.
4. Never say something is missing, not built, not possible or not there unless a block says so in plain words. A code snippet that does not show a rule is not proof it is missing. When the NOTES or DATA FACTS contain the rule, it is part of the logic: say yes and explain it with the numbers. "Your logic" / "your file" means work I did; the NOTES describe it.
5. If the data shows exceptions (rows that break a rule, a gap, a conflict) and they asked about them, name them briefly (group equal values: "a, b, c (all $125k)"), and end with one concrete offer that says what would happen, e.g. "want me to bump those up to $300k? then their level 3 would land at $600k.".
   For a worked example, use the WORKED EXAMPLE line (a row where the rule applies), not an exception.
6. Speak to each person listed under "People to answer", by first name. Skip people with nothing open.
7. Do not ask for files or information that are already in the thread or the blocks. Do not invent names, numbers, dates, links or promises.
8. Write as me (I). Plain text, no markdown headers or bold. A short numbered list ("1. ...") for steps or a fix. No signature, no subject line. Keep it short: what a busy expert would type."""

_ABSENT = re.compile(
    r"(?i)\b(?:does(?:n'?t| not)|do(?:n'?t| not)|is(?:n'?t| not)|are(?:n'?t| not)|was(?:n'?t| not)|"
    r"not (?:built|included|set up|supported|there|in place|handled)|missing|no logic|lacks?|can'?t|cannot)\b"
)
_PUSHBACK = re.compile(r"(?i)\b(?:please |can you |could you )?(?:send|share|provide)\b[^.?!\n]{0,40}\b(?:updated |the |a |an )?(?:file|sheet|spreadsheet|list|attachment|goals)\b")
_NUMBER = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:k|m|mm|million)?\b|\b\d[\d,]*(?:\.\d+)?\s*(?:k|m|%)?\b", re.I)


@dataclass
class Context:
    screen: str = ""
    subject: str = ""
    thread: threadmod.Thread | None = None
    thread_block: str = ""
    attach_block: str = ""
    notes_block: str = ""
    facts_block: str = ""
    image_block: str = ""
    voice: str = ""
    me: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    settled_people: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)  # data: URLs for a vision model
    plan: str = ""
    files: list[str] = field(default_factory=list)

    prior: dict = field(default_factory=dict)
    prior_block: str = ""
    kind: str = "email"       # email or chat (shape.surface)
    target: int = 120         # words
    cap: int = 180            # hard word cap; over it gets a retry-shorter pass
    examples: str = ""        # real sent replies, local only

    def sources(self) -> str:
        # The user's own edits to an earlier draft count as source facts.
        edited = str(self.prior.get("placed") or "") if self.prior.get("kind") not in (None, "", "none", "exact") else ""
        kept = "\n".join(self.locks())
        return "\n".join([self.screen, self.thread_block, self.attach_block, self.notes_block,
                          self.facts_block, self.image_block, edited, kept])

    def lock_plan(self) -> dict | None:
        plan = (self.prior or {}).get("locks")
        return plan if isinstance(plan, dict) else None

    def locks(self) -> list[str]:
        return list((self.lock_plan() or {}).get("locks") or [])

    def has_data(self) -> bool:
        return bool(self.facts_block.strip() or self.attach_block.strip() or self.notes_block.strip())


def _subject(doc: dict, text: str) -> str:
    blocks = doc.get("blocks") or []
    for idx, block in enumerate(blocks):
        if isinstance(block, dict) and str(block.get("text") or "").strip().lower() == "subject:":
            for nxt in blocks[idx + 1: idx + 3]:
                val = str((nxt or {}).get("text") or "").strip()
                if val and val.lower() != "subject:":
                    return val
    m = re.search(r"(?im)^\s*Subject:\s*(.+)$", text or "")
    if m:
        return m.group(1).strip()
    return str(doc.get("title") or "")


def _recipients(doc: dict) -> tuple[list[str], list[str]]:
    """To and Cc of the compose window, from the directory chips next to the labels."""
    to, cc, cur = [], [], None
    for block in doc.get("blocks") or []:
        text = str((block or {}).get("text") or "").strip()
        low = text.lower()
        if low in {"to:", "to"}:
            cur = to
            continue
        if low in {"cc:", "cc", "bcc:"}:
            cur = cc
            continue
        if low in {"subject:", "from:"}:
            cur = None
            continue
        if cur is not None and "@" in text:
            name, _email = threadmod.person(text)
            if name:
                cur.append(name)
    return to, cc


def screen_text(doc: dict) -> str:
    parts = [str(doc.get("selection") or ""), str(doc.get("field_text") or ""), str(doc.get("window_ocr") or "")]
    seen = set()
    for block in doc.get("blocks") or []:
        text = str((block or {}).get("text") or "").strip() if isinstance(block, dict) else str(block)
        if text and text not in seen and len(text) > 2:
            seen.add(text)
            parts.append(text)
    return "\n".join(p for p in parts if p.strip())


def _image_urls(doc: dict) -> list[str]:
    out = []
    for item in doc.get("images") or []:
        path = Path(str(item)).expanduser()
        if path.is_file() and path.stat().st_size < 12 * 1024 * 1024:
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            out.append(f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii"))
    for raw in doc.get("image_b64") or []:
        if isinstance(raw, str) and raw:
            out.append(raw if raw.startswith("data:") else "data:image/png;base64," + raw)
    return out[:3]


def gather(doc: dict, cfg: dict | None, lookup=None, extra_dirs: list[Path] | None = None,
           code_roots: list[Path] | None = None) -> Context:
    paiste = (cfg or {}).get("paiste") or {}
    me = [p.strip() for p in str(paiste.get("me") or "").split(",") if p.strip()]
    ctx = Context(me=me)
    ctx.screen = screen_text(doc)
    ctx.subject = _subject(doc, ctx.screen)
    if isinstance(doc.get("prior_draft"), dict):
        ctx.prior = doc["prior_draft"]
        ctx.prior_block = owndraft.prior_note(ctx.prior)
    field_text = str(doc.get("field_text") or "") or ctx.screen
    ctx.thread = threadmod.parse(field_text, me, subject=ctx.subject)
    if not ctx.thread.ok:
        chat = threadmod.parse_chat(field_text, me)
        if chat.ok and chat.newest_other() is not None:
            ctx.thread = chat
    if not ctx.thread.ok and doc.get("window_ocr"):
        ctx.thread = threadmod.parse(str(doc.get("window_ocr")), me, subject=ctx.subject)
    if ctx.thread.ok:
        ctx.thread_block = threadmod.render(ctx.thread)
        ctx.people = threadmod.addressees(ctx.thread)
        open_names = {m.first for m in ctx.thread.open_messages()}
        ctx.settled_people = sorted({m.first for m in ctx.thread.settled() if not m.mine and m.first and m.first not in open_names})
    newest = ctx.thread.newest_other() if ctx.thread and ctx.thread.ok else None
    ask = newest.body if newest else ctx.screen[-2500:]

    # Attachments: names on screen, local folders, then the configured helper.
    names = attach.names_in(ctx.screen)
    dirs = [Path(p).expanduser() for p in (paiste.get("attach_dirs") or []) if str(p).strip()]
    dirs += extra_dirs if extra_dirs is not None else attach.default_dirs()
    found = attach.find_files(names, ctx.subject, [d for d in dirs if d.is_dir()]) if (names or ctx.subject) else []
    tables, docs_text = [], []
    for path in found:
        ext = attach.extract(path)
        ctx.files.append(path.name)
        if ext.kind == "image" and not ext.text.strip() and path.stat().st_size < 12 * 1024 * 1024:
            # No OCR on this machine: let a vision model look at the picture itself.
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            ctx.images.append(f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii"))
            docs_text.append(f"[{path.name}] image attached below; read it.")
            continue
        if ext.tables:
            tables.extend(ext.tables)
            for table in ext.tables[:3]:
                docs_text.append(f"[{path.name} / {table.name}] {len(table.rows)} rows\n" + attach.table_text(table, 25))
        elif ext.text.strip():
            docs_text.append(f"[{path.name}]\n" + ext.text.strip()[:6000])
    helper = str(paiste.get("attach_cmd") or "").strip()
    if helper:
        remote = attach.remote(helper, {"subject": ctx.subject, "names": names, "ask": ask[:3000],
                                        "thread": (ctx.thread_block or ctx.screen)[:6000]})
        if remote:
            docs_text.append(remote[:12000])
    ctx.attach_block = "\n\n".join(docs_text)[:16000]

    # Notes: notes helper plus the rule the ask is about in local code and docs.
    notes = []
    if lookup is not None:
        try:
            # The notes helper gets the ask plus the whole thread (and screenshot text), not only the ask.
            brain = str(lookup(ask + "\n" + ctx.subject + "\n" + (ctx.thread_block or ctx.screen or "")) or "").strip()
        except Exception:  # a lookup failure must not stop the draft
            brain = ""
        if brain:
            notes.append(brain[:4000])
    roots = code_roots if code_roots is not None else rulenotes.roots_from()
    rules = rulenotes.search(ask, roots) if roots else ""
    if rules:
        notes.append("From local code and docs:\n" + rules)
    ctx.notes_block = "\n\n".join(notes)[:9000]

    # Computed facts from the data and the best rule text.
    rule_text = _best_rule(ctx.notes_block + "\n" + ctx.screen + "\n" + ctx.attach_block)
    if tables:
        ctx.facts_block = datafacts.facts(tables, ask, rule_text, ctx.subject)
    elif "DATA FACTS" in ctx.attach_block:
        ctx.facts_block = ""  # the helper already computed them inside its block

    ctx.images = (_image_urls(doc) + ctx.images)[:4]
    voice_file = str(paiste.get("context_file") or "").strip()
    if voice_file and Path(voice_file).expanduser().is_file():
        ctx.voice = Path(voice_file).expanduser().read_text(encoding="utf-8", errors="replace")[:1800]
    ctx.kind = shape.surface(doc)
    incoming = shape.words(shape.newest_message(screen_text(doc)))
    ctx.target, ctx.cap = shape.word_cap(ctx.kind, incoming)
    ctx.examples = shape.load_examples(paiste)
    return ctx


def _best_rule(text: str) -> str:
    best, score = "", 0
    chunks = re.split(r"\n---\n|\n\n", text or "")
    for chunk in chunks:
        rule = datafacts.parse_rule(chunk)
        s = len(rule) + (2 if "offset" in rule and "low" in rule and "high" in rule else 0)
        if s > score:
            best, score = chunk, s
    return best if score >= 2 else ""


def messages(ctx: Context, rules: str) -> list[dict]:
    system = (rules.strip() + "\n\n" + QUALITY_RULES).strip()
    if ctx.me:
        system += f"\n\nYou are {ctx.me[0]}. Never greet yourself and never sign the reply."
    if ctx.voice:
        system += "\n\nVOICE (tone and style only):\n" + ctx.voice
    if ctx.examples:
        system += ("\n\nMY REAL SENT REPLIES (copy the shape and tone; never copy a fact, name or number from them):\n"
                   + ctx.examples)
    system += "\n\n" + shape.rules(ctx.kind, ctx.target, ctx.cap)
    blocks = []
    if ctx.thread_block:
        blocks.append(ctx.thread_block)
    else:
        blocks.append("ON SCREEN:\n" + ctx.screen[-9000:])
    if ctx.facts_block:
        blocks.append("DATA FACTS (computed from the attached data; quote these numbers):\n" + ctx.facts_block)
    if ctx.attach_block:
        blocks.append("ATTACHMENTS (read from the files):\n" + ctx.attach_block)
    if ctx.notes_block:
        blocks.append("NOTES (notes helper, code and docs; use only what matches this message):\n" + ctx.notes_block)
    if ctx.image_block:
        blocks.append("IMAGES IN THE MESSAGE (text read from them):\n" + ctx.image_block)
    extra = str(ctx.screen)
    if ctx.thread_block and "window_ocr" in extra:
        pass
    task = "Write the reply now. Only the reply text."
    if ctx.prior_block:
        blocks.append(ctx.prior_block)
        task = "Write the reply now: the whole text that replaces your earlier draft. Only the reply text."
    if ctx.people:
        task += " Answer: " + ", ".join(ctx.people) + "."
    user_text = "\n\n".join(blocks) + "\n\n" + task
    if ctx.images:
        content: list | str = [{"type": "text", "text": user_text}] + [
            {"type": "image_url", "image_url": {"url": url}} for url in ctx.images]
    else:
        content = user_text
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def _values(text: str) -> set[float]:
    vals = set()
    for m in _NUMBER.finditer(text or ""):
        raw = m.group(0).lower().replace("$", "").replace(",", "").strip()
        mult = 1.0
        if raw.endswith("%"):
            raw = raw[:-1]
        elif raw.endswith("mm") or raw.endswith("million"):
            mult, raw = 1e6, re.sub(r"(mm|million)$", "", raw)
        elif raw.endswith("m"):
            mult, raw = 1e6, raw[:-1]
        elif raw.endswith("k"):
            mult, raw = 1e3, raw[:-1]
        try:
            vals.add(round(float(raw.strip()) * mult, 2))
        except ValueError:
            continue
    return vals


def check(text: str, ctx: Context) -> list[str]:
    """Problems with a draft. Empty means it passes."""
    issues: list[str] = []
    body = (text or "").strip()
    if not body:
        return ["empty reply"]
    src_vals = _values(ctx.sources())
    unknown = []
    for v in sorted(_values(body)):
        if v < 10 and float(v).is_integer():
            continue  # small counts and list numbers
        if any(abs(v - s) <= max(0.006 * s, 0.5) for s in src_vals):
            continue
        if any(abs(v / 100.0 - s) < 0.001 for s in src_vals) or any(abs(v * 100.0 - s) < 0.5 for s in src_vals):
            continue  # 120% and x1.2 are the same number
        unknown.append(v)
    if unknown:
        issues.append("these numbers are not in the thread, data or notes: "
                      + ", ".join(datafacts.fmt_money(v) if v >= 1000 else f"{v:g}" for v in unknown[:6])
                      + ". Use only numbers from the blocks.")
    if _ABSENT.search(body) and ctx.has_data():
        has_num = bool(_values(body) & src_vals)
        if not has_num:
            issues.append("the reply says something is missing or not done without citing the data. "
                          "Check the NOTES and DATA FACTS: if they show it, say so with the figures; if they do not, say what you checked.")
    if _ABSENT.search(body) and "RULE IS IN THE NOTES" in ctx.facts_block and re.search(
            r"(?i)\b(?:not|n't)\b[^.\n]{0,40}\b(?:build|built|include|implement|apply|handle|cover)", body):
        rule_line = next((l for l in ctx.facts_block.splitlines() if l.startswith("RULE IS IN THE NOTES")), "")
        issues.append("the reply says the rule is not built in, but the notes contain it: " + rule_line
                      + " Say it is built in, explain it with an example, and name only the real gap.")
    if ctx.facts_block and not (_values(body) & _values(ctx.facts_block)):
        issues.append("the DATA FACTS have the answer but the reply uses none of its numbers. Quote the specific figures and one example.")
    if ctx.files and _PUSHBACK.search(body):
        issues.append("the reply asks them to send a file that is already attached (" + ", ".join(ctx.files[:2]) + "). Use it instead.")
    for name in ctx.settled_people:
        if re.search(r"(?im)^(?:hey |hi )?" + re.escape(name.lower()) + r"\b\s*(?:--|-|,|:)", body.lower()):
            issues.append(f"{name} has nothing open; their ask was handled earlier. Do not address them.")
    if len(ctx.people) > 1:
        missing = [p for p in ctx.people if p.lower() not in body.lower()]
        if missing:
            issues.append("answer each person with an open ask; missing: " + ", ".join(missing) + ".")
    issues += shape.issues(body, ctx.kind, ctx.cap)
    if re.search(r"(?m)^\s*(?:#|\*\*|- \*\*)", body):
        issues.append("no markdown.")
    issues += _prior_issues(body, ctx)
    return issues


def _prior_issues(body: str, ctx: Context) -> list[str]:
    """A redraft must not copy the earlier draft, answer it, or drop the user's edits."""
    prior = ctx.prior or {}
    original = str(prior.get("original") or "").strip()
    if not original:
        return []
    import difflib
    from hailports.clip import locks
    out = []
    low = " ".join(body.lower().split())
    kept = ctx.locks()
    gone = locks.missing(body, kept)
    if gone:
        out.append("keep these sentences word for word, in place: " + " | ".join(f'"{g}"' for g in gone[:6]))
    explicit = bool((ctx.lock_plan() or {}).get("explicit")) or bool(prior.get("fresh"))
    if not kept and prior.get("kind") in ("exact", "none") and difflib.SequenceMatcher(
            None, low, " ".join(original.lower().split())).ratio() > 0.93:
        out.append("this is a copy of your earlier draft; write a better version")
    if not explicit and prior.get("kind") in ("edited", "partial") and prior.get("placed"):
        # Numbers, amounts and dates the user typed in are facts: they must survive the redraft.
        missing = [w.strip(".,") for w in prior.get("added") or [] if re.search(r"\d", w) and w.strip(".,") not in low]
        cut = [w.strip(".,") for w in prior.get("removed") or [] if re.search(r"\d", w) and w.strip(".,") in low
               and w.strip(".,") not in ctx.screen.lower()]
        if missing:
            out.append("keep the user's edits: " + ", ".join(sorted(set(missing))[:8]))
        if cut:
            out.append("the user cut these from your draft; leave them out: " + ", ".join(sorted(set(cut))[:8]))
    if re.search(r"(?i)\b(your (earlier|previous) draft|thanks for (the|your) draft)\b", body):
        out.append("do not reply to your own earlier draft")
    return out


def score(text: str, ctx: Context) -> int:
    probs = check(text, ctx)
    s = -10 * len(probs)
    s += min(len(_values(text) & _values(ctx.facts_block + ctx.notes_block)), 8)
    return s


def cleanup(text: str, me: list[str]) -> str:
    from hailports.clip.scene import plain_reply
    body = plain_reply(text or "")
    body = re.sub(r"(?is)^\s*(?:subject|re):[^\n]*\n+", "", body)
    lines = body.rstrip().split("\n")
    # Drop a trailing signature line with my name.
    while lines and me and any(lines[-1].strip().lower().strip(",.-– ") == m.lower() or
                               lines[-1].strip().lower() == m.split()[0].lower() for m in me):
        lines.pop()
    while lines and re.match(r"(?i)^\s*(?:best|thanks|regards|cheers)[,!.]?\s*$", lines[-1]):
        lines.pop()
    return "\n".join(lines).strip()


ANALYZE = """Before the reply is written, work out the answer. Return only JSON:
{"asks": [{"who": "<first name>", "ask": "<their question or request, short>"}],
 "answers": [{"ask": "...", "answer": "<direct answer>", "evidence": "<the line from THREAD / DATA FACTS / NOTES / ATTACHMENTS that proves it>"}],
 "numbers": ["<the specific figures and names the reply should quote>"],
 "example": "<one worked example from the data, if a rule or calculation is involved>",
 "exceptions": "<rows or cases that break the rule, with values, or empty>",
 "offer": "<one concrete next step to offer, or empty>",
 "unknown": "<what the blocks cannot tell, or empty>"}
Only use what the blocks show. If the NOTES or DATA FACTS contain the rule, the answer is that it is built in."""


def analyze(ctx: Context, msgs: list[dict], complete) -> str:
    probe = [dict(m) for m in msgs]
    last = dict(probe[-1])
    if isinstance(last["content"], list):
        parts = [dict(part) for part in last["content"]]
        parts[0]["text"] = parts[0]["text"].rsplit("\n\nWrite the reply now", 1)[0] + "\n\n" + ANALYZE
        last["content"] = parts
    else:
        last["content"] = last["content"].rsplit("\n\nWrite the reply now", 1)[0] + "\n\n" + ANALYZE
    probe[-1] = last
    try:
        raw = str(complete(probe) or "")
    except Exception:
        return ""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return ""
    try:
        doc = json.loads(m.group(0))
    except json.JSONDecodeError:
        return ""
    return json.dumps(doc, ensure_ascii=False)[:4000]


def draft(doc: dict, cfg: dict | None, rules: str, complete, lookup=None, tries: int = 3,
          extra_dirs: list[Path] | None = None, code_roots: list[Path] | None = None, log=None) -> tuple[str, Context, list]:
    """Return (reply, context, attempts). `complete(messages) -> text`."""
    ctx = gather(doc, cfg, lookup=lookup, extra_dirs=extra_dirs, code_roots=code_roots)
    msgs = messages(ctx, rules)
    plan = analyze(ctx, msgs, complete) if ctx.has_data() or (ctx.thread and ctx.thread.ok) else ""
    ctx.plan = plan
    if plan:
        last = msgs[-1]
        note = ("\n\nMY ANALYSIS (use it; the reply must lead with the direct answer, use one or two of the key numbers, "
                "list the fix steps when there are any, and end with the offer; leave the other figures out unless they asked):\n" + plan)
        if isinstance(last["content"], list):
            last["content"][0]["text"] += note
        else:
            last["content"] += note
    attempts = []
    best, best_score = "", -10**6
    for attempt in range(max(1, tries)):
        raw = complete(msgs)
        text = cleanup(str(raw or ""), ctx.me)
        probs = check(text, ctx)
        s = score(text, ctx)
        attempts.append({"text": text, "issues": probs, "score": s})
        if log:
            log(attempt, text, probs)
        if s > best_score:
            best, best_score = text, s
        if not probs:
            break
        msgs = msgs + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "Rewrite the reply. Fix these problems:\n- " + "\n- ".join(probs)
             + "\nKeep everything that was right. Only the reply text."},
        ]
    if best and shape.words(best) > ctx.cap and not ctx.locks():
        # Hard length cap: one more retry-shorter pass, kept only when it lands under the cap.
        shorter = msgs + [
            {"role": "assistant", "content": best},
            {"role": "user", "content": shape.issues(best, ctx.kind, ctx.cap)[0] + " Only the reply text."},
        ]
        try:
            again = cleanup(str(complete(shorter) or ""), ctx.me)
        except Exception:
            again = ""
        if again and shape.words(again) <= ctx.cap:
            attempts.append({"text": again, "issues": check(again, ctx), "score": score(again, ctx)})
            best = again
    if ctx.locks():
        # Still missing a kept sentence after the retries: splice it back, in order and place.
        from hailports.clip import locks
        best = locks.enforce(best, ctx.lock_plan())
    if best:
        best = shape.finish(best, ctx.kind, ctx.me)
    return best, ctx, attempts


def wants_full(doc: dict, cfg: dict | None) -> bool:
    """Use the full-context path for threads, files, screenshots and redrafts."""
    if doc.get("prior_draft"):
        return True
    if doc.get("images") or doc.get("image_b64") or doc.get("window_ocr"):
        return True
    text = str(doc.get("field_text") or "") + "\n" + str(doc.get("selection") or "")
    if re.search(r"(?im)^[ \t>]*From:[^\n]+\n[ \t>]*(?:Sent|Date|To):", text) or re.search(r"(?im)^On .{6,80} wrote:\s*$", text):
        return True
    return bool(attach.names_in(screen_text(doc)))
