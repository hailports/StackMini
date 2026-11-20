"""Find paisty's own earlier draft on screen, so a redraft replaces it instead of answering it.

After a draft is placed the reply box holds text paisty wrote, maybe edited by the user.
A redraft must not treat that text as the other person's message, must keep the user's edits,
and must replace the placed text rather than add a second reply under it.

    locate(field_text, draft) -> Placement   where the draft sits in the field, and how intact
    plan(field_text, draft)   -> dict         replace this range, or insert fresh
    strip_scene(doc, draft)   -> dict         the scene without the placed draft, plus prior_draft
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

EXACT = "exact"
EDITED = "edited"
PARTIAL = "partial"
NONE = "none"

_WS = re.compile(r"\s+")
# Reply headers in normalized (lowercased, single-spaced) text.
_HEADER = re.compile(r"(?:\bfrom: .{1,120}? (?:sent|date|to): |\bon .{4,90}? wrote:|-----original message|^> | > )")


@dataclass
class Placement:
    kind: str = NONE
    start: int = 0          # character offsets into the field text
    end: int = 0
    placed: str = ""        # the text now standing where the draft went (with the user's edits)
    coverage: float = 0.0   # share of the draft's words still there
    added: list[str] = field(default_factory=list)    # words the user added
    removed: list[str] = field(default_factory=list)  # words the user cut

    @property
    def found(self) -> bool:
        return self.kind != NONE


def _norm_map(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace and lowercase; keep a map from each normalized char to its source offset."""
    out: list[str] = []
    where: list[int] = []
    space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if out and not space:
                out.append(" ")
                where.append(i)
            space = True
            continue
        space = False
        out.append(ch.lower())
        where.append(i)
    if out and out[-1] == " ":
        out.pop()
        where.pop()
    return "".join(out), where


def _words(text: str) -> list[str]:
    return re.findall(r"[\w$%.,'’-]+", text.lower())


