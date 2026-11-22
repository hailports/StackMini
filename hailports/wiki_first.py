"""wiki_first: fail-open "compiled wiki first" context for a chat turn.

A compile job writes entity pages (see wiki_core) plus a state.json index. At chat time,
wiki_prefix() picks the pages a question names and returns their compiled layer
(flags, summary, next steps, top facts) so the caller can PREPEND it to the normal
retrieval context. It never replaces retrieval: every source the turn already sees
stays in the prompt, and any miss, error, stale index or off switch returns "".

Off switches: env WIKI_FIRST=0, or an off-flag path passed by the caller.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import wiki_core as W

SUB = {"person": "people", "project": "projects", "ticket": "tickets", "week": "weeks",
       "decision": "decisions", "system": "systems"}


def _state(wiki_dir: Path) -> dict:
    try:
        return json.loads((wiki_dir / "state.json").read_text())
    except Exception:
        return {}


def select(question: str, st: dict, limit: int = 3) -> list[str]:
    pages = st.get("pages", {})
    ix = [{"slug": s, "title": p.get("title", s), "aliases": p.get("aliases", []), "type": p.get("type")}
          for s, p in pages.items() if p.get("type") in ("person", "project", "decision", "system")]
    return [s for s, _ in W.match_pages(question, ix, limit=limit)]


def _trim(md: str, room: int, facts: bool = True) -> str:
    body = re.sub(r"\A---\n.*?\n---\n", "", md, count=1, flags=re.S)
    head = body.split("## facts", 1)[0].strip()
    if not facts:
        return head[:room]
    f = W.section(body, "facts")
    fr = max(0, room - len(head) - 40)
    if len(f) > fr:
        f = f[:fr].rsplit("\n", 1)[0] + "\n(...more facts on the page)"
    return (head + ("\n## facts\n" + f if f else ""))[:room]


def wiki_prefix(question: str, wiki_dir: Path, budget: int = 3000, *, facts: bool = True,
                max_age_s: float = 4 * 86400, off_flag: Path | None = None,
                blocked=None, mask=None) -> str:
    """Compiled pages for `question`, or "" on miss/error/stale/off.

    blocked(question) -> truthy refuses the whole lookup (e.g. a data-line policy).
    mask(text) -> text scrubs secrets before the text leaves this function.
    """
    try:
        if os.environ.get("WIKI_FIRST", "1") == "0" or (off_flag and Path(off_flag).exists()):
            return ""
        if not (question or "").strip() or (blocked and blocked(question)):
            return ""
        st = _state(Path(wiki_dir))
        if time.time() - float(st.get("compiled_at") or 0) > max_age_s:
            return ""
        parts, used = [], 0
        slugs = select(question, st)
        for i, s in enumerate(slugs):
            typ = st["pages"][s].get("type", "")
            p = Path(wiki_dir) / SUB.get(typ, "other") / f"{s}.md"
            if not p.exists():
                continue
            room = budget - used
            if room < 400:
                break
            txt = _trim(p.read_text(errors="replace"), max(400, room // (len(slugs) - i)), facts=facts)
            if not facts and not re.search(r"^## (summary|next steps|flags)", txt, re.M):
                continue
            parts.append(f"=== WIKI PAGE [[{s}]] ===\n{txt}")
            used += len(txt) + 30
        if not parts:
            return ""
        text = "\n\n".join(parts) + "\n(wiki = compiled memory; live sources win where they disagree.)"
        return mask(text) if mask else text
    except Exception:
        return ""
