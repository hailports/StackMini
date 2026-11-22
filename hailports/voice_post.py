"""Deterministic voice pass for drafts. No model calls. Drafts only.

Public-safe copy of the Mini draft voice filter (2026-11-16):
lowercase the body (keep emails/urls/IDs), strip sign-offs.
"""
from __future__ import annotations

import re

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

_FORBIDDEN_DASH = re.compile(r"—|–|--")

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
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
        return t
    except Exception:
        return text or ""


def sanitize_dashes(text: str) -> tuple[str, str]:
    lines = (text or "").splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines):
        lines[idx] = re.sub(
            r"(?i)^(\s*(?:hey|hi|hello|dear)\s+[A-Za-z][A-Za-z'.-]*)\s*(?:--|—|–)\s*",
            lambda m: m.group(1).rstrip() + ",",
            lines[idx],
            count=1,
        )
        lines[idx] = re.sub(r",(\S)", r", \1", lines[idx], count=1)
    new = "\n".join(lines)
    if _FORBIDDEN_DASH.search(new):
        return new, "dash_left"
    return new, ""


def apply(text: str, sender_name: str = "", inbound: str = "", extra_names: list | None = None) -> str:
    """Public draft voice pass + failure-gated paid second pass hook."""
    del extra_names  # kept for API compatibility
    raw, _why = sanitize_dashes(text or "")
    raw = post_filter(raw)
    if sender_name:
        tok = re.sub(r"[^A-Za-z]", "", str(sender_name).split()[0] if str(sender_name).split() else "")
        if tok and len(tok) >= 2:
            raw = re.sub(
                r"(?i)^(\s*hey\s+)([A-Za-z][A-Za-z'.-]*)",
                lambda m: m.group(1).lower() + tok.lower(),
                raw,
                count=1,
            )
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    # Paid only when local draft fails checks (flag = paid allowed on failure).
    try:
        from hailports.draft_voice_harden import second_pass

        raw = second_pass(raw, inbound=inbound or "", channel="outlook")
    except Exception:
        pass
    return raw


def _self_test() -> None:
    got = apply(
        "Hey Pat,\n\nThe link is https://example.com/X and mail noreply@hailports.com. Best regards,\nAl" + "ex",
        sender_name="Pat",
    )
    assert got.startswith("hey pat,"), got
    assert "https://example.com/X" in got
    assert "noreply@hailports.com" in got
    assert "best regards" not in got.lower()


if __name__ == "__main__":
    _self_test()
    print("voice_post ok")
