"""Viral-reason gate (operator 2026-10-20 07:16 CT): every original must DRIVE AWARENESS/URGENCY, carry VALUE,
and have a SHARE TRIGGER. Free-model judge (lane name contains 'judge' -> caption_brain keeps it off Cursor).
Originals floor 8, replies use the lighter floor (6, value + share only). Every decision is logged with the
three named reasons to data/hustle/logs/viral_reason.jsonl. Fail closed: unparseable/unreachable = 0."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

LOG = Path("/home/operator/claude-stack/data/hustle/logs/viral_reason.jsonl")
FLOOR_ORIGINAL = 8
FLOOR_REPLY = 6

_LANE_CTX = {
    "x_ima": "Ines Fiends: satire about ADHD, stimulants, caffeine and big-corp / big-tech office life.",
    "x_hailports": "hailports: anonymous builder; helpful AI/automation/local-LLM thought leadership and a Mac mini home-server build log.",
    "x_darkgary": "DarkGary: Texas A&M / Houston sports doom humor, real scores only.",
}

_RUBRIC = (
    "You are a viral-content editor for X. Decide whether this {kind} has a REAL reason to spread.\n"
    "Account: {ctx}\n"
    "Name all three, concretely, or score it low:\n"
    " 1. URGENCY/AWARENESS: why read it NOW (today's news in the source, a live/upcoming game, a pain people "
    "feel this week, or a 'you're doing this wrong' angle). Fake urgency (made-up deadline, invented stat, "
    "'breaking' not backed by the SOURCE) = cap 3.\n"
    " 2. VALUE: what the reader gets (a specific screenshot-worthy laugh, a usable tip, a real number from the "
    "source, a contrarian insight). Vibes-only captions = cap 4.\n"
    " 3. SHARE TRIGGER: why someone reposts, quotes or tags a friend (identity 'this is so me', fair argument "
    "bait, save-worthy utility, 'tag your coworker').\n"
    "Also: would a human screenshot this? Does any line sound like a bot/brand (essay register, tidy aphorism, "
    "corporate filler)? Either problem caps at 6.\n"
    "{media}"
    "{source}"
    "POST:\n\"{text}\"\n\n"
    "SATIRE NOTE: an obvious joke (absurd personification, exaggeration, fake memo/ticket formats) is NOT a fake fact. "
    "For jokes, URGENCY = a pain readers feel this week/today (the 3pm crash, a standup that should have been an email, protecting cali sober sleep, "
    "a game this weekend). For engineering/ops posts (hailports), URGENCY = a recurring production, demo, or "
    "integration failure mode builders hit every week (no calendar deadline or numbered list required; plain "
    "sentences with a concrete, stealable habit or failure mode devs send to their team score 8+). Only penalize "
    "invented NEWS, STATS or DEADLINES presented as real.\n"
    "CALIBRATION: 'coffee is my personality' -> 3 (no value, no trigger). 'build log #13. fact gates check every "
    "number...' -> 5 (useful-ish, no urgency, nothing to share). 'my adderall clocked out at 2:58 and left me alone "
    "with a spreadsheet that still has feelings' -> 8 (urgency: the afternoon med crash people hit daily; value: "
    "specific screenshot laugh; share: 'this is so me'). 'stop signing that renewal. 4 checks for a real exit door: ...' "
    "-> 9 (urgency: renewal season mistake; value: usable checklist; share: save/forward to your team). "
    "'packers 23, cowboys 13. last super bowl they won you still waited on a pager.' -> 8 (live result; specific "
    "burn; rivals argue).\n"
    "Score 1-10 (8 = all three are clearly present; 10 = you'd share it yourself). "
    'Return STRICT JSON only: {{"urgency": "...", "value": "...", "share": "...", "score": n}}'
)

_RUBRIC_REPLY = (
    "You judge one reply on X. Lighter bar: does it ADD VALUE to the conversation (a specific joke, a useful "
    "point, a real number) and give the reader a reason to like/answer it (relatable, fair debate, helpful)? "
    "Generic agreement or bot-sounding filler = 3.\nAccount: {ctx}\nCONTEXT:\n{source}\nREPLY:\n\"{text}\"\n"
    'Return STRICT JSON only: {{"urgency": "...", "value": "...", "share": "...", "score": n}}'
)


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def judge(text: str, lane: str, kind: str = "post", source: str = "", media: str = "",
          reply: bool = False, ref: str = "", floor: int | None = None) -> tuple[int, dict]:
    base = lane.split(":")[0]
    ctx = _LANE_CTX.get(base, base)
    if reply:
        prompt = _RUBRIC_REPLY.format(ctx=ctx, source=(source or "(none)")[:900], text=(text or "")[:600])
    else:
        prompt = _RUBRIC.format(
            kind=kind, ctx=ctx, text=(text or "")[:1500],
            media=(f"MEDIA ATTACHED: {media}\n" if media else ""),
            source=(f"SOURCE (only facts here are real): {source[:700]}\n" if source else "SOURCE: none (no news/stat claims allowed)\n"))
    try:
        from core.caption_brain import brain_complete_sync
        raw, prov = brain_complete_sync(prompt, max_tokens=220, lane="viral_judge:" + {"x_ima": "ines", "x_hailports": "hp", "x_darkgary": "dg"}.get(base, "x"))
    except Exception as exc:  # noqa: BLE001
        raw, prov = "", f"err:{type(exc).__name__}"
    d = _parse(raw or "")
    try:
        sc = int(d.get("score", 0))
    except Exception:
        sc = 0
    reasons = {k: str(d.get(k, "") or "")[:160] for k in ("urgency", "value", "share")}
    if not reply and not all(len(v.strip()) >= 4 and not re.match(r"^(none|n/?a|missing|no\b)", v.strip(), re.I)
                             for v in reasons.values()):
        sc = min(sc, 5)  # all three must be NAMED
    if floor is None:
        floor = FLOOR_REPLY if reply else FLOOR_ORIGINAL
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "lane": base, "kind": "reply" if reply else kind,
           "ref": ref, "score": sc, "pass": sc >= floor, "reasons": reasons, "provider": prov,
           "text": " ".join((text or "").split())[:280]}
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return sc, reasons


def passes(text: str, lane: str, kind: str = "post", source: str = "", media: str = "",
           reply: bool = False, ref: str = "", votes: int = 2, floor: int | None = None) -> tuple[bool, int, dict]:
    """Free judges are noisy: originals get up to 2 reads, pass on any read >= floor only if the other is >= floor-1.
    floor overrides the original floor for this call. The default stays FLOOR_ORIGINAL (8)."""
    if floor is None:
        floor = FLOOR_REPLY if reply else FLOOR_ORIGINAL
    s1, r1 = judge(text, lane, kind, source, media, reply, ref, floor=floor)
    if s1 >= floor or reply or votes < 2 or s1 < floor - 1:
        return s1 >= floor, s1, r1
    s2, r2 = judge(text, lane, kind, source, media, reply, ref, floor=floor)
    return (s2 >= floor), max(s1, s2), (r2 if s2 >= s1 else r1)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "/home/operator/claude-stack")
    print(passes(sys.argv[2], sys.argv[1]))
