"""2026-11-04 operator [pt329u] "horrible post": STRANGER TEST for @hailports originals.
A post must be understandable to a stranger scrolling X in one read: a clear subject, no unexplained in-house
UI terms ('the review tab', 'the risky field'), no metaphor standing in for the point. Hard floor FLOOR.
Deterministic hits are an instant fail; otherwise one judge call (free pool; cached by text sha).
Returns (ok, why). why starts with 'clarity_unavailable' when no judge answered (callers HOLD, never post)."""
from __future__ import annotations
import hashlib, json, re
from pathlib import Path

FLOOR = 7
LANES = {"x_hailports"}
_CACHE = Path("/home/operator/claude-stack/data/runtime/clarity_cache.jsonl")
_HARD = [
    (re.compile(r"scavenger hunt|make me hunt|tab hunt", re.I), "metaphor:scavenger_hunt"),
    # 'the review tab / the approval screen / the risky field' with no product named = someone's work UI
    (re.compile(r"^\W*the (?:review|approval|admin|settings|change|audit|diff) (?:tab|screen|page|queue|panel|view)\b", re.I),
     "opens_on_unexplained_ui"),
    (re.compile(r"\bthe risky field\b|\bproposed change\b.*\bthe button\b", re.I), "unexplained_work_ui"),
]
RUBRIC = (
    "STRANGER TEST. A stranger scrolling X sees this post from an account they have never seen, with no image and "
    "no context. Would they understand what this is about in one read?\n"
    "Score 0-10. 8+ only if the subject (what tool, system or situation) is obvious from the words alone and every "
    "term is common knowledge for developers.\n"
    "Score 4 or lower if ANY: no clear subject; in-house or unnamed UI terms ('the review tab', 'the risky field', "
    "'the button') that the reader has to guess at; a metaphor stands in for the actual point; it reads like a "
    "fragment of someone's private work notes; you had to re-read it to get it.\n"
    'Return STRICT JSON only: {"why": "one clause", "score": 0-10}'
)


def _sha(t: str) -> str:
    return hashlib.sha256(" ".join((t or "").split()).lower().encode()).hexdigest()


def _cached(k: str):
    try:
        if _CACHE.exists():
            for l in reversed(_CACHE.read_text().splitlines()[-4000:]):
                if k in l:
                    d = json.loads(l)
                    if d.get("k") == k:
                        return int(d["s"]), str(d.get("w", ""))
    except Exception:
        pass
    return None


def hard_hit(text: str) -> str | None:
    body = " ".join(re.sub(r"https?://\S+", "", text or "").split())
    for rx, why in _HARD:
        if rx.search(body):
            return why
    return None


CTX_RUBRIC = (  # 2026-11-04 [pt346u]: thread replies are read in thread order, judged in context
    "STRANGER TEST FOR A THREAD REPLY. A stranger opened this X thread and read the earlier posts shown as THREAD SO "
    "FAR, then reads REPLY. Judge ONLY the reply: does it make sense to that stranger given what came before?\n"
    "Score 0-10. 8+ if the reply's subject and terms are clear from the thread so far plus common developer knowledge. "
    "Score 4 or lower if it uses terms neither the thread nor common knowledge explain, a metaphor stands in for the "
    "point, or it reads like private work notes.\n"
    'Return STRICT JSON only: {"why": "one clause", "score": 0-10}'
)


def score(text: str, lane: str = "x_hailports", context: list | None = None) -> tuple[int | None, str]:
    body = " ".join(re.sub(r"https?://\S+", "", text or "").split()).strip()
    if not body:
        return 0, "empty"
    if (h := hard_hit(body)):
        return 0, "hard:" + h
    ctx = [" ".join(re.sub(r"https?://\S+", "", c or "").split()) for c in (context or []) if (c or "").strip()]
    k = _sha(body + ("||ctx||" + "||".join(ctx) if ctx else ""))
    if (c := _cached(k)) is not None:
        return c
    try:
        from core.caption_brain import brain_complete_sync
        prompt = (f"{CTX_RUBRIC}\n\nTHREAD SO FAR:\n" + "\n---\n".join(ctx) + f"\n\nREPLY:\n\"{body}\"") if ctx \
            else f"{RUBRIC}\n\nPOST:\n\"{body}\""
        raw, _p = brain_complete_sync(prompt, system=None, max_tokens=120,
                                      lane=f"{lane}:clarity_judge", timeout=240)
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if not m:
            return None, "clarity_unavailable:unparsed"
        d = json.loads(m.group(0))
        s = int(d.get("score", 0)); w = str(d.get("why", ""))[:160]
    except Exception as exc:  # noqa: BLE001
        return None, f"clarity_unavailable:{type(exc).__name__}"
    try:
        with _CACHE.open("a") as fh:
            fh.write(json.dumps({"k": k, "s": s, "w": w}) + "\n")
    except Exception:
        pass
    return s, w


_WRONG_NUM = re.compile(r"\b94\s?%|\b94 ?percent\b|\bninety.?four percent\b", re.I)  # 2026-11-04: the real cut is 91% ($42.50 -> $3.90/day)


def check(lane: str, text: str, context: list | None = None, floor: int | None = None) -> tuple[bool, str]:
    if lane not in LANES:
        return True, "n/a"
    from core.identity_gate import check as _idc  # 2026-11-04 [pt332u] hard blocks (job title, employer, industry...)
    if (idr := _idc(text or "")):
        return False, ",".join(idr)
    if _WRONG_NUM.search(text or ""):
        return False, "wrong_number:94% (the real figure is 91%, $42.50 -> $3.90/day)"
    s, w = score(text, lane, context)
    if s is None:
        return False, w
    need = FLOOR if floor is None else floor
    return (s >= need), f"clarity:{s}:{w[:80]}"


def check_thread(lane: str, parts: list, floor: int | None = None) -> tuple[bool, str]:
    """2026-11-04 [pt346u]: main post judged alone; each reply judged with the main post + earlier replies as context."""
    parts = [p for p in (parts or []) if (p or "").strip()]
    whys = []
    for i, p in enumerate(parts):
        ok, why = check(lane, p, parts[:i] or None, floor=floor)
        whys.append(f"{i}:{why}")
        if not ok:
            return False, f"part{i}:{why}"
    return True, " | ".join(w[:40] for w in whys)
