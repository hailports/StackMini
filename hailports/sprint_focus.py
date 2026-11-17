"""Focus-aware, local-only helpers for a biweekly sprint plan note.

Everything here is pure: no network, no tracker writes. The caller reads its
tracker and its local notes index, then uses these helpers to

* load an editable focus config (focus areas, de-prioritized areas,
  out-of-office windows, who work may be pulled from),
* scale the owner's cap by working days in the target sprint,
* propose tickets for the owner to pick up (suggestions only, never assigned),
* diff a plan against the previous one and fail closed when nothing changed,
* accept freshly worded model text only when every number in it is grounded.

Model and tracker endpoints must be loopback; a hosted URL is refused.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
PLAN_TYPES = {"user story", "bug", "product backlog item", "story"}
DONE = {"Closed", "Resolved", "Done", "Removed", "Cut"}

DEFAULT_FOCUS = {
    "owner": "me",
    "manager": "",
    "cap": 8,
    "focus": [],
    "deprioritize": [],
    "pull_from": [],
    "out_of_office": [],
    "ktlo": {"types": ["Bug"], "max_priority": 2, "max_unpointed": 2},
    "max_unpointed_commit": 3,
    "max_stretch": 3,
    "test_ticket_pattern": r"^\s*test\b|\btest\s*$|safe to delete",
}

# derived notes (prep cards, study notes, enrichment, invites) are not new priorities
NOISE_TITLE = re.compile(
    r"(?i)^\[local|idlefill|work enrich|study note|^ado \d|^\[?(user story|bug|feature) \d|^pr #\d|"
    r"^suggested:|^accepted:|^canceled:|^declined:|recent messages|^attachment:|ticket check|"
    r"meeting assets for|_transcripts$"
)
SECTION = re.compile(r"(?im)^#+\s*(executive summary|for you|decisions?|action items|next steps|risks[^\n]*)\s*$")


# ----------------------------------------------------------------------------- local only

def is_loopback(url: str) -> bool:
    try:
        host = (urlsplit(url if "://" in url else f"http://{url}").hostname or "").lower()
    except Exception:
        return False
    return host in LOOPBACK


def assert_local_only(**urls: str) -> None:
    bad = [f"{k}={v}" for k, v in urls.items() if not is_loopback(v or "")]
    if bad:
        raise RuntimeError("refusing a non-local endpoint: " + ", ".join(bad))


# ----------------------------------------------------------------------------- config

def load_focus(path: str | Path | None) -> dict:
    cfg = dict(DEFAULT_FOCUS)
    try:
        got = json.loads(Path(path).read_text()) if path else {}
    except Exception:
        got = {}
    if isinstance(got, dict):
        cfg.update({k: v for k, v in got.items() if not str(k).startswith("_")})
    for k in ("focus", "deprioritize"):
        cfg[k] = [a for a in (cfg.get(k) or []) if isinstance(a, dict) and a.get("name")]
    return cfg


def _terms(area: dict) -> list[str]:
    return [str(t).lower() for t in (area.get("match") or []) if str(t).strip()]


def _epics(tags: str) -> str:
    return " ".join(m.group(1) for m in re.finditer(r"(?i)\bepic:\s*([^;]+)", tags or ""))


def area_of(item: dict, areas: list[dict], title_only: bool = False) -> str | None:
    """Whole-word match on title, parent title and epic tag (free-form tags are too noisy)."""
    parts = [item.get("title")] if title_only else [item.get("title"), item.get("parent_title"), _epics(item.get("tags") or "")]
    hay = " ".join(str(x or "") for x in parts).lower()
    for a in areas:
        for t in _terms(a):
            if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", hay):
                return a["name"]
    return None


def workdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def owner_cap(cfg: dict, sprint_start: str, sprint_finish: str) -> tuple[float, list[dict]]:
    cap = float(cfg.get("cap") or 8)
    days = workdays(date.fromisoformat(sprint_start), date.fromisoformat(sprint_finish))
    if not days:
        return cap, []
    hits, off = [], set()
    for w in cfg.get("out_of_office") or []:
        try:
            a, b = date.fromisoformat(w["start"]), date.fromisoformat(w["end"])
        except Exception:
            continue
        o = {d for d in days if a <= d <= b}
        if o:
            off |= o
            hits.append({"start": w["start"], "end": w["end"], "days": len(o)})
    return float(int(cap * (len(days) - len(off)) / len(days))), hits


# ----------------------------------------------------------------------------- notes

def doc_date(title: str, body: str, ts) -> str:
    """When it happened (a date in the title or a Date: line), not when it was indexed."""
    m = re.search(r"(20\d\d-\d\d-\d\d)", title or "") or re.search(r"(?im)^\s*date:\s*(20\d\d-\d\d-\d\d)", (body or "")[:600])
    if m:
        return m.group(1)
    try:
        return datetime.fromtimestamp(float(ts)).date().isoformat()
    except Exception:
        return ""


def key_sections(body: str, limit: int = 1400) -> str:
    body = body or ""
    picks = []
    for m in SECTION.finditer(body):
        nxt = SECTION.search(body, m.end())
        chunk = body[m.start():nxt.start() if nxt else m.end() + 900]
        picks.append(re.sub(r"\n{2,}", "\n", chunk.strip())[:600])
    if not picks:
        i = body.lower().find("next steps")
        picks.append(body[i:i + 900] if i >= 0 else body[:700])
    return "\n".join(picks)[:limit]


def chunk_docs(docs: list[dict], max_chars: int) -> list[str]:
    """Pack dated notes into prompts that fit a small local context window."""
    chunks, cur = [], ""
    for d in docs:
        block = f"[{d.get('date', '')} {d.get('source', '')}] {d.get('title', '')}\n{d.get('text', '')}\n\n"
        if cur and len(cur) + len(block) > max_chars:
            chunks.append(cur)
            cur = ""
        cur += block[:max_chars]
    if cur:
        chunks.append(cur)
    return chunks


def grounded_signals(raw: list[dict], source_text: str, limit: int = 14) -> list[dict]:
    """Keep model-extracted facts only when every id they cite appears in the source notes."""
    out = []
    for g in raw or []:
        fact = re.sub(r"\s+", " ", str((g or {}).get("fact") or "")).strip()
        ids = {int(n) for n in re.findall(r"(?<!\d)\d{6}(?!\d)", fact + " " + " ".join(map(str, (g or {}).get("ids") or [])))}
        if not fact or len(fact) > 260 or any(str(n) not in source_text for n in ids):
            continue
        out.append({"date": str((g or {}).get("date") or "")[:10], "fact": fact, "ids": sorted(ids)})
    out.sort(key=lambda g: g["date"], reverse=True)
    return out[:limit]


# ----------------------------------------------------------------------------- proposals (never assigned)

def propose(items: list[dict], cfg: dict, load: dict, sprint_paths: set[str], cap_left: float,
            signals: list[dict] | None = None) -> dict:
    owner = cfg.get("owner") or "me"
    cap = float(cfg.get("cap") or 8)
    pull_from = set(cfg.get("pull_from") or [])
    test_re = re.compile(cfg.get("test_ticket_pattern") or r"$^", re.I)
    ktlo = cfg.get("ktlo") or {}
    k_types = {t.lower() for t in ktlo.get("types") or ["bug"]}
    k_prio = int(ktlo.get("max_priority") or 2)
    sig_ids = {}
    for g in signals or []:
        for n in g.get("ids") or []:
            if re.search(r"(?i)\b(you|assign|own|pick up)\b", g.get("fact") or ""):
                sig_ids.setdefault(n, g)
    cands, tests = [], []
    for x in items:
        if x["who"] == owner or x["type"].lower() not in PLAN_TYPES or x["state"] in DONE:
            continue
        sprinted = any(x["iter"].startswith(p) for p in sprint_paths)
        if test_re.search(x.get("title") or ""):
            if x["who"] == "unassigned" and sprinted:
                tests.append(x["id"])
            continue
        area = area_of(x, cfg.get("focus") or [])
        if area_of(x, cfg.get("deprioritize") or []) and not (area and area_of(x, cfg.get("focus") or [], title_only=True)):
            continue
        over = x["who"] not in ("unassigned", owner) and load.get(x["who"], 0) > cap
        if not (x["who"] == "unassigned" or over or x["who"] in pull_from or x["id"] in sig_ids):
            continue
        score, why = 0, []
        if area:
            score += 3
            why.append(area)
        if x["id"] in sig_ids:
            score += 4
            why.append("asked")
        if over and area:
            score += 2
            why.append("over cap")
        is_ktlo = (x["who"] == "unassigned" and sprinted and x["type"].lower() in k_types
                   and int(x.get("prio") or 9) <= k_prio and x["state"] in ("New", "Active", "Committed"))
        if is_ktlo:
            score += 2
            why.append("KTLO")
        if not area and x["id"] not in sig_ids and not is_ktlo:
            continue
        if x["state"] == "Testing" and x["who"] != "unassigned":
            continue
        score += 1 if sprinted else 0
        cands.append({**x, "score": score, "why": why, "area": area or ("KTLO" if is_ktlo else "")})
    cands.sort(key=lambda c: (-c["score"], int(c.get("prio") or 9), c["pts"] is None, c["pts"] or 0, c["id"]))
    commit, stretch, used, unp, unp_k = [], [], 0.0, 0, 0
    for c in cands:
        if c["pts"] is not None:
            if used + c["pts"] <= cap_left:
                used += c["pts"]
                commit.append(c)
            elif len(stretch) < int(cfg.get("max_stretch") or 3):
                stretch.append(c)
        else:
            is_k = "KTLO" in c["why"] and c["id"] not in sig_ids
            if unp < int(cfg.get("max_unpointed_commit") or 3) and (not is_k or unp_k < int(ktlo.get("max_unpointed") or 2)):
                unp += 1
                unp_k += 1 if is_k else 0
                commit.append(c)
    return {"commit": commit, "stretch": stretch, "used": used, "test_tickets": sorted(tests)}


def proposal_lane(c: dict, cfg: dict, stretch: bool = False) -> str:
    area = c.get("area") or ""
    mgr = cfg.get("manager") or ""
    src = ("no owner" if c["who"] == "unassigned" else ("from you" if mgr and c["who"] == mgr else f"from {c['who']}"))
    lane = f"{area}, {src}" if area else src
    return ("stretch, " + lane) if stretch else lane


def eased_load(load: dict, picked: list[dict]) -> dict:
    out = defaultdict(float, load)
    for r in picked:
        if r["who"] in out:
            out[r["who"]] -= r["pts"] or 0
    return dict(out)


# ----------------------------------------------------------------------------- diff + fail closed

def signature(rows: list[dict]) -> dict:
    return {str(r["id"]): {"who": r.get("from") or r.get("who"), "pts": r.get("pts"), "lane": r.get("lane")} for r in rows}


def diff_plans(prev_rows: list[dict] | None, cur_rows: list[dict], prev_props=None, cur_props=None) -> dict:
    if prev_rows is None:
        return {"first": True, "changed": True}
    a, b = signature(prev_rows), signature(cur_rows)
    d = {"first": False,
         "new": sorted(set(b) - set(a)), "gone": sorted(set(a) - set(b)),
         "moved": sorted(k for k in set(a) & set(b) if a[k]["who"] != b[k]["who"]),
         "repointed": sorted(k for k in set(a) & set(b) if a[k]["pts"] != b[k]["pts"]),
         "proposals_before": sorted(map(str, prev_props or [])), "proposals_now": sorted(map(str, cur_props or []))}
    d["changed"] = bool(d["new"] or d["gone"] or d["moved"] or d["repointed"]
                        or d["proposals_before"] != d["proposals_now"])
    return d


def _join(items) -> str:
    items = [str(x) for x in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " & " + items[-1]


def diff_sentence(d: dict, prev_label: str | None) -> str:
    if d.get("first") or not prev_label:
        return ""
    bits = []
    for key, word in (("new", "new on the list"), ("gone", "closed or moved off"),
                      ("moved", "changed owner"), ("repointed", "re-pointed")):
        if d.get(key):
            bits.append(f"{len(d[key])} {word}")
    if d.get("proposals_now") and d.get("proposals_now") != d.get("proposals_before"):
        bits.append("my pickups shifted with the new priorities")
    return (f"since the {prev_label} plan: " + _join(bits) + ".") if bits else ""


# ----------------------------------------------------------------------------- grounded wording

def numbers_in(*texts) -> set[str]:
    out = set()
    for t in texts:
        out |= set(re.findall(r"(?<![A-Za-z])\d+(?![A-Za-z])", str(t)))
    return out


def grounded_text(text: str, *sources: str, max_len: int = 320) -> bool:
    """Fresh model wording is used only when every number in it already appears in the sources."""
    t = re.sub(r"\s+", " ", text or "").strip()
    return bool(t) and 20 <= len(t) <= max_len and " - " not in t and numbers_in(t) <= numbers_in(*sources) \
        and not re.search(r"(?<!\d)\d{6}(?!\d)", t)
