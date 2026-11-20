"""Local engine first, with a quality gate, then the premium engines.

When the saved order starts with local engines (http or ssh) and has a premium engine
(cursor, claude, codex) after them, a draft runs on the local engines only. The result
must pass the gate: the length cap, "hey <First>," then the answer, no markdown, no
number or list dumps, and "best," with the first name on email. A failed gate gets one
shorter rewrite on the same local engines. If that fails too, or the local engine is
down, busy or slow, the whole draft runs again on the premium engines. The engine that
produced each draft is written to engine-log.jsonl next to drafts.jsonl. Nothing is sent.
"""

from __future__ import annotations

import copy as _copy
import json
import os
import re
import time
from pathlib import Path

from hailports.clip import shape

PREMIUM = {"cursor", "claude", "codex"}
_NUM = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?(?![\w])")
_LIST = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*•])\s+")
_MD = re.compile(r"(?m)^\s*(?:#{1,6}\s|\*\*|- \*\*|\|.*\|)")


def _via(spec: dict) -> str:
    return str((spec or {}).get("via") or "http").strip().lower()


def split(cfg: dict | None) -> tuple[dict, dict] | None:
    """(local-only route, premium-only route), or None when the order is not local-then-premium."""
    paiste = (cfg or {}).get("paiste") or {}
    models = paiste.get("models") or {}
    order = [str(n) for n in (paiste.get("order") or []) if isinstance(models.get(n), dict)]
    if not order or _via(models[order[0]]) in PREMIUM:
        return None
    local = [n for n in order if _via(models[n]) not in PREMIUM]
    premium = [n for n in order if _via(models[n]) in PREMIUM]
    if not local or not premium:
        return None

    def only(names: list[str]) -> dict:
        out = _copy.deepcopy(cfg)
        out["paiste"]["order"] = list(names)
        out["paiste"]["preferred"] = names[0]
        return out

    return only(local), only(premium)


def kind_cap(doc: dict | None) -> tuple[str, int]:
    kind = shape.surface(doc)
    incoming = shape.words(shape.newest_message(str((doc or {}).get("field_text") or "")))
    _target, cap = shape.word_cap(kind, incoming)
    return kind, cap


def gate(text: str, kind: str, cap: int, me: list[str] | None = None) -> list[str]:
    """Problems that send a local draft to a rewrite, then to premium. Empty means it passes."""
    body = (text or "").strip()
    if not body:
        return ["empty"]
    core = shape.strip_close(body, me)
    n = shape.words(core)
    out = []
    if n > cap:
        out.append(f"too long: {n} words, at most {cap}")
    if kind == "email" and n < 25:
        out.append(f"too thin: {n} words")
    if not re.match(r"(?i)^hey\s+[A-Za-z][\w'.-]*,", body):
        out.append('does not open with "hey <first name>,"')
    if kind == "email":
        first = ((me or [""])[0].split() or [""])[0]
        if first and not re.search(r"(?i)\nbest,\n" + re.escape(first) + r"\s*$", body):
            out.append('no "best,\\n' + first + '" sign-off')
    if _MD.search(body):
        out.append("markdown")
    nums = {m.group(0) for m in _NUM.finditer(_LIST.sub("", core))}
    if len(nums) > 6:
        out.append(f"number dump: {len(nums)} figures, keep one or two key numbers")
    if len(_LIST.findall(core)) > 5:
        out.append("list dump: more than 5 list lines, keep 2 to 4 steps")
    return out


def shorter_messages(text: str, probs: list[str], kind: str, cap: int, me_first: str) -> list[dict]:
    target = min(cap, 120) if kind == "email" else min(cap, 45)
    return [
        {"role": "system", "content": shape.rules(kind, target, cap, me_first)},
        {"role": "user", "content": "Rewrite this reply so it fixes: " + "; ".join(probs)
         + f". At most {target} words. Keep the facts, the answer and the ask. Plain text, only the reply.\n\n"
         + text},
    ]


def log_path() -> Path:
    raw = os.environ.get("PAISTE_ENGINE_LOG", "")
    return Path(raw) if raw else Path.home() / ".config" / "paiste" / "engine-log.jsonl"


def log(row: dict, path: Path | None = None) -> None:
    p = path or log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **row}
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.chmod(p, 0o600)
    except OSError:
        pass
