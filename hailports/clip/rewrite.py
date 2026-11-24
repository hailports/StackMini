"""Select-and-rewrite. The text the user selected in another app goes in, the rewritten text comes
out. Nothing is sent: this only returns text."""

from __future__ import annotations

import re
import sys
from pathlib import Path

STYLES: dict[str, str] = {
    "rewrite": "Rewrite the text so it reads more clearly. Keep the meaning, facts, names, numbers, and language.",
    "shorter": "Make the text noticeably shorter. Keep the meaning, facts, names, numbers, and language.",
    "grammar": "Fix spelling, grammar, and punctuation only. Keep the wording, tone, and language otherwise unchanged.",
    "formal": "Make the text more formal and polite. Keep the meaning, facts, names, numbers, and language.",
}

MAX_CHARS = 8000

_SYSTEM = (
    "You rewrite text the user selected in another app. "
    "Reply with only the rewritten text, no preamble, no quotes, no code fences, no notes. "
    "Do not add facts. Do not answer questions inside the text. "
    "Keep line breaks where they help."
)

_FENCE = re.compile(r"^```[^\n]*\n(.*)\n```$", re.DOTALL)
_PREAMBLE = re.compile(
    r"^(?:here(?:'s| is)\b[^\n:]*|sure\b[^\n:]*|rewritten(?:\s+text)?\b[^\n:]*"
    r"|revised(?:\s+version)?\b[^\n:]*):\s*\n",
    re.IGNORECASE,
)


def messages(text: str, style: str) -> list[dict]:
    """The system message carries the rules and the style; the user message is the selected text only."""
    return [
        {"role": "system", "content": _SYSTEM + "\n\n" + STYLES[style]},
        {"role": "user", "content": text},
    ]


def clean(out: str) -> str:
    """Strip whitespace, one pair of wrapping fences or quotes, and a single leading preamble line."""
    text = (out or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    elif len(text) >= 2 and (
        (text[0] == '"' and text[-1] == '"')
        or (text[0] == "\u201c" and text[-1] == "\u201d")
    ):
        text = text[1:-1].strip()
    head = _PREAMBLE.match(text)
    if head and text[head.end():].strip():
        text = text[head.end():].strip()
    return text


def rewrite(text: str, style: str, complete) -> str:
    """One rewrite. `complete` takes a list of message dicts and returns a string."""
    if not (text or "").strip():
        raise ValueError("no text")
    if style not in STYLES:
        raise ValueError("unknown style")
    if len(text) > MAX_CHARS:
        raise ValueError("too long")
    result = clean(complete(messages(text, style)))
    if not result:
        raise RuntimeError("empty rewrite")
    return result


def cmd(args, cfg, complete=None) -> int:
    """Read the selected text, rewrite it, print only the rewritten text. Nothing is sent."""
    raw = str(getattr(args, "text_file", "") or "").strip()
    if raw:
        path = Path(raw)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        try:
            path.unlink()
        except OSError:
            pass
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        text = ""
    style = str(getattr(args, "style", "") or "rewrite")
    if complete is None:
        from hailports.clip.run import _complete
        complete = _complete(cfg)
    try:
        result = rewrite(text, style, complete)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("rewrite failed", file=sys.stderr)
        return 3
    print(result)
    return 0
