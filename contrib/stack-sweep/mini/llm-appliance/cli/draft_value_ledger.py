#!/usr/bin/env python3
"""Real value of local-model drafts, from what operator actually sent.

Replaces self-graded gold_value totals in the morning ledger. Offline by
default: local JSONL plus, only with --live, the read-only Outlook/Zoom
helpers. Never sends mail, never writes Outlook or Zoom, never changes
read/unread, never calls a cloud API, never touches llama-server :8080.

Window is the last N Mon-Fri dates in America/Chicago (default 5), including
today when today is a weekday. A reply counts only inside 48 business hours
of the staged draft. Business hours are elapsed Mon-Fri hours (weekends add
nothing; a Friday 16:00 plus 48h lands on Tuesday 16:00). Holidays are not
removed. Outcome observed_at is observation time, not the exact send time.
Verdict rows are labels and ratios only (key = sha1(raw source_key)[:10],
same as draft_outcome_observer._h). age_h, when present, places the reply.

Classes
  used_as_is   similarity >= 0.9
  light_edit   similarity >= 0.6 and < 0.9
  rewritten    similarity < 0.6
  not_used     no reply inside 48 business hours, or deleted / discarded_by_operator / gone_unproven
  superseded   a sent reply timestamped before the stage
  excluded     verdict superseded_by_newer_inbound, invalid_source_id, or expired_no_verdict
               (unmeasurable; left out of the percentages)
  pending      still inside 48 business hours and no outcome yet (left out of the percentages)
  skipped_sensitive  hard data line; stored with empty text and left out of the percentages

Percent denominator = staged - pending - excluded - skipped_sensitive.

Minutes (per measured draft; wrong_after_send replaces the class minutes).
Env overrides: DRAFT_VALUE_MIN_USED_AS_IS (3.0), DRAFT_VALUE_MIN_LIGHT_EDIT (1.5),
DRAFT_VALUE_MIN_REWRITTEN (0), DRAFT_VALUE_MIN_NOT_USED (0), DRAFT_VALUE_MIN_WRONG (-5).
superseded, pending, excluded, and skipped_sensitive are 0.
DRAFT_VALUE_NOW overrides the clock (ISO-8601).

Kill switch: claude-stack/data/flags/draft_value_off under --home. No writes.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
REPLY_BIZ_HOURS = 48.0
LABEL_MAX = 120
TEXT_MAX = 2000
CLASSES = (
    "used_as_is",
    "light_edit",
    "rewritten",
    "not_used",
    "superseded",
    "excluded",
    "pending",
    "skipped_sensitive",
)
PCT_CLASSES = ("used_as_is", "light_edit", "rewritten", "not_used", "superseded")
EXCLUDED_VERDICTS = {
    "superseded_by_newer_inbound",
    "invalid_source_id",
    "expired_no_verdict",
}
NOT_USED_VERDICTS = {"deleted", "gone_unproven", "discarded_by_operator"}
NOT_USED_OUTCOMES = {"deleted", "discarded_by_operator"}
LABEL_SCORE = {"sent_unchanged": 1.0, "sent_edited": 0.75, "sent_rewritten": 0.3}
SENT_TIME_KEYS = (
    "sent_at",
    "SentDateTime",
    "sentDateTime",
    "receivedDateTime",
    "ReceivedDateTime",
    "date",
    "ts",
    "time",
    "when",
)
WRONG_SEED = {
    "surface": "outlook",
    "source_key": "redacted-email",
    "event_id": "4958af449f0cf948689aee41",
    "note": "Michael 8:45 reply said profile lacked edit; real cause was products-locked Amount",
}
_LABEL_RE = re.compile(
    r"(?i)("
    r"\b(?:lawyer|attorneys?|counsel|accountants?|cpa)\b"
    r"|\blegal\b"
    r"|\btax(?:es|ation)?\b"
    r"|\bbank\b"
    r"|finance[\s_-]*folder"
    r"|(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)"
    r")"
)
_SECRET_RE = re.compile(
    r"(?i)("
    r"\bpasswords?\b"
    r"|seed\s+phrases?"
    r"|recovery\s+phrases?"
    r"|\bmnemonic\b"
    r"|\b2\s*-?\s*fa\b"
    r"|two[- ]factor"
    r"|verification\s+codes?"
    r"|one[- ]time\s+(?:codes?|passwords?|pins?)"
    r"|\botp\b"
    r")"
)
_GREET_RE = re.compile(
    r"(?i)^(hey|hi|hello|dear|good\s+(?:morning|afternoon|evening))\b"
)
_BOUND = (
    re.compile(r"(?im)^\s*best\s*,?\s*\n\s*operator(?:\s+de\s+operator)?\s*$"),
    re.compile(r"(?im)^\s*operator\s+de\s+operator\s*\|\s*Sr\.?\s+Technical\s+Lead"),
    re.compile(r"(?im)^\s*From:\s+"),
)
_OBS = {"mod": None, "tried": False}


def minutes_table() -> dict:
    def env_float(name: str, default: float) -> float:
        raw = os.environ.get(name, "")
        if str(raw).strip() == "":
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    return {
        "used_as_is": env_float("DRAFT_VALUE_MIN_USED_AS_IS", 3.0),
        "light_edit": env_float("DRAFT_VALUE_MIN_LIGHT_EDIT", 1.5),
        "rewritten": env_float("DRAFT_VALUE_MIN_REWRITTEN", 0.0),
        "not_used": env_float("DRAFT_VALUE_MIN_NOT_USED", 0.0),
        "superseded": 0.0,
        "pending": 0.0,
        "excluded": 0.0,
        "skipped_sensitive": 0.0,
        "wrong_after_send": env_float("DRAFT_VALUE_MIN_WRONG", -5.0),
    }


def parse_ts(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v = v / 1000.0
        try:
            return datetime.fromtimestamp(v, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", s):
            return parse_ts(float(s))
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # Bare dates: datetime.fromisoformat("YYYY-MM-DD") raises on Python 3.9.
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            try:
                dt = datetime.strptime(s, "%Y-%m-%d")
            except ValueError:
                return None
        else:
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", s)
                if not m:
                    return None
                try:
                    dt = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CT)
        return dt.astimezone(timezone.utc)
    return None


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def iso_ct(dt: datetime) -> str:
    return dt.astimezone(CT).isoformat(timespec="seconds")


def resolve_now(now=None) -> datetime:
    if now is not None:
        if isinstance(now, datetime):
            dt = now if now.tzinfo else now.replace(tzinfo=CT)
            return dt.astimezone(CT)
        parsed = parse_ts(now)
        if parsed is not None:
            return parsed.astimezone(CT)
    raw = os.environ.get("DRAFT_VALUE_NOW", "").strip()
    if raw:
        parsed = parse_ts(raw)
        if parsed is not None:
            return parsed.astimezone(CT)
    return datetime.now(CT)


def add_business_hours(start: datetime, hours: float) -> datetime:
    """Add Mon-Fri elapsed hours in America/Chicago. Weekends contribute 0."""
    ct = start.astimezone(CT)
    remaining = float(hours)
    guard = 0
    while remaining > 1e-9 and guard < 10000:
        guard += 1
        if ct.weekday() >= 5:
            nxt = (ct + timedelta(days=(7 - ct.weekday()))).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            ct = nxt
            continue
        nxt = (ct + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        avail = (nxt - ct).total_seconds() / 3600.0
        if remaining <= avail + 1e-9:
            ct = ct + timedelta(seconds=remaining * 3600.0)
            remaining = 0.0
        else:
            remaining -= avail
            ct = nxt
    return ct


def window_for(now: datetime, days: int) -> tuple:
    now = now.astimezone(CT)
    days = max(1, int(days))
    found = []
    cursor = now.date()
    while len(found) < days:
        if cursor.weekday() < 5:
            found.append(cursor)
        cursor -= timedelta(days=1)
    start_day = found[-1]
    start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=CT)
    biz = 0
    walk = start.date()
    end_day = now.date()
    while walk <= end_day:
        if walk.weekday() < 5:
            biz += 1
        walk += timedelta(days=1)
    return start, now, biz


def norm_key(value: str) -> str:
    return str(value or "").strip().strip("<>").lower()


def key_hash(raw: str) -> str:
    return hashlib.sha1(str(raw).encode("utf-8", "replace")).hexdigest()[:10]


def read_jsonl(path: Path) -> list:
    rows = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def mask_stored(text: str) -> str:
    if not text:
        return ""
    out = str(text)
    cut = None
    for match in re.finditer(r"(?i)\b(?:password|code)\s*:", out):
        cut = match.end() if cut is None else min(cut, match.end())
    if cut is not None:
        out = out[:cut] + " [masked]"
    out = re.sub(r"\d{6,}", "[digits]", out)
    return out


def clip(text: str, n: int) -> str:
    text = mask_stored(text or "")
    if len(text) <= n:
        return text
    return text[:n]


def _observer():
    if not _OBS["tried"]:
        _OBS["tried"] = True
        try:
            import importlib.util

            path = Path(__file__).resolve().parent / "draft_outcome_observer.py"
            cli = str(path.parent)
            if cli not in sys.path:
                sys.path.insert(0, cli)
            spec = importlib.util.spec_from_file_location("draft_outcome_observer_value", path)
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _OBS["mod"] = mod
        except Exception:
            _OBS["mod"] = None
    return _OBS["mod"]


def authored(text: str) -> str:
    mod = _observer()
    if mod is not None:
        try:
            return mod.authored(text)
        except Exception:
            pass
    body = str(text or "").replace("\r\n", "\n").strip()
    cut = len(body)
    for pattern in _BOUND:
        match = pattern.search(body)
        if match:
            cut = min(cut, match.start())
    return body[:cut].strip()


def strip_greeting(text: str) -> str:
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return ""
    first = lines[idx].strip()
    if _GREET_RE.match(first) and len(first) <= 80:
        words = first.replace("—", "--").split()
        if first.endswith((",", "--", ":", "-")) or len(words) <= 6:
            return "\n".join(lines[idx + 1 :]).strip()
    return str(text or "").strip()


def tokens(text: str) -> list:
    norm = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return []
    return [part for part in norm.split(" ") if part]


def similarity(draft: str, sent: str) -> float:
    left = tokens(strip_greeting(authored(draft)))
    right = tokens(strip_greeting(authored(sent)))
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    overlap = len(set(left) & set(right)) / max(len(set(left)), len(set(right)))
    return max(ratio, overlap)


def class_from_score(score: float) -> str:
    if score >= 0.9:
        return "used_as_is"
    if score >= 0.6:
        return "light_edit"
    return "rewritten"


def sensitive(cands: list, final: str = "", extra: dict | None = None) -> bool:
    labels = []
    secrets = []
    for cand in cands:
        labels.extend([cand.get("thread") or "", cand.get("actor") or "", cand.get("source_text") or ""])
        secrets.extend(
            [
                cand.get("draft") or "",
                cand.get("thread") or "",
                cand.get("actor") or "",
                cand.get("source_text") or "",
            ]
        )
    extra = extra or {}
    for key in ("thread", "actor", "source_text"):
        labels.append(str(extra.get(key) or ""))
        secrets.append(str(extra.get(key) or ""))
    secrets.append(final or "")
    if _LABEL_RE.search("\n".join(labels)):
        return True
    if _SECRET_RE.search("\n".join(secrets)):
        return True
    return False


def paths_for(home: Path) -> dict:
    learn = home / "claude-stack" / "data" / "learning"
    return {
        "events": learn / "draft_feedback_events.jsonl",
        "pending": learn / "draft_feedback_pending.json",
        "verdicts": home / "llm-appliance" / "runtime" / "local-cycle-browser" / "draft_verdicts.jsonl",
        "wrong": learn / "draft_wrong_after_send.jsonl",
        "outcomes": learn / "draft_outcomes.jsonl",
        "summary": learn / "draft_value_summary.json",
        "gold": learn / "value_gold_examples.jsonl",
        "shadow": home / "claude-stack" / "data" / "idle_filler" / "value_shadow_queue.jsonl",
        "gold_value": home / "claude-stack" / "data" / "playbooks" / "gold_value.jsonl",
        "use": learn / "idle_artifact_use.jsonl",
        "flag": home / "claude-stack" / "data" / "flags" / "draft_value_off",
        "idle": home / "claude-stack" / "data" / "idle_filler",
        "shadow_lessons": learn / "idle_shadow_lessons.jsonl",
        "neg_lessons": learn / "idle_negative_lessons.jsonl",
    }


def _cand(raw_key: str, surface: str, staged_at: datetime, draft: str, thread: str, actor: str, source_text: str, event_id: str, seq: int) -> dict:
    return {
        "raw_key": str(raw_key or ""),
        "norm": norm_key(raw_key),
        "surface": str(surface or "").casefold(),
        "staged_at": staged_at,
        "draft": str(draft or ""),
        "thread": str(thread or ""),
        "actor": str(actor or ""),
        "source_text": str(source_text or ""),
        "event_id": str(event_id or ""),
        "seq": seq,
    }


def load_events(path: Path) -> tuple:
    staged = []
    outcomes = []
    rows = read_jsonl(path)
    invalidated = set()
    for obj in rows:
        if str(obj.get("event_kind") or "") == "invalidation":
            target = str(obj.get("target_event_id") or "")
            if target:
                invalidated.add(target)
    for seq, obj in enumerate(rows):
        kind = str(obj.get("event_kind") or "")
        if kind == "invalidation":
            continue
        if kind == "runner_disposition":
            if str(obj.get("disposition") or "").casefold() != "staged":
                continue
            draft = str(obj.get("draft_text") or "")
            if not draft.strip():
                continue
            at = parse_ts(obj.get("observed_at"))
            if at is None:
                continue
            staged.append(
                _cand(
                    obj.get("source_key") or "",
                    obj.get("surface") or "",
                    at,
                    draft,
                    obj.get("thread_label") or "",
                    obj.get("actor") or "",
                    obj.get("source_text") or "",
                    obj.get("event_id") or "",
                    seq,
                )
            )
            continue
        if kind:
            continue
        outcome = str(obj.get("outcome") or "").casefold()
        if not outcome:
            continue
        event_id = str(obj.get("event_id") or "")
        if event_id and event_id in invalidated:
            continue
        at = parse_ts(obj.get("observed_at"))
        if at is None:
            continue
        outcomes.append(
            {
                "surface": str(obj.get("surface") or "").casefold(),
                "norm": norm_key(obj.get("source_key") or ""),
                "raw_key": str(obj.get("source_key") or ""),
                "at": at,
                "outcome": outcome,
                "final": str(obj.get("final_text") or ""),
                "draft": str(obj.get("draft_text") or ""),
                "thread": str(obj.get("thread_label") or ""),
                "actor": str(obj.get("actor") or ""),
                "source_text": str(obj.get("source_text") or ""),
                "event_id": event_id or f"anon-outcome-{seq}",
            }
        )
    return staged, outcomes


def load_pending(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return []
    if not isinstance(data, dict):
        return []
    rows = []
    for idx, (key, row) in enumerate(data.items()):
        if not isinstance(row, dict):
            continue
        draft = str(row.get("draft_text") or "")
        if not draft.strip():
            continue
        at = parse_ts(row.get("staged_at") or row.get("observed_at"))
        if at is None:
            continue
        raw = str(row.get("source_key") or key or "")
        rows.append(
            _cand(
                raw,
                row.get("surface") or "",
                at,
                draft,
                row.get("thread_label") or "",
                row.get("actor") or "",
                row.get("source_text") or "",
                row.get("event_id") or "",
                100000 + idx,
            )
        )
    return rows


def load_verdicts(path: Path) -> list:
    out = []
    for obj in read_jsonl(path):
        at = parse_ts(obj.get("ts"))
        verdict = str(obj.get("verdict") or "")
        key = str(obj.get("key") or "")
        if at is None or not verdict or not key:
            continue
        ratio = obj.get("ratio")
        try:
            ratio_f = float(ratio) if ratio is not None and ratio != "" else None
        except (TypeError, ValueError):
            ratio_f = None
        age = obj.get("age_h")
        try:
            age_f = float(age) if age is not None and age != "" else None
        except (TypeError, ValueError):
            age_f = None
        out.append(
            {
                "surface": str(obj.get("surface") or "").casefold(),
                "key": key,
                "at": at,
                "verdict": verdict,
                "ratio": ratio_f,
                "age_h": age_f,
            }
        )
    return out


def load_wrong(path: Path, dry_run: bool) -> dict:
    if not path.is_file():
        rows = [dict(WRONG_SEED)]
        if not dry_run:
            atomic_write(path, json.dumps(WRONG_SEED, ensure_ascii=False) + "\n")
    else:
        rows = read_jsonl(path)
    keys = set()
    event_ids = set()
    for row in rows:
        if row.get("source_key"):
            keys.add(norm_key(row.get("source_key")))
        if row.get("event_id"):
            event_ids.add(str(row.get("event_id")))
    return {"keys": keys, "event_ids": event_ids}


def choose_cand(group: dict, sent_at: datetime | None):
    cands = sorted(group["cands"], key=lambda cand: (cand["staged_at"], cand["seq"]))
    if not cands:
        return None
    if sent_at is None:
        return cands[-1]
    before = [cand for cand in cands if cand["staged_at"] <= sent_at]
    if before:
        return before[-1]
    return None


def _is_sent(outcome: dict) -> bool:
    return bool(outcome.get("final")) and str(outcome.get("outcome") or "").startswith("sent")


def assign_outcomes(groups: list, outcomes: list) -> dict:
    by_key = {}
    for group in groups:
        by_key.setdefault((group["surface"], group["norm"]), []).append(group)
    send_for = {}
    neg_for = {}
    late_for = {}
    for outcome in outcomes:
        matched = by_key.get((outcome["surface"], outcome["norm"]), [])
        if not matched:
            continue
        if _is_sent(outcome):
            qual = []
            late = []
            for group in matched:
                cand = choose_cand(group, outcome["at"])
                if cand is None:
                    continue
                deadline = add_business_hours(cand["staged_at"], REPLY_BIZ_HOURS)
                if cand["staged_at"] <= outcome["at"] <= deadline:
                    qual.append((cand["staged_at"], group))
                elif outcome["at"] > deadline:
                    late.append((cand["staged_at"], group))
            if qual:
                qual.sort(key=lambda item: item[0])
                group = qual[-1][1]
                prev = send_for.get(group["id"])
                if prev is None or outcome["at"] >= prev["at"]:
                    send_for[group["id"]] = outcome
                continue
            if late:
                late.sort(key=lambda item: item[0])
                group = late[-1][1]
                prev = late_for.get(group["id"])
                if prev is None or outcome["at"] < prev["at"]:
                    late_for[group["id"]] = outcome
            continue
        if outcome["outcome"] in NOT_USED_OUTCOMES:
            qual = []
            for group in matched:
                earliest = min(cand["staged_at"] for cand in group["cands"])
                if earliest <= outcome["at"]:
                    qual.append(group)
            if not qual:
                continue
            group = max(qual, key=lambda item: max(cand["staged_at"] for cand in item["cands"]))
            prev = neg_for.get(group["id"])
            if prev is None or outcome["at"] >= prev["at"]:
                neg_for[group["id"]] = outcome
    return {"send": send_for, "neg": neg_for, "late": late_for}


def assign_verdicts(groups: list, verdicts: list) -> dict:
    chosen = {}
    for verdict in verdicts:
        matched = []
        for group in groups:
            if verdict["surface"] and verdict["surface"] != group["surface"]:
                continue
            if verdict["key"] not in group["hashes"]:
                continue
            earliest = min(cand["staged_at"] for cand in group["cands"])
            if verdict["at"] >= earliest:
                matched.append(group)
        if not matched:
            continue
        group = max(matched, key=lambda item: max(cand["staged_at"] for cand in item["cands"]))
        prev = chosen.get(group["id"])
        if prev is None or verdict["at"] >= prev["at"]:
            chosen[group["id"]] = verdict
    return chosen


def superseding_send(group: dict, outcomes: list):
    earliest = min(cand["staged_at"] for cand in group["cands"])
    before = [
        outcome
        for outcome in outcomes
        if outcome["surface"] == group["surface"]
        and outcome["norm"] == group["norm"]
        and _is_sent(outcome)
        and outcome["at"] < earliest
    ]
    if not before:
        return None
    return max(before, key=lambda outcome: outcome["at"])


def verdict_in_window(verdict: dict, staged: datetime) -> bool:
    deadline = add_business_hours(staged, REPLY_BIZ_HOURS)
    if verdict.get("age_h") is not None:
        implied = staged + timedelta(hours=float(verdict["age_h"]))
        if staged <= implied <= deadline:
            return True
    return staged <= verdict["at"] <= deadline


def map_verdict(verdict: dict, staged: datetime) -> dict | None:
    name = verdict["verdict"]
    if name in EXCLUDED_VERDICTS:
        return {"class": "excluded", "score": None, "source": "verdict", "sent_at": None, "sent_text": ""}
    if name in NOT_USED_VERDICTS:
        return {"class": "not_used", "score": None, "source": "verdict", "sent_at": None, "sent_text": ""}
    if not str(name).startswith("sent"):
        return None
    if not verdict_in_window(verdict, staged):
        return {
            "class": "not_used",
            "score": verdict.get("ratio"),
            "source": "verdict",
            "sent_at": None,
            "sent_text": "",
        }
    if verdict.get("ratio") is not None:
        score = float(verdict["ratio"])
    else:
        score = LABEL_SCORE.get(name, 0.3)
    return {
        "class": class_from_score(score),
        "score": score,
        "source": "verdict",
        "sent_at": None,
        "sent_text": "",
    }


def _payload_sent_time(payload: dict) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    sent_ids = {str(item) for item in (payload.get("sent_ids") or [])}
    replies = [row for row in (payload.get("sent_replies") or []) if isinstance(row, dict)]
    chosen = [row for row in replies if not sent_ids or str(row.get("id") or "") in sent_ids]
    if not chosen:
        chosen = replies
    found = []
    blobs = list(chosen)
    blobs.extend(row for row in (payload.get("sent_receipts") or []) if isinstance(row, dict))
    for row in blobs:
        for key in SENT_TIME_KEYS:
            if row.get(key):
                stamp = parse_ts(row.get(key))
                if stamp is not None:
                    found.append(stamp)
    if not found:
        return None
    return min(found)


def _zoom_sent_time(messages: list, staged_at: datetime) -> datetime | None:
    staged_ts = staged_at.timestamp()
    found = []
    for msg in messages or []:
        if not isinstance(msg, dict) or not msg.get("from_self"):
            continue
        try:
            raw = float(msg.get("t"))
        except (TypeError, ValueError):
            continue
        if raw > 1e11:
            raw = raw / 1000.0
        if raw > staged_ts:
            found.append(raw)
    if not found:
        return None
    return datetime.fromtimestamp(min(found), timezone.utc)


def _lazy_feed(name: str):
    import importlib

    cli = str(Path(__file__).resolve().parent)
    if cli not in sys.path:
        sys.path.insert(0, cli)
    feeds = importlib.import_module("ops_safari_feeds")
    return getattr(feeds, name)


def live_resolve(group: dict, cand: dict, now: datetime, ctx: dict) -> dict | None:
    """Read-only helper. Any exception leaves the draft unresolved."""
    mod = _observer()
    if mod is None:
        return None
    surface = cand["surface"]
    try:
        if surface == "zoom":
            zoom_fn = ctx.get("zoom") or _lazy_feed("_zoom_digest_msgs_for_person")
            messages = zoom_fn(cand["raw_key"], cand.get("thread") or "") or []
            decided = mod.decide_zoom(messages, cand["staged_at"].timestamp(), cand.get("draft") or "", now.timestamp())
            sent_at = _zoom_sent_time(messages, cand["staged_at"])
        else:
            if ctx["calls"] >= ctx["max"]:
                return None
            ctx["calls"] += 1
            inspect = ctx.get("inspect") or _lazy_feed("_owa_reply_inspect")
            payload = inspect(cand["raw_key"])
            if not isinstance(payload, dict):
                payload = {}
            age = max(0.0, (now - cand["staged_at"]).total_seconds())
            decided = mod.decide_outlook(payload, {}, age)
            sent_at = _payload_sent_time(payload)
            messages = None
    except Exception:
        return None
    if not isinstance(decided, dict):
        return None
    verdict = str(decided.get("verdict") or "")
    if verdict == "error" and decided.get("terminal"):
        return {"class": "excluded", "score": None, "source": "live", "sent_at": None, "sent_text": ""}
    if verdict == "error":
        return None
    if verdict in {"deleted", "gone_unproven"}:
        return {"class": "not_used", "score": None, "source": "live", "sent_at": None, "sent_text": ""}
    if verdict != "sent":
        return None
    final = str(decided.get("final") or "")
    deadline = add_business_hours(cand["staged_at"], REPLY_BIZ_HOURS)
    if sent_at is not None and sent_at < cand["staged_at"]:
        score = similarity(cand.get("draft") or "", final) if final else None
        return {
            "class": "superseded",
            "score": score,
            "source": "live",
            "sent_at": sent_at,
            "sent_text": final,
        }
    if sent_at is not None and sent_at > deadline:
        score = similarity(cand.get("draft") or "", final) if final else None
        return {"class": "not_used", "score": score, "source": "live", "sent_at": sent_at, "sent_text": final}
    # sent_at unknown: a proven operator send still counts (observer semantics), scored by text.
    if not final:
        return None
    score = similarity(cand.get("draft") or "", final)
    return {
        "class": class_from_score(score),
        "score": score,
        "source": "live",
        "sent_at": sent_at,
        "sent_text": final,
    }


def _wrong_hit(group: dict, outcome: dict | None, wrongs: dict) -> bool:
    ids = {cand["event_id"] for cand in group["cands"] if cand.get("event_id")}
    if outcome and outcome.get("event_id"):
        ids.add(str(outcome["event_id"]))
    if ids & wrongs["event_ids"]:
        return True
    return group["norm"] in wrongs["keys"]


def _blank_counts() -> dict:
    return {name: 0 for name in CLASSES}


def build_groups(cands: list, start: datetime, end: datetime) -> list:
    groups = {}
    for cand in cands:
        if not (start <= cand["staged_at"] <= end):
            continue
        if not cand["norm"] or not cand["surface"]:
            continue
        day = cand["staged_at"].astimezone(CT).date().isoformat()
        gid = f"{cand['surface']}|{cand['norm']}|{day}"
        group = groups.get(gid)
        if group is None:
            group = {
                "id": gid,
                "surface": cand["surface"],
                "norm": cand["norm"],
                "cands": [],
                "hashes": set(),
            }
            groups[gid] = group
        group["cands"].append(cand)
        if cand["raw_key"]:
            group["hashes"].add(key_hash(cand["raw_key"]))
    return list(groups.values())


def classify_all(groups: list, outcomes: list, verdicts: list, wrongs: dict, now: datetime, live: bool, ctx: dict) -> list:
    assigned = assign_outcomes(groups, outcomes)
    verdict_for = assign_verdicts(groups, verdicts)
    rows = []
    for group in groups:
        outcome = None
        cand = None
        sent_text = ""
        sent_at = None
        score = None
        source = "none"
        cls = None
        if group["id"] in assigned["send"]:
            outcome = assigned["send"][group["id"]]
            cand = choose_cand(group, outcome["at"]) or choose_cand(group, None)
            if outcome.get("draft"):
                # score the draft that was actually in the mailbox when operator sent (restages differ)
                cand = {**cand, "draft": outcome["draft"]}
            sent_text = outcome["final"]
            sent_at = outcome["at"]
            score = similarity(cand["draft"], sent_text)
            cls = class_from_score(score)
            source = "outcome"
        elif group["id"] in assigned["neg"]:
            outcome = assigned["neg"][group["id"]]
            cand = choose_cand(group, None)
            cls = "not_used"
            source = "outcome"
        else:
            prior = superseding_send(group, outcomes)
            if prior is not None:
                outcome = prior
                cand = choose_cand(group, None)
                sent_text = prior["final"]
                sent_at = prior["at"]
                score = similarity(cand["draft"], sent_text) if sent_text else None
                cls = "superseded"
                source = "outcome"
            elif group["id"] in assigned["late"]:
                outcome = assigned["late"][group["id"]]
                cand = choose_cand(group, outcome["at"]) or choose_cand(group, None)
                sent_text = outcome["final"]
                sent_at = outcome["at"]
                score = similarity(cand["draft"], sent_text) if sent_text else None
                cls = "not_used"
                source = "outcome"
            else:
                verdict = verdict_for.get(group["id"])
                cand = choose_cand(group, None)
                if verdict is not None and cand is not None:
                    mapped = map_verdict(verdict, cand["staged_at"])
                    if mapped is not None:
                        cls = mapped["class"]
                        score = mapped["score"]
                        source = mapped["source"]
                        sent_at = mapped["sent_at"]
                        sent_text = mapped["sent_text"]
        if cand is None:
            cand = choose_cand(group, None)
        extra = {}
        if outcome:
            extra = {
                "thread": outcome.get("thread") or "",
                "actor": outcome.get("actor") or "",
                "source_text": outcome.get("source_text") or "",
            }
        if sensitive(group["cands"], sent_text, extra):
            rows.append(_row(group, cand, "skipped_sensitive", None, None, "", "sensitive", False, 0.0))
            continue
        if cls is None and live and cand is not None:
            try:
                lived = live_resolve(group, cand, now, ctx)
            except Exception:
                lived = None
            if lived:
                cls = lived["class"]
                score = lived["score"]
                source = lived["source"]
                sent_at = lived["sent_at"]
                sent_text = lived["sent_text"] or sent_text
        if cls is None:
            deadline = add_business_hours(cand["staged_at"], REPLY_BIZ_HOURS)
            cls = "pending" if now < deadline else "not_used"
            source = "none"
        wrong = _wrong_hit(group, outcome, wrongs)
        table = minutes_table()
        minutes = table["wrong_after_send"] if wrong else table.get(cls, 0.0)
        rows.append(_row(group, cand, cls, score, sent_at, sent_text, source, wrong, minutes))
    rows.sort(key=lambda row: row["staged_at"] or "")
    return rows


def _row(group, cand, cls, score, sent_at, sent_text, source, wrong, minutes) -> dict:
    if cls == "skipped_sensitive":
        return {
            "surface": group["surface"],
            "key_hash": key_hash(cand["raw_key"]) if cand and cand.get("raw_key") else "",
            "thread_label": "",
            "staged_at": iso_utc(cand["staged_at"]) if cand else None,
            "sent_at": None,
            "class": cls,
            "score": None,
            "minutes": 0.0,
            "wrong_after_send": False,
            "resolution_source": "sensitive",
            "draft_text": "",
            "sent_text": "",
        }
    scored = None if score is None else round(float(score), 4)
    return {
        "surface": group["surface"],
        "key_hash": key_hash(cand["raw_key"]),
        "thread_label": clip(cand.get("thread") or "", LABEL_MAX),
        "staged_at": iso_utc(cand["staged_at"]),
        "sent_at": iso_utc(sent_at),
        "class": cls,
        "score": scored,
        "minutes": round(float(minutes), 4),
        "wrong_after_send": bool(wrong),
        "resolution_source": source,
        "draft_text": clip(cand.get("draft") or "", TEXT_MAX),
        "sent_text": clip(sent_text or "", TEXT_MAX),
    }


def _stamp_in(obj: dict, cutoff: datetime, end: datetime) -> bool:
    for key in ("ts", "observed_at", "generated_at", "time", "staged_at"):
        if key in obj:
            stamp = parse_ts(obj.get(key))
            if stamp is not None:
                return cutoff <= stamp <= end
    day = obj.get("day")
    if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.strip()):
        stamp = parse_ts(day.strip() + "T12:00:00")
        if stamp is not None and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=CT)
        if stamp is not None:
            return cutoff <= stamp <= end
    return False


def count_produced(home_paths: dict, now: datetime) -> int:
    cutoff = now - timedelta(hours=24)
    end = now + timedelta(minutes=5)
    total = 0
    files = []
    root = home_paths["idle"]
    if root.is_dir():
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".json", ".jsonl"}:
                continue
            if path.name.endswith(".tmp") or path.name in {"state.json", "index.json"}:
                continue
            files.append(path)
    for extra in (home_paths["shadow_lessons"], home_paths["neg_lessons"]):
        if extra.is_file():
            files.append(extra)
    seen = set()
    for path in files:
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.suffix == ".jsonl":
            for obj in read_jsonl(path):
                if _stamp_in(obj, cutoff, end):
                    total += 1
            continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            obj = None
        if isinstance(obj, dict) and _stamp_in(obj, cutoff, end):
            total += 1
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if cutoff <= mtime <= end:
            total += 1
    return total


def count_consumed(path: Path, now: datetime) -> tuple:
    if not path.is_file():
        return None, (
            "consumed is null: idle_artifact_use.jsonl does not exist. "
            "draft_fewshot and continuous_learning append a row when a value_gold example or an "
            "idle shadow/negative lesson is injected into a draft prompt; none yet, so consumption is unmeasurable."
        )
    cutoff = now - timedelta(hours=24)
    end = now + timedelta(minutes=5)
    total = 0
    for obj in read_jsonl(path):
        if _stamp_in(obj, cutoff, end):
            total += 1
    detail = (
        "consumed_24h counts idle_artifact_use.jsonl rows in the last 24 wall-clock hours. "
        "One row per value_gold example (kind=value_gold) or idle lesson (kind=idle_shadow / "
        "idle_negative) that survived into a draft prompt, written by draft_fewshot.build_block "
        "and continuous_learning.prompt_block."
    )
    return total, detail


def self_graded(path: Path, start: datetime, end: datetime) -> dict:
    totals = []
    for obj in read_jsonl(path):
        raw = obj.get("day") or obj.get("ts")
        if isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw.strip()):
            try:
                # date.fromisoformat / strptime: 3.9 datetime.fromisoformat rejects a bare date.
                day = datetime.strptime(raw.strip(), "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (start.date() <= day <= end.date()):
                continue
        else:
            stamp = parse_ts(raw)
            if stamp is None or not (start <= stamp <= end):
                continue
        try:
            totals.append(float(obj.get("total")))
        except (TypeError, ValueError):
            continue
    if not totals:
        return {"n": 0, "mean": None}
    return {"n": len(totals), "mean": round(sum(totals) / len(totals), 3)}


def _work_loop_block(now: datetime, home_paths: dict) -> dict:
    """Last-24h counts from work-loop/value-ledger/worktime.jsonl. Missing file is zeros."""
    empty = {
        "artifacts_finished": 0,
        "by_kind": {},
        "noops_skipped": 0,
        "suppressed": 0,
        "tasks_run": 0,
        "detail": "worktime ledger missing or empty",
    }
    paths = []
    env = os.environ.get("WORK_LOOP_DIR", "").strip()
    if env:
        paths.append(Path(env) / "value-ledger" / "worktime.jsonl")
    else:
        if isinstance(home_paths, dict):
            for key in ("events", "idle"):
                raw = home_paths.get(key)
                if not raw:
                    continue
                try:
                    home = Path(raw).parents[3]
                except Exception:
                    continue
                paths.append(home / "llm-appliance" / "runtime" / "work-loop" / "value-ledger" / "worktime.jsonl")
        paths.append(Path.home() / "llm-appliance" / "runtime" / "work-loop" / "value-ledger" / "worktime.jsonl")
    path = next((p for p in paths if p.exists()), None)
    if path is None:
        return empty
    try:
        end = now if isinstance(now, datetime) else datetime.now(timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=CT)
        cutoff = end.astimezone(timezone.utc) - timedelta(hours=24)
    except Exception:
        return empty
    artifacts = 0
    by_kind: dict = {}
    noops = 0
    suppressed = 0
    tasks = 0
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return empty
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        stamp = parse_ts(rec.get("ts"))
        if stamp is None or stamp < cutoff or stamp > end.astimezone(timezone.utc) + timedelta(seconds=5):
            continue
        event = str(rec.get("event") or "")
        if event == "artifact_finished":
            artifacts += 1
            kind = str(rec.get("kind") or "unknown")
            by_kind[kind] = by_kind.get(kind, 0) + 1
        elif event == "noop_skipped":
            noops += 1
        elif event == "suppressed":
            suppressed += 1
        elif event == "task_run":
            tasks += 1
    return {
        "artifacts_finished": artifacts,
        "by_kind": by_kind,
        "noops_skipped": noops,
        "suppressed": suppressed,
        "tasks_run": tasks,
        "detail": (
            f"last 24h work-loop ledger: {artifacts} artifacts, {noops} noops skipped, "
            f"{suppressed} suppressed, {tasks} tasks run"
        ),
    }


def summarize(rows: list, start: datetime, end: datetime, biz: int, now: datetime, home_paths: dict) -> dict:
    counts = _blank_counts()
    per_day = {}
    per_surface = {
        "outlook": {"staged": 0, "counts": _blank_counts(), "minutes": 0.0},
        "zoom": {"staged": 0, "counts": _blank_counts(), "minutes": 0.0},
    }
    for row in rows:
        cls = row["class"]
        counts[cls] = counts.get(cls, 0) + 1
        day = ""
        stamp = parse_ts(row.get("staged_at"))
        if stamp is not None:
            day = stamp.astimezone(CT).date().isoformat()
        bucket = per_day.setdefault(day, {"counts": _blank_counts(), "minutes": 0.0})
        bucket["counts"][cls] = bucket["counts"].get(cls, 0) + 1
        bucket["minutes"] = round(bucket["minutes"] + float(row["minutes"]), 4)
        surface = row["surface"] if row["surface"] in per_surface else None
        if surface:
            per_surface[surface]["staged"] += 1
            per_surface[surface]["counts"][cls] = per_surface[surface]["counts"].get(cls, 0) + 1
            per_surface[surface]["minutes"] = round(
                per_surface[surface]["minutes"] + float(row["minutes"]), 4
            )
    staged = len(rows)
    pending = counts.get("pending", 0)
    excluded = counts.get("excluded", 0)
    skipped = counts.get("skipped_sensitive", 0)
    denom = staged - pending - excluded - skipped
    pct = {}
    for name in PCT_CLASSES:
        pct[name] = None if denom <= 0 else round(100.0 * counts.get(name, 0) / denom, 1)
    produced = count_produced(home_paths, now)
    consumed, consumed_detail = count_consumed(home_paths["use"], now)
    table = minutes_table()
    return {
        "generated_at": iso_utc(datetime.now(timezone.utc)),
        "window": {"start": iso_ct(start), "end": iso_ct(end), "business_days": biz},
        "staged": staged,
        "resolved": denom,
        "pending": pending,
        "excluded": excluded,
        "skipped_sensitive": skipped,
        "counts": counts,
        "pct": pct,
        "minutes_saved": round(sum(float(row["minutes"]) for row in rows), 4),
        "minutes_assumptions": {
            "used_as_is": table["used_as_is"],
            "light_edit": table["light_edit"],
            "rewritten": table["rewritten"],
            "not_used": table["not_used"],
            "superseded": 0.0,
            "pending": 0.0,
            "excluded": 0.0,
            "skipped_sensitive": 0.0,
            "wrong_after_send": table["wrong_after_send"],
            "unit": "minutes",
            "reply_business_hours": REPLY_BIZ_HOURS,
            "business_hours": "Mon-Fri elapsed hours in America/Chicago; weekends add 0; holidays count as weekdays",
            "env": [
                "DRAFT_VALUE_MIN_USED_AS_IS",
                "DRAFT_VALUE_MIN_LIGHT_EDIT",
                "DRAFT_VALUE_MIN_REWRITTEN",
                "DRAFT_VALUE_MIN_NOT_USED",
                "DRAFT_VALUE_MIN_WRONG",
            ],
            "excluded_verdicts": sorted(EXCLUDED_VERDICTS),
            "note": (
                "wrong_after_send replaces class minutes with MIN_WRONG. "
                "Percentages omit pending, excluded, and skipped_sensitive. "
                "excluded verdicts are superseded_by_newer_inbound, invalid_source_id, "
                "and expired_no_verdict (not a measured miss)."
            ),
        },
        "per_day": per_day,
        "per_surface": per_surface,
        "idle_artifacts": {
            "produced_24h": produced,
            "consumed_24h": consumed,
            "detail": (
                "produced_24h counts timestamped jsonl rows and json artifacts under "
                "claude-stack/data/idle_filler (state.json, index.json, and tmp files omitted) "
                "plus learning/idle_shadow_lessons.jsonl and idle_negative_lessons.jsonl "
                "whose timestamps fall in the last 24 wall-clock hours. "
                + consumed_detail
            ),
        },
        "self_graded": self_graded(home_paths["gold_value"], start, end),
        "work_loop": _work_loop_block(now, home_paths),
    }


def _keep_prior_live(path: Path, rows: list) -> list:
    """Offline reruns must not erase earlier live (Sent Items / Zoom digest) resolutions.

    A prior row resolved live wins over a new row resolved only by verdict label or by
    timeout ('none'); outcome-row and fresh live resolutions always win."""
    prior = {}
    for obj in read_jsonl(path):
        if isinstance(obj, dict) and obj.get("resolution_source") == "live":
            prior[(obj.get("surface"), obj.get("key_hash"), obj.get("staged_at"))] = obj
    if not prior:
        return rows
    table = minutes_table()
    out = []
    for row in rows:
        old = prior.get((row.get("surface"), row.get("key_hash"), row.get("staged_at")))
        if old and row.get("resolution_source") in ("none", "verdict") and row.get("class") != "skipped_sensitive":
            keep = dict(old)
            keep["wrong_after_send"] = bool(row.get("wrong_after_send") or old.get("wrong_after_send"))
            keep["minutes"] = table["wrong_after_send"] if keep["wrong_after_send"] else table.get(keep.get("class"), 0.0)
            out.append(keep)
        else:
            out.append(row)
    return out


def merge_outcomes(path: Path, rows: list, start: datetime, end: datetime) -> None:
    kept = []
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                kept.append(raw)
                continue
            if not isinstance(obj, dict):
                kept.append(raw)
                continue
            stamp = parse_ts(obj.get("staged_at"))
            if stamp is not None and start <= stamp <= end:
                continue
            kept.append(raw)
    fresh = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows]
    body = "\n".join(kept + fresh)
    if body:
        body += "\n"
    atomic_write(path, body)


def _append_new(path: Path, rows: list, key_fn) -> None:
    if not rows:
        return
    seen = set()
    if path.is_file():
        for obj in read_jsonl(path):
            seen.add(key_fn(obj))
    fresh = []
    for row in rows:
        key = key_fn(row)
        if not key or key in seen:
            continue
        seen.add(key)
        fresh.append(json.dumps(row, ensure_ascii=False) + "\n")
    if not fresh:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(fresh)


def write_feedback(rows: list, home_paths: dict, now: datetime) -> None:
    gold = []
    shadow = []
    stamp = iso_utc(now)
    for row in rows:
        if row["class"] == "skipped_sensitive":
            continue
        if row["class"] in {"used_as_is", "light_edit"} and not row["wrong_after_send"]:
            if row["draft_text"].strip() and row["sent_text"].strip():
                gold.append(
                    {
                        "key_hash": row["key_hash"],
                        "surface": row["surface"],
                        "thread_label": row["thread_label"],
                        "draft_text": row["draft_text"],
                        "sent_text": row["sent_text"],
                        "class": row["class"],
                        "score": row["score"],
                        "staged_at": row["staged_at"],
                        "ts": stamp,
                    }
                )
        if row["wrong_after_send"] or row["class"] in {"rewritten", "light_edit", "not_used"}:
            if row["draft_text"].strip():
                shadow.append(
                    {
                        "pair_id": f"{row['key_hash']}|{row['staged_at']}",
                        "draft_text": row["draft_text"],
                        "sent_text": row["sent_text"],
                        "thread_label": row["thread_label"],
                        "class": "wrong_after_send" if row["wrong_after_send"] else row["class"],
                        "wrong_after_send": bool(row["wrong_after_send"]),
                    }
                )
    _append_new(
        home_paths["gold"],
        gold,
        lambda obj: f"{obj.get('key_hash')}|{obj.get('staged_at') or obj.get('ts')}",
    )
    _append_new(home_paths["shadow"], shadow, lambda obj: str(obj.get("pair_id") or ""))


_PREF_CONTEXT_MAX = 1500


def _pref_paths(home: Path) -> tuple:
    learn = home / "claude-stack" / "data" / "learning"
    return learn / "pref_pairs.jsonl", learn / "pref_pairs_heldout.jsonl"


def _pref_split(key: str) -> str:
    digest = hashlib.sha1(b"pref|" + str(key).encode("utf-8", "replace")).hexdigest()
    if int(digest[:8], 16) % 5 == 0:
        return "heldout"
    return "train"


def _frozen_pref(home: Path) -> tuple:
    """Ledger hashes of frozen ids, plus ticket tokens. Fail-open to empty."""
    hashes = set()
    tickets = []
    path = home / "claude-stack/data/learning/eval_frozen/exclude.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return hashes, tickets
    for raw in list(doc.get("event_ids") or []) + list(doc.get("source_keys") or []):
        hashes.add(key_hash(str(raw)))
    for raw in doc.get("tickets") or []:
        text = str(raw).strip()
        if text:
            tickets.append(text)
    return hashes, tickets


def _sensitive_pref_hashes(path: Path) -> set:
    out = set()
    for row in read_jsonl(path):
        if row.get("class") == "skipped_sensitive" and row.get("key_hash"):
            out.add(str(row["key_hash"]))
    return out


def _context_by_hash(path: Path) -> dict:
    idx = {}
    for obj in read_jsonl(path):
        text = str(obj.get("source_text") or "")
        if not text.strip():
            continue
        for raw in (obj.get("source_key"), obj.get("event_id")):
            if not raw:
                continue
            hashed = key_hash(str(raw))
            if len(text) > len(idx.get(hashed) or ""):
                idx[hashed] = text
    return idx


def _ticket_in(blob: str, tickets: list) -> bool:
    for ticket in tickets:
        if len(ticket) < 3:
            continue
        if re.search(r"(?<!\w)" + re.escape(ticket) + r"(?!\w)", blob):
            return True
    return False


def _line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())


def _chmod_private(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except OSError:
        pass


def capture_pref_pairs(home: Path, rows: list | None = None) -> dict:
    """Append rewritten and used-as-is pairs. No training and no live mail lookup."""
    home = Path(home)
    home_paths = paths_for(home)
    train_path, hold_path = _pref_paths(home)
    if rows is None:
        rows = read_jsonl(home_paths["outcomes"])
    frozen_h, tickets = _frozen_pref(home)
    sensitive_h = _sensitive_pref_hashes(home_paths["outcomes"])
    contexts = _context_by_hash(home_paths["events"])
    counts = {
        "eligible": 0,
        "skipped_sensitive": 0,
        "skipped_frozen": 0,
        "skipped_empty": 0,
        "skipped_class": 0,
        "train": 0,
        "heldout": 0,
    }
    train_rows = []
    hold_rows = []
    for row in rows or []:
        cls = str(row.get("class") or "")
        if cls not in {"rewritten", "used_as_is"}:
            counts["skipped_class"] += 1
            continue
        key = str(row.get("key_hash") or "")
        draft = str(row.get("draft_text") or row.get("draft") or "")
        sent = str(row.get("sent_text") or row.get("sent") or "")
        context = contexts.get(key) or str(row.get("thread_label") or "")
        if key in sensitive_h or sensitive(
            [{"draft": draft, "thread": str(row.get("thread_label") or ""), "actor": "", "source_text": context}],
            sent,
        ):
            counts["skipped_sensitive"] += 1
            continue
        blob = "\n".join([draft, sent, context, str(row.get("thread_label") or "")])
        if (key and key in frozen_h) or _ticket_in(blob, tickets):
            counts["skipped_frozen"] += 1
            continue
        if not key or not draft.strip() or not sent.strip():
            counts["skipped_empty"] += 1
            continue
        counts["eligible"] += 1
        split = _pref_split(key)
        rec = {
            "key_hash": key,
            "class": cls,
            "split": split,
            "context": clip(context, _PREF_CONTEXT_MAX),
            "draft": clip(draft, TEXT_MAX),
            "sent": clip(sent, TEXT_MAX),
            "score": row.get("score"),
        }
        if split == "heldout":
            counts["heldout"] += 1
            hold_rows.append(rec)
        else:
            counts["train"] += 1
            train_rows.append(rec)
    before_train = _line_count(train_path)
    before_hold = _line_count(hold_path)
    _append_new(train_path, train_rows, lambda obj: str(obj.get("key_hash") or ""))
    _append_new(hold_path, hold_rows, lambda obj: str(obj.get("key_hash") or ""))
    _chmod_private(train_path)
    _chmod_private(hold_path)
    counts["appended_train"] = _line_count(train_path) - before_train
    counts["appended_heldout"] = _line_count(hold_path) - before_hold
    return counts


def run(home: Path | str | None = None, days: int = 5, live: bool = False, max_live: int = 25, dry_run: bool = False, now=None, inspect_fn=None, zoom_fn=None) -> dict:
    home = Path(home).expanduser() if home else Path.home()
    home_paths = paths_for(home)
    if home_paths["flag"].exists():
        return {"enabled": False, "reason": "draft_value_off"}
    now_ct = resolve_now(now)
    start, end, biz = window_for(now_ct, days)
    staged, outcomes = load_events(home_paths["events"])
    staged.extend(load_pending(home_paths["pending"]))
    verdicts = load_verdicts(home_paths["verdicts"])
    wrongs = load_wrong(home_paths["wrong"], dry_run)
    groups = build_groups(staged, start, end)
    ctx = {"calls": 0, "max": max(0, int(max_live)), "inspect": inspect_fn, "zoom": zoom_fn}
    rows = classify_all(groups, outcomes, verdicts, wrongs, now_ct, bool(live), ctx)
    rows = _keep_prior_live(home_paths["outcomes"], rows)
    summary = summarize(rows, start, end, biz, now_ct, home_paths)
    if live:
        summary["live_outlook_calls"] = ctx["calls"]
    if dry_run:
        summary["dry_run"] = True
        return summary
    merge_outcomes(home_paths["outcomes"], rows, start, end)
    atomic_write(home_paths["summary"], json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    write_feedback(rows, home_paths, now_ct)
    try:
        capture_pref_pairs(home, rows)
    except Exception:
        pass
    return summary


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure local-draft value from operator's sent replies")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-live", type=int, default=25)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--capture-pairs", action="store_true")
    args = parser.parse_args(argv)
    if args.capture_pairs:
        print(json.dumps(capture_pref_pairs(Path(args.home).expanduser())))
        return 0
    summary = run(
        home=args.home,
        days=args.days,
        live=args.live,
        max_live=args.max_live,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
