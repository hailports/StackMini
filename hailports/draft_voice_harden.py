"""Draft voice harden (public): checks first; paid only when checks fail.

Flag semantics: a caller-supplied flag path (or env DRAFT_SECOND_PASS_FLAG)
means paid is *allowed on failure*, not always-on. Fail-open. Drafts only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

FLAG_SECOND = Path(
    os.environ.get(
        "DRAFT_SECOND_PASS_FLAG",
        str(Path.home() / "claude-stack/data/flags/draft_second_pass"),
    )
)
LOG_SECOND = Path(
    os.environ.get(
        "DRAFT_SECOND_PASS_LOG",
        str(Path.home() / "claude-stack/data/runtime/draft-second-pass.jsonl"),
    )
)
LOG_ROUTER = Path(
    os.environ.get(
        "DRAFT_ROUTER_LOG",
        str(Path.home() / "claude-stack/data/runtime/router-decisions.jsonl"),
    )
)

VOICE_HARD_RULES = (
    "VOICE HARD:\n"
    "- write everything lowercase, including sentence starts; short plain sentences; "
    "no sign-off (the mail client adds the signature).\n"
    "- never restate what the other person just said; if agreeing, say \"agreed\" plus "
    "what we will or won't do.\n"
    "- only state facts present in the thread or lookup results; otherwise omit or write "
    "\"need to check\".\n"
    "- no promises or \"i'll\" lines for work not done; report what is done.\n"
)

_KEEP_CASE = re.compile(
    r"(https?://[^\s<>\]]+)|"
    r"([\w.+-]+@[\w-]+(?:\.[\w-]+)+)|"
    r"(\b(?:AAMk|AAMkA)[A-Za-z0-9+/=_-]{10,}\b)|"
    r"(\b0[0-9A-Za-z]{14,17}\b)|"
    r"(\b[A-Z]{2,6}-\d{2,}\b)|"
    r"(\b[0-9a-f]{12,40}\b)",
    re.I,
)

_SIGNOFF = re.compile(
    r"(?is)(?:\n+|(?<=[.!?])\s+|\A)\s*(?:"
    r"best(?:\s+regards)?|kind regards|warm regards|regards|"
    r"thanks(?:\s+and\s+regards)?|thank you|cheers|sincerely|"
    r"respectfully|yours truly"
    r")[,!.]?\s*"
    r"(?:\s*[A-Za-z][A-Za-z .'-]{0,40})?"
    r"\s*\Z"
)

_SECRET_HARD = re.compile(
    r"(?i)\b(password\s*[:=]|seed phrase|2fa\b|otp\b|totp\b|recovery code|"
    r"private key|api[_-]?key)\b"
)

_I_LL = re.compile(
    r"(?i)\bi(?:'|’)?ll\b|\bi will\b|\blet me\b|\bi can (?:get|look|check|send|update)\b"
)

_AI_FILLER = (
    "i hope this helps",
    "hope this helps",
    "certainly",
    "absolutely",
    "happy to help",
    "delve",
    "leverage",
    "robust",
    "seamless",
    "as an ai",
    "as a language model",
    "i'm an ai",
    "i am an ai",
)

_AI_STACK = re.compile(
    r"(?i)\b(?:as an ai|language model|system prompt|chatgpt|openai|anthropic)\b"
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I)
_URL = re.compile(r"https?://[^\s<>\]\)\"']+", re.I)
_NUM = re.compile(r"\b\d{3,}(?:[./-]\d{2,})*\b")
_DATE = re.compile(
    r"(?i)\b(?:"
    r"\d{4}-\d{2}-\d{2}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}"
    r"(?:,?\s+\d{4})?"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r")\b"
)
_NAME_GREET = re.compile(r"(?i)^\s*hey\s+([a-z][a-z'.-]{1,30})\b")
_PROPER_IN_SRC = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)\b")
_NAME_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "you",
        "your",
        "this",
        "that",
        "with",
        "from",
        "have",
        "will",
        "need",
        "check",
        "please",
        "thanks",
        "thank",
        "hey",
        "hi",
        "hello",
        "agreed",
        "done",
        "today",
        "tomorrow",
    }
)


def lowercase_keep_tokens(text: str) -> str:
    raw = text or ""
    if not raw:
        return ""
    out: list[str] = []
    last = 0
    for m in _KEEP_CASE.finditer(raw):
        out.append(raw[last : m.start()].lower())
        out.append(m.group(0))
        last = m.end()
    out.append(raw[last:].lower())
    return "".join(out)


def strip_signoffs(text: str) -> str:
    return _SIGNOFF.sub("", (text or "").rstrip()).rstrip()


def post_filter(text: str) -> str:
    try:
        t = strip_signoffs(text or "")
        t = t.replace("—", ",").replace("–", ",")
        t = re.sub(r"\s*--\s*", ", ", t)
        t = lowercase_keep_tokens(t)
        return re.sub(r"\n{3,}", "\n\n", t).strip()
    except Exception:
        return text or ""


def second_pass_enabled(flag: Path | None = None) -> bool:
    path = flag or FLAG_SECOND
    try:
        return path.exists()
    except Exception:
        return False


def paid_allowed_on_failure(flag: Path | None = None) -> bool:
    return second_pass_enabled(flag)


def thread_hash(thread_id: str = "", inbound: str = "", channel: str = "") -> str:
    raw = (thread_id or "") + "|" + (channel or "") + "|" + (inbound or "")[:400]
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _entity_tokens(text: str) -> list[tuple[str, str]]:
    t = text or ""
    out: list[tuple[str, str]] = []
    for m in _EMAIL.finditer(t):
        out.append(("email", m.group(0)))
    for m in _URL.finditer(t):
        out.append(("link", m.group(0).rstrip(".,;)]}\"'")))
    for m in _DATE.finditer(t):
        out.append(("date", m.group(0)))
    for m in _NUM.finditer(t):
        val = m.group(0)
        if re.fullmatch(r"20\d{2}", val):
            continue
        out.append(("number", val))
    gm = _NAME_GREET.match(t)
    if gm:
        name = gm.group(1).lower()
        if name not in _NAME_STOP and len(name) >= 2:
            out.append(("name", name))
    return out


def _restates_inbound(draft: str, inbound: str) -> bool:
    """True when draft copies a long verbatim inbound window (12+ words)."""
    src = re.sub(r"\s+", " ", (inbound or "").lower()).strip()
    dst = re.sub(r"\s+", " ", (draft or "").lower()).strip()
    if len(src) < 60 or len(dst) < 30:
        return False
    dst_body = re.sub(r"(?i)^\s*hey\s+\S+,?\s*", "", dst).strip()
    words = src.split()
    if len(words) < 12:
        return False
    for i in range(0, min(len(words) - 11, 40)):
        window = " ".join(words[i : i + 12])
        if window in dst_body:
            return True
    return False

def _has_uppercase_body(text: str) -> bool:
    masked = _KEEP_CASE.sub(lambda m: " " * (m.end() - m.start()), text or "")
    masked = re.sub(r"\bI\b", "i", masked)
    return bool(re.search(r"[A-Z]", masked))


def run_checks(draft: str, *, inbound: str = "", lookups: str = "") -> list[str]:
    fails: list[str] = []
    raw = (draft or "").strip()
    if not raw or len(raw) < 8 or len(raw.split()) < 3:
        return ["empty_or_malformed"]
    if len(raw) > 1800 or len(raw.split()) > 280:
        fails.append("length_too_long")
    if _has_uppercase_body(raw):
        fails.append("not_lowercase")
    if _SIGNOFF.search(raw):
        fails.append("has_signoff")
    if _I_LL.search(raw):
        fails.append("ill_promise")
    if _restates_inbound(raw, inbound or ""):
        fails.append("restates_inbound")
    low = raw.lower()
    for phrase in _AI_FILLER:
        if phrase in low:
            fails.append("ai_text")
            break
    if "ai_text" not in fails and _AI_STACK.search(raw):
        fails.append("ai_text")
    ground = f"{inbound or ''}\n{lookups or ''}".lower()
    if ground.strip():
        for kind, val in _entity_tokens(raw):
            needle = val.lower()
            if kind == "link":
                host = re.sub(r"^https?://", "", needle).split("/")[0]
                if needle not in ground and host not in ground:
                    fails.append(f"hallucinated_{kind}")
                    break
            elif needle not in ground:
                if kind == "name":
                    props = {
                        m.group(1).split()[0].lower()
                        for m in _PROPER_IN_SRC.finditer(inbound or "")
                    }
                    if needle in props or needle in ground:
                        continue
                fails.append(f"hallucinated_{kind}")
                break
    if _SECRET_HARD.search(raw) or re.search(r"(?i)/legal/|/finance/", raw):
        fails.append("hard_data_line")
    elif re.search(
        r"(?i)\b(password|seed phrase|2fa|otp|wire transfer|ssn|routing number)\b",
        raw,
    ):
        fails.append("hard_data_line")
    seen: set[str] = set()
    out: list[str] = []
    for f in fails:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _append_jsonl(path: Path, row: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass


def log_decision(
    *,
    thread_id_hash: str,
    checks_failed: list[str],
    retry_used: bool,
    paid_used: bool,
    channel: str = "",
    route: str = "",
    why: str = "",
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fails = [str(x)[:60] for x in (checks_failed or [])][:12]
    row = {
        "ts": ts,
        "thread_id_hash": thread_id_hash,
        "checks_failed": fails,
        "retry_used": bool(retry_used),
        "paid_used": bool(paid_used),
        "channel": (channel or "")[:32],
        "route": route or ("paid" if paid_used else "local"),
        "why": (why or (",".join(fails) if fails else "pass"))[:160],
    }
    _append_jsonl(LOG_SECOND, row)
    _append_jsonl(
        LOG_ROUTER,
        {
            "ts": ts,
            "tool": "draft_second_pass",
            "class": "classify",
            "route": row["route"],
            "tokens": 0,
            "latency": None,
            "why": row["why"],
            "backend": "paid" if paid_used else "local",
            "thread_id_hash": thread_id_hash,
            "retry_used": bool(retry_used),
            "paid_used": bool(paid_used),
            "checks_failed": fails,
        },
    )


def gate(
    text: str,
    *,
    inbound: str = "",
    lookups: str = "",
    channel: str = "outlook",
    thread_id: str = "",
    dry_run: bool = False,
    rewrite_fn=None,
    paid_fn=None,
    flag: Path | None = None,
) -> tuple[str, dict]:
    """Checks -> one local retry -> paid only if still failing and flag allows."""
    raw = post_filter(text or "")
    tid = thread_hash(thread_id, inbound, channel)
    meta = {
        "thread_id_hash": tid,
        "checks_failed": [],
        "retry_used": False,
        "paid_used": False,
        "paid_would": False,
        "route": "local",
    }
    fails = run_checks(raw, inbound=inbound, lookups=lookups)
    if not fails:
        log_decision(
            thread_id_hash=tid,
            checks_failed=[],
            retry_used=False,
            paid_used=False,
            channel=channel,
            route="local",
            why="pass",
        )
        return raw, meta

    meta["retry_used"] = True
    retried = raw
    if rewrite_fn is not None:
        try:
            retried = rewrite_fn(
                raw, fails=fails, inbound=inbound, lookups=lookups, channel=channel
            )
        except Exception:
            retried = raw
    retried = post_filter(retried or raw)
    fails2 = run_checks(retried, inbound=inbound, lookups=lookups)
    if not fails2:
        meta["checks_failed"] = fails
        meta["route"] = "local_retry"
        log_decision(
            thread_id_hash=tid,
            checks_failed=fails,
            retry_used=True,
            paid_used=False,
            channel=channel,
            route="local_retry",
            why="retry_fixed:" + ",".join(fails)[:120],
        )
        return retried, meta

    meta["checks_failed"] = fails2
    if paid_allowed_on_failure(flag):
        meta["paid_would"] = True
        if dry_run:
            meta["route"] = "paid_dry"
            log_decision(
                thread_id_hash=tid,
                checks_failed=fails2,
                retry_used=True,
                paid_used=False,
                channel=channel,
                route="paid_dry",
                why="would_paid:" + ",".join(fails2)[:120],
            )
            return retried, meta
        edited = retried
        if paid_fn is not None:
            try:
                edited = paid_fn(retried, inbound=inbound, channel=channel) or retried
            except Exception:
                edited = retried
        meta["paid_used"] = True
        meta["route"] = "paid"
        final = post_filter(edited)
        log_decision(
            thread_id_hash=tid,
            checks_failed=fails2,
            retry_used=True,
            paid_used=True,
            channel=channel,
            route="paid",
            why="paid:" + ",".join(fails2)[:120],
        )
        return final, meta

    meta["route"] = "local_fail"
    log_decision(
        thread_id_hash=tid,
        checks_failed=fails2,
        retry_used=True,
        paid_used=False,
        channel=channel,
        route="local_fail",
        why="fail_no_paid_flag:" + ",".join(fails2)[:120],
    )
    return retried, meta


def second_pass(
    text: str,
    *,
    inbound: str = "",
    lookups: str = "",
    channel: str = "outlook",
    thread_id: str = "",
    dry_run: bool = False,
    rewrite_fn=None,
    paid_fn=None,
) -> str:
    final, _meta = gate(
        text,
        inbound=inbound,
        lookups=lookups,
        channel=channel,
        thread_id=thread_id,
        dry_run=dry_run,
        rewrite_fn=rewrite_fn,
        paid_fn=paid_fn,
    )
    return final
