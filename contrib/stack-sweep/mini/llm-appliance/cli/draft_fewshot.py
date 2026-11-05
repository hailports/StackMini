"""Retrieved few-shot (2026-10-10): 2 examples from operator's OWN sent/edited replies, matched to the new inbound.

Replaces (not adds to) the static 5-item BEFORE->AFTER list inside the continuous-learning block.
Pure python BM25 over ~200 pairs, no embedding model, no model call, no network.
Flag/arm: draft_v2.feature_arm("fewshot", queue_key) via runtime/local-cycle-browser/draft_features.json.
Fails open: any problem returns "" and the caller keeps the old static block.
No example text is ever logged.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
EVENTS = Path(os.environ.get("DRAFT_FEWSHOT_EVENTS", str(HOME / "claude-stack/data/learning/draft_feedback_events.jsonl")))
CONTEXT = Path(os.environ.get("DRAFT_FEWSHOT_CONTEXT", str(HOME / "claude-stack/data/learning/continuous_learning_context.json")))
MAX_CHARS = 5200
_STOP = set("the a an and or to for of on in at it this that is are was were be been you your our we i me my can could will would "
            "please thanks thank hi hey hello let know have has had not no yes with from by as if so do does did just also any all "
            "there here what when who how why about into out up".split())
_cache = {"mt": 0.0, "pairs": [], "df": {}, "n": 0}


def _toks(t: str) -> list:
    return [w for w in re.findall(r"[a-z][a-z0-9]{2,}", (t or "").lower()) if w not in _STOP]


def _clip_sentence(t: str, n: int, tail: bool = False) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    if len(t) <= n:
        return t
    if tail:  # keep the END (latest ask); start at a sentence boundary
        s = t[-n:]
        m = re.search(r"[.!?]\s+", s)
        return (s[m.end():] if m and m.end() < len(s) - 40 else s).strip()
    s = t[:n]
    m = max(s.rfind(". "), s.rfind("? "), s.rfind("! "))
    return (s[: m + 1] if m > n * 0.5 else s).strip()


def scrub(t: str, extra_names: list | None = None) -> str:
    t = re.sub(r"https?://\S+", "<link>", t or "")
    t = re.sub(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "<email>", t)
    t = re.sub(r"\b(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{15,18}\b", "<id>", t)
    t = re.sub(r"\b\d{3}[-. )]*\d{3}[-. ]*\d{4}\b", "<phone>", t)
    t = re.sub(r"\b[A-Z0-9]{2,6}-?\d{3,}\b", "<ref>", t)
    for nm in extra_names or []:
        if len(nm) >= 3:
            t = re.sub(r"\b" + re.escape(nm) + r"\b", "<name>", t, flags=re.I)
    return t


def scrub_style(t: str, extra_names: list | None = None) -> str:
    """Style and structure only. Names and numbers become placeholders."""
    t = scrub(t, extra_names)
    t = re.sub(r"\d+", "<n>", t)
    t = re.sub(
        r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:\s+(?:[A-Z][a-z]+|[A-Z]{2,}))+\b",
        "<name>",
        t,
    )
    t = re.sub(r"(?:<name>\s*){2,}", "<name> ", t)
    t = re.sub(r"(?:<n>\s*){2,}", "<n> ", t)
    return re.sub(r"\s+", " ", t).strip()


def _sha10(raw: str) -> str:
    return hashlib.sha1(str(raw).encode("utf-8", "replace")).hexdigest()[:10]


def _home() -> Path:
    return Path.home()


def heldout_ids(home: Path | None = None) -> set:
    """Raw frozen ids and their ledger hashes. Fail-open to empty."""
    home = home or _home()
    banned = set()
    try:
        doc = json.loads((home / "claude-stack/data/learning/eval_frozen/exclude.json").read_text())
    except Exception:
        return banned
    for raw in list(doc.get("event_ids") or []) + list(doc.get("source_keys") or []):
        banned.add(str(raw))
        banned.add(_sha10(raw))
    for raw in doc.get("tickets") or []:
        banned.add(str(raw))
    return banned


def sensitive_hashes(home: Path | None = None) -> set:
    home = home or _home()
    out = set()
    path = home / "claude-stack/data/learning/draft_outcomes.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("class") == "skipped_sensitive" and row.get("key_hash"):
            out.add(str(row["key_hash"]))
    return out


def _names(e: dict) -> list:
    out = []
    for fld in ("thread_label", "actor"):
        out += [w for w in re.findall(r"[A-Za-z]{3,}", str(e.get(fld) or "")) if w.lower() not in _STOP]
    return out[:8]


def _eval_excluded():
    """Frozen held-out eval items (qwen_alone_eval): never retrieve / few-shot / train on them. Fail-open."""
    try:
        d = json.loads((Path.home() / "claude-stack/data/learning/eval_frozen/exclude.json").read_text())
        return set(d.get("event_ids") or []), set(d.get("source_keys") or []), set(str(x) for x in d.get("tickets") or [])
    except Exception:
        return set(), set(), set()


def _events_path() -> Path:
    return Path(os.environ.get("DRAFT_FEWSHOT_EVENTS") or (Path.home() / "claude-stack/data/learning/draft_feedback_events.jsonl"))


def _pinned_path() -> Path:
    return Path(os.environ.get("DRAFT_FEWSHOT_PINNED") or (Path.home() / "claude-stack/data/learning/pinned_fewshots.json"))


def _context_path() -> Path:
    return Path(os.environ.get("DRAFT_FEWSHOT_CONTEXT") or (Path.home() / "claude-stack/data/learning/continuous_learning_context.json"))


def _value_gold_path() -> Path:
    return Path(os.environ.get("DRAFT_VALUE_GOLD") or (Path.home() / "claude-stack/data/learning/value_gold_examples.jsonl"))


def _idle_use_path() -> Path:
    return Path(os.environ.get("DRAFT_IDLE_USE") or (Path.home() / "claude-stack/data/learning/idle_artifact_use.jsonl"))


_CL = None


def _continuous_learning():
    """Lazy import of core.continuous_learning. None if it cannot be loaded."""
    global _CL
    if _CL is not None:
        return _CL
    try:
        import sys
        root = str(Path.home() / "claude-stack")
        if root not in sys.path:
            sys.path.insert(0, root)
        import core.continuous_learning as cl
        _CL = cl
        return cl
    except Exception:
        return None


def _note_artifact_use(kind: str, ref: str) -> None:
    """Append-only consumption log. Never raises and never stores example text."""
    try:
        path = _idle_use_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": str(kind or "")[:40],
            "ref": str(ref or "")[:80],
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        return


def _load_value_gold(pairs: list, df: dict) -> None:
    """Low-priority voice pairs. retrieve() keeps at most 2."""
    path = _value_gold_path()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    rows = []
    for line in lines[-50:]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("class") not in ("used_as_is", "light_edit"):
            continue
        src = str(obj.get("draft_text") or "")
        fin = str(obj.get("sent_text") or "")
        if len(src.strip()) < 20 or len(fin.strip()) < 25:
            continue
        rows.append(obj)
    for obj in rows:
        src = str(obj.get("draft_text") or "")
        fin = str(obj.get("sent_text") or "")
        label = str(obj.get("thread_label") or "")
        tk = _toks((label + "\n" + src)[-1500:])
        _add_pair(
            pairs,
            df,
            {
                "surface": str(obj.get("surface") or ""),
                "outcome": "sent_edited",
                "src": src,
                "fin": fin,
                "ask": label,
                "tk": tk,
                "key": str(obj.get("key_hash") or ""),
                "names": _names({"thread_label": label}),
                "at": str(obj.get("ts") or ""),
                "pinned": False,
                "match": "",
                "superseded_by": "",
                "value_gold": True,
            },
        )


def _add_pair(pairs: list, df: dict, row: dict) -> None:
    tk = row.get("tk") or []
    if not tk:
        return
    for w in set(tk):
        df[w] = df.get(w, 0) + 1
    pairs.append(row)


def load_pairs(force: bool = False) -> list:
    ev, pin, gold = _events_path(), _pinned_path(), _value_gold_path()
    mt = 0.0
    for p in (ev, pin, gold):
        try:
            mt += p.stat().st_mtime
        except Exception:
            pass
    try:
        _exf = Path.home() / "claude-stack/data/learning/eval_frozen/exclude.json"
        mt += _exf.stat().st_mtime if _exf.exists() else 0
    except Exception:
        pass
    sig = (str(ev), str(pin), str(gold), mt)
    if not force and _cache.get("sig") == sig:
        return _cache["pairs"]
    pairs, df = [], {}
    _XE, _XK, _ = _eval_excluded()
    _sens = sensitive_hashes()
    try:
        for line in ev.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("outcome") not in ("sent_unchanged", "sent_edited"):
                continue
            if e.get("event_id") in _XE or e.get("source_key") in _XK:  # frozen held-out eval item
                continue
            if _sha10(e.get("source_key") or "") in _sens or _sha10(e.get("event_id") or "") in _sens:
                continue
            if str(e.get("class") or "") == "skipped_sensitive":
                continue
            src, fin = str(e.get("source_text") or ""), str(e.get("final_text") or "")
            if len(src.strip()) < 40 or len(fin.strip()) < 25:
                continue
            tk = _toks(src[-1500:])
            _add_pair(pairs, df, {"surface": str(e.get("surface")), "outcome": e["outcome"], "src": src, "fin": fin,
                                  "tk": tk, "key": str(e.get("source_key") or ""), "names": _names(e), "at": str(e.get("observed_at") or ""),
                                  "pinned": False, "match": "", "superseded_by": ""})
    except Exception:
        pass
    try:
        rows = json.loads(pin.read_text(encoding="utf-8"))
    except Exception:
        rows = []
    _banned = heldout_ids()
    for e in rows if isinstance(rows, list) else []:
        if e.get("superseded_by"):
            continue
        if str(e.get("source") or "") in _banned or str(e.get("event_id") or "") in _banned:
            continue
        fin = str(e.get("after") or "")
        src = "\n".join(x for x in (str(e.get("ask") or ""), str(e.get("pattern") or e.get("rule") or ""), str(e.get("before") or "")) if x)
        if len(src.strip()) < 20 or len(fin.strip()) < 25:
            continue
        tk = _toks(src[-1500:])
        _add_pair(pairs, df, {"surface": str(e.get("surface") or "zoom"), "outcome": "sent_unchanged", "src": src, "fin": fin,
                              "tk": tk, "key": str(e.get("source") or ""), "names": [], "at": "",
                              "pinned": True, "match": str(e.get("match") or ""), "superseded_by": "",
                              "style_only": bool(e.get("style_only")),
                              "ask": str(e.get("ask") or "")})
    try:
        _load_value_gold(pairs, df)
    except Exception:
        pass
    _cache.update(sig=sig, mt=mt, pairs=pairs, df=df, n=len(pairs))
    return pairs


def retrieve(ask: str, surface: str, k: int = 2, exclude_key: str = "", exclude_names: list | None = None) -> list:
    pairs = load_pairs()
    # Style pins are the stable prefix. At most the two shots build_block asks for.
    style = [p for p in pairs if p.get("style_only") and p.get("pinned") and not p.get("superseded_by")]
    if style:
        ordered = [p for p in style if p.get("surface") == surface] + [p for p in style if p.get("surface") != surface]
        # File order within a surface keeps the prefix stable for that surface.
        seen = set()
        out = []
        for p in ordered:
            ident = id(p)
            if ident in seen:
                continue
            seen.add(ident)
            out.append({**p, "score": 1.0})
            if len(out) >= k:
                break
        return out
    q = _toks((ask or "")[-1200:])
    if not pairs or not q:
        return []
    df, n = _cache["df"], max(1, _cache["n"])
    qs = set(q)
    avg = sum(len(p["tk"]) for p in pairs) / len(pairs)
    scored = []
    for p in pairs:
        if exclude_key and p["key"] == exclude_key:
            continue
        if p.get("superseded_by"):
            continue
        matched = False
        if p.get("match"):
            try:
                matched = re.search(str(p["match"]), ask or "") is not None
            except re.error:
                matched = False
            if not matched:
                continue
        # 2026-10-12: never a same-thread prior answer (names overlap with current thread) - style only
        if exclude_names and any(str(x).lower() in {str(n).lower() for n in p["names"]} or
                                 str(x).lower() in (p["src"][:400].lower()) for x in exclude_names):
            continue
        tf = {}
        for w in p["tk"]:
            if w in qs:
                tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w, f in tf.items():
            idf = math.log(1 + (n - df.get(w, 0) + 0.5) / (df.get(w, 0) + 0.5))
            s += idf * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * len(p["tk"]) / avg))
        if p["surface"] == surface:
            s *= 1.35
        if p["outcome"] == "sent_unchanged":
            s *= 1.2
        if matched:
            s += 50.0
        L = len(p["fin"])
        if L > 900 or L < 40:
            s *= 0.6
        if p.get("value_gold"):
            s *= 0.15  # voice example, below operator's own sent/edited pairs
        scored.append((s, p))
    scored.sort(key=lambda x: -x[0])
    out, seen_threads = [], set()
    gold_used = 0
    for s, p in scored:
        if s <= 0:
            break
        if p.get("value_gold") and gold_used >= 2:
            continue
        th = tuple(sorted(p["names"]))[:3]
        if th and th in seen_threads:
            continue
        seen_threads.add(th)
        out.append({**p, "score": s})
        if p.get("value_gold"):
            gold_used += 1
        if len(out) >= k:
            break
    return out


def rank_lessons_generic(query: str, items: list, k: int = 4, cap: int = 1800, each: int = 600) -> list:
    """Top-k items by content-word overlap with query (ties keep stored order = newest first), within a char cap."""
    q = set(_toks(query or ""))
    sc = []
    for i, x in enumerate(items):
        x = str(x or "")
        if not x or x.startswith("patterns:") and len(x) < 40:
            continue
        t = set(_toks(x)) | set(w for w in re.findall(r"[a-z_]{4,}", x.lower()) for w in w.split("_"))
        sc.append((len(q & t), -i, x))
    sc.sort(reverse=True)
    out, used = [], 0
    for s, _, x in sc:
        if s <= 0 or len(out) >= k:
            break
        x = _clip_sentence(x, each)
        if used + len(x) > cap:
            continue
        out.append(x); used += len(x)
    return out


def _rank_lessons(ask: str, qa: list) -> list:
    return rank_lessons_generic(ask, qa)


def build_block(surface: str, ask: str, exclude_key: str = "", max_chars: int = MAX_CHARS) -> str:
    """Same shape as continuous_learning.prompt_block, but static BEFORE->AFTER replaced by 2 retrieved pairs.
    Returns "" (=> caller keeps old block) when nothing usable is retrieved."""
    ex = retrieve(ask, surface, 2, exclude_key)
    if len(ex) < 1:
        return ""
    payload = json.loads(_context_path().read_text(encoding="utf-8"))
    clip = lambda s, n: _clip_sentence(str(s or ""), n)
    lines = [
        "=== CONTINUOUS LEARNING (advisory; NOT current evidence; rewrite-only) ===",
        "surface=" + str(surface),
        "CURRENT THREAD, ATTACHMENTS, QUERY FACTS, operator AND WORK-BRAIN EVIDENCE OVERRIDE ALL EXAMPLES.",
    ]
    # 2026-10-20: QA + idle lessons ranked by overlap with this ask, ~1800-char budget, before fixed rules
    lines += ["QA/RULE FEEDBACK (most relevant to this ask):"]
    _cl = _continuous_learning()
    _picked = None
    if _cl is not None:
        try:
            _picked = _cl.select_lessons(ask, payload.get("qa_rules") or [], _cl.idle_lessons())
        except Exception:
            _picked = None
    if _picked is None:
        lines += ["- " + x for x in _rank_lessons(ask, payload.get("qa_rules") or [])]
    else:
        lines += ["- " + text for text, _meta in _picked]
    lines += ["RULES:"] + ["- " + clip(x, 500) for x in (payload.get("rules") or [])[:18]]
    exl = ["operator'S OWN REPLIES TO SIMILAR ASKS (voice and answer-first shape only; facts here are NOT facts of this thread):"]
    for i, p in enumerate(ex, 1):
        nm = p["names"]
        shown = (p.get("ask") or p["src"]) if p.get("value_gold") else p["src"]
        a = scrub_style(_clip_sentence(shown, 320, tail=True), nm)
        r = scrub_style(_clip_sentence(p["fin"], 420), nm)
        exl += [f"[{i}] ASK: {a}", f"[{i}] operator: {r}"]
    ex_part = "\n".join(exl)
    foot = "\n=== END CONTINUOUS LEARNING ===\n"
    head = "\n".join(lines)
    room = max_chars - len(ex_part) - len(foot) - 2
    if len(head) > room:  # examples are never cut; the rule/QA tail gives way (same total size as the old block)
        head = head[:room].rsplit("\n", 1)[0]
    block = head + "\n" + ex_part + foot
    if _picked and _cl is not None:
        try:
            used = [meta for text, meta in _picked if meta and text and text in block]
            if used:
                _cl.note_lesson_use(used, surface=surface)
        except Exception:
            pass
    for p in ex:
        if p.get("value_gold"):
            _note_artifact_use("value_gold", str(p.get("key") or ""))
    return block


_NAME_SKIP = _STOP | {
    "outlook", "zoom", "email", "mail", "reply", "team", "update", "sent", "draft",
    "subject", "thread", "message", "thanks", "thank", "please", "operator",
}


def _label_names(label: str) -> list:
    out = []
    for word in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", label or ""):
        if word.lower() in _NAME_SKIP:
            continue
        out.append(word)
    return out[:12]


def _ticket_hit(text: str, banned: set) -> bool:
    for num in re.findall(r"\b\d{4,}\b", text or ""):
        if num in banned:
            return True
    return False


def rebuild_style_pins(home: Path | None = None, write: bool = True) -> dict:
    """Two style shots from sent edits. Sensitive and frozen held-out ids stay out.

    Names and numbers are placeholders. The Oct 21 pin file is left for the caller
    to back up before write. Returns counts only.
    """
    home = home or _home()
    learn = home / "claude-stack/data/learning"
    outcomes = learn / "draft_outcomes.jsonl"
    banned = heldout_ids(home)
    sensitive = sensitive_hashes(home)
    eligible = []
    skipped = {"sensitive": 0, "heldout": 0, "empty": 0, "class": 0}
    try:
        lines = outcomes.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        cls = str(row.get("class") or "")
        if cls == "skipped_sensitive":
            skipped["sensitive"] += 1
            continue
        if cls not in ("used_as_is", "rewritten"):
            skipped["class"] += 1
            continue
        key = str(row.get("key_hash") or "")
        if key in banned or key in sensitive:
            skipped["heldout" if key in banned else "sensitive"] += 1
            continue
        draft = str(row.get("draft_text") or "")
        sent = str(row.get("sent_text") or "")
        label = str(row.get("thread_label") or "")
        if _ticket_hit(label, banned) or _ticket_hit(draft, banned) or _ticket_hit(sent, banned):
            skipped["heldout"] += 1
            continue
        if len(draft.strip()) < 40 or len(sent.strip()) < 25:
            skipped["empty"] += 1
            continue
        names = _label_names(label)
        before = scrub_style(draft, names)[:700]
        after = scrub_style(sent, names)[:700]
        if len(after) < 25 or re.search(r"\d", before + after) or "@" in (before + after):
            skipped["empty"] += 1
            continue
        eligible.append({
            "surface": str(row.get("surface") or "zoom"),
            "class": cls,
            "score": float(row.get("score") or 0),
            "before": before,
            "after": after,
        })

    def rank(rows: list) -> list:
        return sorted(rows, key=lambda r: (0 if r["class"] == "used_as_is" else 1, -r["score"]))

    picked = []
    for surface in ("outlook", "zoom"):
        pool = [r for r in eligible if r["surface"] == surface]
        if pool:
            picked.append(rank(pool)[0])
    if len(picked) < 2:
        have = {(p["surface"], p["before"][:40]) for p in picked}
        for row in rank(eligible):
            ident = (row["surface"], row["before"][:40])
            if ident in have:
                continue
            picked.append(row)
            have.add(ident)
            if len(picked) >= 2:
                break
    picked = picked[:2]
    pins = []
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in picked:
        pins.append({
            "added_at": stamp,
            "surface": row["surface"],
            "source": "style-" + row["surface"],
            "style_only": True,
            "ask": "short reply on this surface. answer first. plain words. this example is shape only.",
            "before": row["before"],
            "after": row["after"],
            "pattern": "shape and wording only. placeholders are not facts of the current thread.",
            "rule": "do not copy a name, a number, or a fact from this example.",
            "match": "",
            "note": "tune style shot",
            "operator_redo": False,
        })
    result = {
        "pins": len(pins),
        "surfaces": [p["surface"] for p in pins],
        "eligible": len(eligible),
        "skipped_sensitive": skipped["sensitive"],
        "skipped_heldout": skipped["heldout"],
        "skipped_empty": skipped["empty"],
        "digits": sum(1 for p in pins if re.search(r"\d", p["before"] + p["after"])),
        "emails": sum(1 for p in pins if "@" in (p["before"] + p["after"])),
    }
    if write and pins:
        path = learn / "pinned_fewshots.json"
        path.write_text(json.dumps(pins, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _cache.clear()
    result["wrote"] = bool(write and pins)
    return result


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    home = None
    if "--home" in args:
        home = Path(args[args.index("--home") + 1])
    if args and args[0] == "rebuild-pins":
        print(json.dumps(rebuild_style_pins(home), sort_keys=True))
    else:
        raise SystemExit("usage: draft_fewshot.py rebuild-pins [--home DIR]")