def _para_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Grow [start, end) to the paragraph around it, stopping at blank lines (quoted thread stays out)."""
    left = text.rfind("\n\n", 0, start)
    left = 0 if left < 0 else left + 2
    right = text.find("\n\n", end)
    right = len(text) if right < 0 else right
    # Never swallow a reply header or quote marker that sits in the same paragraph.
    head = re.search(r"(?im)^(?:on .{0,80}wrote:|from:\s|-----original message|>)", text[end:right])
    if head:
        right = end + head.start()
    return left, right


def locate(field_text: str, draft: str) -> Placement:
    field_text = field_text or ""
    draft = (draft or "").strip()
    if not draft or not field_text.strip():
        return Placement()
    nf, fmap = _norm_map(field_text)
    nd, _ = _norm_map(draft)
    if not nd:
        return Placement()
    at = nf.find(nd)
    if at >= 0:
        start = fmap[at]
        end = fmap[at + len(nd) - 1] + 1
        return Placement(EXACT, start, end, field_text[start:end], 1.0)
    matcher = difflib.SequenceMatcher(None, nd, nf, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size >= 4]
    if not blocks:
        return Placement()
    # Quoted text below can repeat the draft's words ("is the level 3 rule built in?"). Keep the
    # tightest run of shared pieces, and never cross into a reply header.
    stop = _HEADER.search(nf, nf.find(nd[:20]) if nf.find(nd[:20]) >= 0 else blocks[0].b)
    limit = stop.start() if stop else len(nf)
    clusters: list[list] = []
    for blk in blocks:
        if blk.b >= limit:
            continue
        if clusters and blk.b - (clusters[-1][-1].b + clusters[-1][-1].size) <= 40 + max(0, blk.a - (clusters[-1][-1].a + clusters[-1][-1].size)):
            clusters[-1].append(blk)
        else:
            clusters.append([blk])
    if not clusters:
        return Placement()
    blocks = max(clusters, key=lambda c: sum(b.size for b in c))
    matched = sum(b.size for b in blocks)
    longest = max(b.size for b in blocks)
    share = matched / len(nd)
    # The span in the field between the first and last shared run is where the draft sits.
    first, last = blocks[0], blocks[-1]
    s_norm, e_norm = first.b, last.b + last.size
    span = max(1, e_norm - s_norm)
    density = matched / span
    dw = set(_words(draft))
    if share >= 0.6 and density >= 0.45:
        kind = EDITED
    elif (share >= 0.3 or longest >= 60) and density >= 0.45 and longest >= 20:
        kind = PARTIAL
    else:
        return Placement()
    start = fmap[s_norm]
    end = fmap[e_norm - 1] + 1
    # Edits often run past the shared text: a new last sentence, a changed greeting.
    start, end = _para_bounds(field_text, start, end)
    placed = field_text[start:end].strip("\n")
    lead = len(field_text[start:end]) - len(field_text[start:end].lstrip("\n"))
    start += lead
    end = start + len(placed)
    pw = _words(placed)
    added = [w for w in pw if w not in dw]
    removed = [w for w in _words(draft) if w not in set(pw)]
    coverage = sum(1 for w in dw if w in set(pw)) / max(1, len(dw))
    if kind == EDITED and coverage >= 0.98 and not added:
        kind = EXACT
    return Placement(kind, start, end, placed, round(coverage, 3), added[:40], removed[:40])


def utf16(text: str, index: int) -> int:
    """Accessibility ranges count UTF-16 units, not characters."""
    return len(text[:index].encode("utf-16-le")) // 2


def plan(field_text: str, draft: str) -> dict:
    """What the app should do with the new text: replace the placed draft, or insert fresh."""
    spot = locate(field_text, draft)
    if not spot.found:
        return {"mode": "insert", "kind": NONE}
    return {
        "mode": "replace",
        "kind": spot.kind,
        "start": spot.start,
        "end": spot.end,
        "start16": utf16(field_text, spot.start),
        "length16": utf16(field_text, spot.end) - utf16(field_text, spot.start),
        "placed": spot.placed,
        "coverage": spot.coverage,
    }


def _strip_lines(text: str, draft: str) -> tuple[str, str]:
    """Drop OCR lines that are pieces of the draft. Returns (kept, dropped)."""
    nd, _ = _norm_map(draft)
    kept, dropped = [], []
    for line in (text or "").splitlines():
        nl, _ = _norm_map(line)
        if len(nl) >= 12 and (nl in nd or difflib.SequenceMatcher(None, nl, nd, autojunk=False).find_longest_match(
                0, len(nl), 0, len(nd)).size >= max(12, int(len(nl) * 0.8))):
            dropped.append(line)
        else:
            kept.append(line)
    return "\n".join(kept), "\n".join(dropped)


def strip_scene(doc: dict, draft: str) -> dict:
    """The scene as the other side wrote it: paisty's placed draft removed, and noted as prior_draft."""
    out = dict(doc or {})
    draft = (draft or "").strip()
    if not draft:
        return out
    prior: dict = {"original": draft, "kind": NONE, "placed": "", "added": [], "removed": []}
    field_text = str(out.get("field_text") or "")
    spot = locate(field_text, draft)
    if spot.found:
        out["field_text"] = (field_text[: spot.start] + field_text[spot.end:]).strip("\n")
        prior.update(kind=spot.kind, placed=spot.placed, added=spot.added, removed=spot.removed, where="field")
    for key in ("window_ocr", "screen_text"):
        if out.get(key):
            kept, dropped = _strip_lines(str(out[key]), draft)
            if dropped:
                out[key] = kept
                if not spot.found:
                    prior.update(kind=PARTIAL if len(dropped) < 3 else EDITED, placed=dropped, where=key)
    blocks = out.get("blocks")
    if isinstance(blocks, list):
        cleaned = []
        for block in blocks:
            text = str((block or {}).get("text") or "") if isinstance(block, dict) else ""
            hit = locate(text, draft) if text else Placement()
            if hit.found and len(hit.placed) >= 0.8 * len(text.strip()):
                if not prior["placed"]:
                    prior.update(kind=hit.kind, placed=hit.placed, where="blocks")
                continue
            cleaned.append(block)
        out["blocks"] = cleaned
    out["prior_draft"] = prior
    return out


def prior_note(prior: dict | None) -> str:
    """The instruction block that tells the model which text is its own earlier draft."""
    if not prior or not prior.get("original"):
        return ""
    placed = str(prior.get("placed") or "").strip()
    kind = prior.get("kind") or NONE
    lines = ["YOUR EARLIER DRAFT. You wrote this, not them. Never reply to it or quote it as their message."]
    if placed and kind != NONE:
        lines.append("It is already in the reply box" + (" and the user edited it." if kind != EXACT else ", unchanged."))
        lines.append("In the reply box now:\n" + placed[:4000])
        if kind != EXACT:
            lines.append("Your original:\n" + str(prior["original"])[:4000])
            lines.append(
                "Keep every change the user made: words they added, facts, names, numbers and tone they changed, "
                "and anything they cut stays out. Improve the rest. Return the whole reply that should replace it."
            )
        else:
            lines.append("The user asked for a redraft: write a better reply to the same message, not a copy of this one.")
    else:
        lines.append(str(prior["original"])[:4000])
        lines.append(
            "It is not in the reply box. If it answers this same conversation, write a better version, not a copy. "
            "If the conversation on screen is a different one, ignore it."
        )
    return "\n".join(lines)
