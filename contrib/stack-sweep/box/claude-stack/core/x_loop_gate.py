#!/usr/bin/env python3
"""x_loop_gate.py — dedup + cap + window + kill-switch gate for the X loop.

Deterministic, $0, no LLM. Fail-closed: check() blocks on ANY reason; supply
and the loop tick both call it before anything is written or posted.

Dup store: sha256 of normalized text across
  * every x lane's ready/ and posted/ dirs under data/hustle/x_decks
  * the shared posted_ledger (core/posted_ledger.py)
  * /home/operator/socials/x_loop_posted.jsonl (this loop's own history)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys_path_added = False
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

X_DECKS = ROOT / "data" / "hustle" / "x_decks"
XLOOP = ROOT / "data" / "hustle" / "x_loop"
READY = XLOOP / "ready"
POSTED = XLOOP / "posted"
REJECTED = XLOOP / "rejected"
LOOP_POSTED = Path("/home/operator/socials/x_loop_posted.jsonl")
CONFIG_PATH = Path("/home/operator/socials/x_loop_config.json")

DEFAULT_CONFIG = {
    "jitter_min": 85,
    "window_start": "06:45",
    "window_end": "23:30",
    "tz": "America/Chicago",
    "daily_cap": 8,
    "kill_switch": "/home/operator/socials/X_LOOP_OFF",
    "lanes": ["x_hailports", "x_ima"],
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except Exception:  # noqa: BLE001
        pass
    try:  # 2026-10-29 operator: central lane switch; disabled lanes drop out of every lanes list (re-enable = edit the json)
        from core.lanes import disabled_lanes
        _d = disabled_lanes()
        if _d:
            cfg["lanes"] = [l for l in cfg.get("lanes") or [] if l not in _d]
            cfg["disabled_lanes"] = sorted(_d)
    except Exception:  # noqa: BLE001
        pass
    return cfg


def _norm(text: str) -> str:
    try:
        from core.persona_series import body_for_dup
        text = body_for_dup(text or "")
    except Exception:
        text = text or ""
    text = text.lower()
    text = "".join(c if (c.isalnum() or c.isspace()) else " " for c in text)
    return " ".join(text.split())


def text_sha(text: str) -> str:
    return hashlib.sha256(_norm(text).encode()).hexdigest()


def style_example_shas() -> set[str]:
    """Exact Ines Fiends style references are duplicates, not drafts."""
    try:
        from core.content_generator import IMA_STYLE_EXAMPLES
    except Exception:  # noqa: BLE001
        return set()
    return {text_sha(x) for x in IMA_STYLE_EXAMPLES}


def kill_switch_on(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    return Path(cfg["kill_switch"]).exists()


def in_window(cfg: dict | None = None, now: datetime | None = None) -> bool:
    cfg = cfg or load_config()
    tz = ZoneInfo(cfg["tz"])
    now = now or datetime.now(tz)
    start = datetime.strptime(cfg["window_start"], "%H:%M").time()
    end = datetime.strptime(cfg["window_end"], "%H:%M").time()
    return start <= now.time() <= end


def is_reply_row(row: dict) -> bool:
    """2026-11-04: thread parts (part>0) and manual reply rows don't use an originals slot.
    Before this, the 4 StackMini launch reply rows on Nov 3 ate the @hailports cap by 3:24 PM CT."""
    try:
        if int(str(row.get("part") or 0)) > 0:
            return True
    except ValueError:
        pass
    ref = str(row.get("ref") or "")
    return bool(re.search(r"(^manual_reply_|_reply(_|$)|_r\d+$|_donate_reply)", ref))


def posts_today(cfg: dict | None = None, now: datetime | None = None,
                lane: str | None = None) -> int:
    """Posts counted today; with `lane`, per-lane so daily_cap applies PER LANE
    (2026-10-16: lanes went to 2, a global cap would halve each lane's share)."""
    cfg = cfg or load_config()
    tz = ZoneInfo(cfg["tz"])
    now = now or datetime.now(tz)
    today = now.date()
    n = 0
    if not LOOP_POSTED.is_file():
        return 0
    for line in LOOP_POSTED.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if lane is not None and row.get("lane") != lane:
            continue
        if is_reply_row(row):  # 2026-11-04 overnight-gap fix: replies/thread parts are not originals
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("at") or row.get("ts") or "").replace("Z", "+00:00"))
            _rf = Path("/home/operator/socials/counter_reset_epoch.json")
            _rts = float(json.loads(_rf.read_text()).get("reset_ts", 0)) if _rf.is_file() else 0.0
            if ts.timestamp() >= _rts and ts.astimezone(tz).date() == today:
                n += 1
        except Exception:  # noqa: BLE001 — undated row counts as today (fail-closed)
            n += 1
    return n


