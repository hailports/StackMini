"""wiki_core: generic helpers for a compiled, interlinked markdown wiki.

Pattern: raw sources -> deterministic FACTS per entity -> a local model writes a
short SUMMARY from those facts -> pages carry links, backlinks and flags
(STALE, CHANGED, CONTRADICTION). Queries read compiled pages first and fall
back to raw retrieval only when no page matches.

No I/O to any model or network here. Pure functions plus small file helpers,
so the compile and query layers stay testable.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

PAGE_TYPES = ("person", "project", "ticket", "week", "system", "decision")
LINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9\-]*)\]\]")
_FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
DAY = 86400.0


def slugify(name: str, prefix: str = "") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:64].strip("-") or "x"
    return f"{prefix}{s}" if prefix else s


def fact_hash(facts: list[str]) -> str:
    """Stable hash of the fact lines. Order matters (facts are pre-sorted by the caller)."""
    h = hashlib.sha256()
    for f in facts:
        h.update(f.strip().encode("utf-8", "replace"))
        h.update(b"\n")
    return h.hexdigest()[:16]


@dataclass
class Page:
    slug: str
    type: str
    title: str
    aliases: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    summary: str = ""
    next_steps: str = ""
    flags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    backlinks: list[str] = field(default_factory=list)
    newest_ts: float = 0.0
    compiled_at: float = 0.0
    fact_hash: str = ""
    summary_hash: str = ""  # fact_hash the summary was written from
    state: dict = field(default_factory=dict)  # key -> value facts used for CHANGED diffs

    def meta(self) -> dict:
        return {
            "slug": self.slug, "type": self.type, "title": self.title,
            "aliases": self.aliases, "newest_ts": round(self.newest_ts, 1),
            "compiled_at": round(self.compiled_at, 1), "fact_hash": self.fact_hash,
            "summary_hash": self.summary_hash, "flags": len(self.flags),
        }


def _day(ts: float) -> str:
    if not ts:
        return "unknown"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def render(p: Page) -> str:
    fm = json.dumps(p.meta(), ensure_ascii=False)
    out = [f"---\n{fm}\n---", f"# {p.title}", ""]
    out.append(f"type: {p.type} | newest source: {_day(p.newest_ts)} | compiled: {_day(p.compiled_at)}")
    if p.aliases:
        out.append("aliases: " + ", ".join(p.aliases[:12]))
    out.append("")
    if p.flags:
        out.append("## flags")
        out += [f"- {f}" for f in p.flags]
        out.append("")
    if p.summary:
        stale = " (facts changed since; summary pending recompile)" if p.summary_hash and p.summary_hash != p.fact_hash else ""
        out += [f"## summary{stale}", p.summary.strip(), ""]
    if p.next_steps:
        out += ["## next steps", p.next_steps.strip(), ""]
    out.append("## facts")
    out += [f"- {f}" for f in p.facts] or ["- (none)"]
    out.append("")
    if p.links:
        out += ["## links", " ".join(f"[[{s}]]" for s in p.links), ""]
    if p.backlinks:
        out += ["## backlinks", " ".join(f"[[{s}]]" for s in p.backlinks), ""]
    return "\n".join(out).rstrip() + "\n"


def parse_meta(md: str) -> dict:
    m = _FM_RE.match(md or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def section(md: str, name: str) -> str:
    m = re.search(rf"^## {re.escape(name)}[^\n]*\n(.*?)(?=^## |\Z)", md or "", re.S | re.M)
    return m.group(1).strip() if m else ""


def backlinks(links: dict[str, list[str]]) -> dict[str, list[str]]:
    """links: slug -> outgoing slugs. Returns slug -> sorted incoming slugs (only known slugs)."""
    known = set(links)
    back: dict[str, set[str]] = {s: set() for s in known}
    for src, outs in links.items():
        for dst in outs:
            if dst in known and dst != src:
                back[dst].add(src)
    return {k: sorted(v) for k, v in back.items()}


def staleness_flags(newest_ts: float, now: float, max_age_days: float,
                    source_ages: dict[str, float] | None = None, source_max_days: float = 14) -> list[str]:
    """STALE when the newest fact is older than max_age_days, or a feeding source is old."""
    flags = []
    if not newest_ts:
        flags.append("STALE: no dated source found for this page")
    elif now - newest_ts > max_age_days * DAY:
        flags.append(f"STALE: newest source is {int((now - newest_ts) // DAY)} days old ({_day(newest_ts)})")
    for name, ts in sorted((source_ages or {}).items()):
        if ts and now - ts > source_max_days * DAY:
            flags.append(f"STALE SOURCE: {name} last compiled {_day(ts)} ({int((now - ts) // DAY)} days)")
    return flags


def diff_state(prev: dict, cur: dict) -> list[str]:
    """CHANGED lines for keys whose value moved between compiles."""
    out = []
    for k in sorted(set(prev) & set(cur)):
        if str(prev[k]) != str(cur[k]):
            out.append(f"CHANGED: {k}: {prev[k]} -> {cur[k]}")
    for k in sorted(set(cur) - set(prev)):
        if prev:
            out.append(f"NEW: {k}: {cur[k]}")
    return out


def claim_conflicts(claims: dict[str, str], live: dict[str, str], claim_src: str, live_src: str) -> list[str]:
    """CONTRADICTION when two sources state different values for the same key."""
    out = []
    for k in sorted(set(claims) & set(live)):
        a, b = str(claims[k]).strip(), str(live[k]).strip()
        if a and b and a.lower() != b.lower():
            out.append(f"CONTRADICTION: {k}: {claim_src} says '{a}', {live_src} says '{b}'")
    return out


def parse_model_page(text: str) -> tuple[str, str, list[str]]:
    """Split a model reply into (summary, next_steps, contradictions).

    Expected headers: SUMMARY:, NEXT:, CONTRADICTIONS:. Missing headers -> whole text is summary.
    'none' contradictions -> [].
    """
    t = (text or "").strip()
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()
    parts = re.split(r"(?im)^\s*(SUMMARY|NEXT(?: STEPS)?|CONTRADICTIONS)\s*:\s*", t)
    if len(parts) < 3:
        return t, "", []
    got: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].upper().split()[0]
        got[key] = parts[i + 1].strip()
    con = []
    for line in got.get("CONTRADICTIONS", "").splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if line and not re.match(r"(?i)^(none|no contradictions|n/?a)\b", line):
            con.append(line)
    return got.get("SUMMARY", "").strip(), got.get("NEXT", "").strip(), con


def match_pages(question: str, index: list[dict], limit: int = 3) -> list[tuple[str, int]]:
    """Rank pages by alias/title hits in the question. index rows: {slug, title, aliases, type}.

    Whole-word, case-insensitive. Longer aliases score more, so 'billing portal' beats 'billing'.
    Aliases shorter than 3 chars are ignored (too noisy) unless they contain a digit.
    """
    q = " " + re.sub(r"\s+", " ", (question or "").lower()) + " "
    scored = []
    for row in index:
        best = 0
        hits = 0
        for a in [row.get("title", "")] + list(row.get("aliases") or []):
            a = (a or "").strip().lower()
            if len(a) < 3 and not re.search(r"\d", a):
                continue
            if re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", q):
                hits += 1
                best = max(best, len(a))
        if hits:
            scored.append((row["slug"], best * 10 + hits))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:limit]


def pack(pages: list[str], budget_chars: int) -> str:
    """Concatenate page texts under a char budget. Facts get cut from the bottom first."""
    out, used = [], 0
    for md in pages:
        body = _FM_RE.sub("", md, count=1).strip()
        room = budget_chars - used
        if room < 400:
            break
        if len(body) > room:
            body = body[: room - 20].rsplit("\n", 1)[0] + "\n(...cut)"
        out.append(body)
        used += len(body) + 2
    return "\n\n".join(out)


def compile_gate(*, paused: bool, healthy: bool, throttle_level: str, interactive: bool) -> tuple[bool, str]:
    """Decide whether a background model compile may run now."""
    if paused:
        return False, "model maintenance pause flag present"
    if not healthy:
        return False, "local model endpoint not healthy"
    if (throttle_level or "").lower() == "critical":
        return False, "memory throttle critical"
    if interactive:
        return False, "interactive chat in flight"
    return True, "ok"


def page_budget(max_pages: int, throttle_rate: float) -> int:
    """Pages a pass may recompile with the model, scaled by the background throttle rate."""
    try:
        r = float(throttle_rate)
    except (TypeError, ValueError):
        r = 1.0
    r = min(max(r, 0.0), 1.0)
    return int(max_pages * r)


def write_atomic(path: Path, text: str) -> bool:
    """Write only when content changed. Returns True when written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8", errors="replace") == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True