def known_shas(exclude=None) -> set[str]:
    """Every text sha the loop must treat as already used."""
    shas: set[str] = set()
    for sub in ("ready", "posted"):
        d = X_DECKS / sub
        if d.is_dir():
            for f in d.glob("*.txt"):
                if exclude is not None and Path(f).resolve() == Path(exclude).resolve():
                    continue  # the candidate itself is not a duplicate of itself
                try:
                    shas.add(text_sha(f.read_text()))
                except Exception:  # noqa: BLE001
                    pass
    for d in (READY, POSTED, REJECTED):
        if d.is_dir():
            for f in d.glob("*.txt"):
                if exclude is not None and Path(f).resolve() == Path(exclude).resolve():
                    continue  # the candidate itself is not a duplicate of itself
                try:
                    shas.add(text_sha(f.read_text()))
                except Exception:  # noqa: BLE001
                    pass
    if LOOP_POSTED.is_file():
        for line in LOOP_POSTED.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if row.get("sha"):
                shas.add(row["sha"])
            for key in ("text", "post"):
                if row.get(key):
                    shas.add(text_sha(str(row[key])))
    try:
        from core import posted_ledger
        for t in posted_ledger.recent_texts(days=3650):
            shas.add(text_sha(t))
    except Exception:  # noqa: BLE001 — ledger unavailable is a block, not a pass
        shas.add("ledger-unreadable-block-all")
    return shas


def check(text: str, *, cfg: dict | None = None, now: datetime | None = None, content_only: bool = False, exclude=None) -> tuple[bool, list[str]]:
    """One decision for the whole loop. FAIL-CLOSED."""
    cfg = cfg or load_config()
    reasons: list[str] = []
    try:
        if not content_only:  # supply only needs the dup/empty checks
            if kill_switch_on(cfg):
                reasons.append("kill_switch")
            if not in_window(cfg, now):
                reasons.append("outside_window")
            # 2026-11-02 operator [t269u]: this was a GLOBAL count against the PER-LANE daily_cap (7), so both live lanes
            # shared 7 originals/day (~3.5 each). Per-lane caps are enforced in bin/socials-x-loop.py (_lane_cap);
            # here only an all-lanes ceiling = sum of the active lanes' caps (override: global_daily_cap).
            _lanes = [l for l in (cfg.get("lanes") or []) if l]
            _gcap = int(cfg.get("global_daily_cap") or sum(int((cfg.get("lane_daily_cap") or {}).get(l, cfg["daily_cap"])) for l in _lanes) or cfg["daily_cap"])
            if posts_today(cfg, now) >= _gcap:
                reasons.append("daily_cap")
        if not _norm(text):
            reasons.append("empty_text")
        from core.identity_gate import check as _idc  # HARD identity split, all lanes
        reasons.extend(_idc(text))
        if text_sha(text) in known_shas(exclude) or text_sha(text) in style_example_shas():
            reasons.append("duplicate_text")
        # 2026-10-26 X Algorithm Update: block recycled wording / copypasta spam (Jaccard >= 0.48 against last 100 posts)
        try:
            from core.content_quality import _jaccard
            _clean_cand = re.sub(r"https?://\S+", "", text or "").strip()
            if LOOP_POSTED.is_file() and len(_clean_cand) >= 35:
                for _ln in LOOP_POSTED.read_text().splitlines()[-120:]:
                    _r = json.loads(_ln)
                    _prev = re.sub(r"https?://\S+", "", str(_r.get("text") or "")).strip()
                    if _prev and _prev != _clean_cand and _jaccard(_clean_cand, _prev) >= 0.48:
                        reasons.append("copypasta_similarity")
                        break
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"gate_error:{type(exc).__name__}:{exc}"[:200])
    return (not reasons), reasons


def record(text: str, lane: str, *, ref: str = "") -> dict:
    """Append one LOOP POST to the loop history (after a verified post only)."""
    row = {
        "at": datetime.now().astimezone().isoformat(),
        "lane": lane,
        "sha": text_sha(text),
        "text": (text or "").strip()[:800],  # raw (punctuation kept); sha stays normalized for dedup
        "ref": ref,
    }
    LOOP_POSTED.parent.mkdir(parents=True, exist_ok=True)
    with LOOP_POSTED.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def _cli() -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="X loop gate check")
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--file", default="")
    args = ap.parse_args()
    text = Path(args.file).read_text() if args.file else args.text
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read()
    ok, reasons = check(text)
    print(json.dumps({"ok": ok, "reasons": reasons, "sha": text_sha(text),
                      "posts_today": posts_today(), "in_window": in_window(),
                      "kill_switch": kill_switch_on()}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli())


# 2026-11-02 operator [pt275u]: @hailports daily article + daily finding. Both post through this loop's ledger
# (LOOP_POSTED), so they count toward the per-account cap (7; no jitter for hailports since 2026-11-02), the 22-min cross-lane gap and the
# hailports peak hold exactly like any other original. Article spacing itself: core.rhythm.ARTICLE_DAILY
# (one per CT day in 7-10 / 11-13 CT, >= 18h apart). Kill switch for the daily pieces:
HP_DAILY_OFF = Path("/home/operator/socials/HP_DAILY_OFF")


def hp_daily_off() -> bool:
    return HP_DAILY_OFF.exists()
