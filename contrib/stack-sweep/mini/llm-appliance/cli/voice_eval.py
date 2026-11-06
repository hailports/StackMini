"""Held-out voice eval for the local draft model. One item at a time.

Reads voice_pairs.jsonl. Writes scores and a private draft cache next to it.
Does not send mail or chat. Does not call any model except 127.0.0.1:8080.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import voice_score as vs

HOME = Path.home()
LEARN = HOME / "claude-stack/data/learning"
PAIRS = LEARN / "voice_pairs.jsonl"
HELD = LEARN / "voice_eval_heldout.jsonl"
DRAFTS = LEARN / "voice_eval_drafts.jsonl"
SCORES = LEARN / "voice_eval_scores.json"
PINS = LEARN / "pinned_fewshots.json"
RULES = LEARN / "PLAIN_VOICE_RULES.md"
HOLDOUT_N = 60


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def split_rows(rows: list[dict], n_hold: int) -> tuple[list[dict], list[dict]]:
    rows = sorted(rows, key=lambda r: str(r.get("id") or ""))
    if len(rows) <= n_hold:
        return rows, []
    step = len(rows) / n_hold
    hold_at = {min(len(rows) - 1, int(i * step)) for i in range(n_hold)}
    # If rounding collides, fill from the front.
    i = 0
    while len(hold_at) < n_hold and i < len(rows):
        hold_at.add(i)
        i += 1
    hold, train = [], []
    for idx, row in enumerate(rows):
        (hold if idx in hold_at else train).append(row)
    return hold, train


def stamp(path: Path) -> tuple:
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def slot_busy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/slots", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return True
    if not isinstance(data, list):
        return True
    return any(bool(slot.get("is_processing")) for slot in data)


def wait_idle(max_wait: float = None) -> None:
    """Wait until the local slot is idle so chat and the draft runner go first."""
    if max_wait is None:
        max_wait = float(os.environ.get("VOICE_EVAL_WAIT_S") or "120")
    start = time.time()
    announced = 0.0
    while slot_busy():
        if time.time() - start >= max_wait:
            return
        now = time.time()
        if now - announced > 30:
            print("waiting for an idle local slot", flush=True)
            announced = now
        time.sleep(0.12)


def build_messages(row: dict) -> list[dict]:
    cli = str(HOME / "llm-appliance/cli")
    if cli not in sys.path:
        sys.path.insert(0, cli)
    import draft_fewshot as fs
    import grounded_drafter as gd
    import tells_gate

    incoming = str(row.get("incoming") or "")
    channel = str(row.get("channel") or "outlook")
    first = str(row.get("first") or "there").strip() or "there"
    system = gd.SYS_MC if gd._mc() else gd.SYS
    style = ""
    try:
        examples = fs.retrieve(incoming, channel, 2)
    except Exception:
        examples = []
    if examples:
        lines = [
            "STYLE EXAMPLES from other threads (voice and length only. none of these facts are true here):"
        ]
        for i, pair in enumerate(examples, 1):
            line = fs.scrub(fs._clip_sentence(str(pair.get("fin") or ""), 260), pair.get("names") or [])
            if line:
                lines.append("[style %d] %s" % (i, line))
        if len(lines) > 1:
            style = "\n".join(lines)
    rules = ""
    try:
        rules = tells_gate.rules_for_prompt(700)
    except Exception:
        rules = ""
    user = gd._gen_prompt_body(
        first,
        ["Reply to the incoming message in the thread."],
        [
            "Use only the incoming message. Do not add a name, number, date, or status it does not state.",
            incoming[:1800],
        ],
        style,
    )
    if rules:
        user = rules + "\n\n" + user
    if channel == "outlook":
        user = user + "\n\n" + gd._OUTLOOK_RULES
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def complete_one(row: dict) -> str:
    cli = str(HOME / "llm-appliance/cli")
    if cli not in sys.path:
        sys.path.insert(0, cli)
    import draft_context as dc

    messages = build_messages(row)
    for _ in range(4):
        wait_idle()
        got = dc.local_complete(messages, 160)
        if got.get("ok") and str(got.get("text") or "").strip():
            return str(got.get("text") or "").strip()
        reason = str(got.get("reason") or "")
        if reason == "preempted" or "503" in reason:
            time.sleep(5)
            continue
        if not got.get("ok"):
            time.sleep(2)
            continue
        return str(got.get("text") or "").strip()
    return ""


def score_row(row: dict, draft: str) -> dict:
    graded = vs.score_reply(
        draft,
        str(row.get("incoming") or ""),
        str(row.get("reply") or ""),
        str(row.get("channel") or "outlook"),
    )
    graded["id"] = row.get("id")
    graded["channel"] = row.get("channel")
    return graded


def cmd_split() -> None:
    rows = _load_jsonl(PAIRS)
    hold, train = split_rows(rows, min(HOLDOUT_N, max(0, len(rows) - 1)))
    if len(hold) < 50:
        raise SystemExit("need 50 held-out pairs, have %d of %d" % (len(hold), len(rows)))
    _write_private(HELD, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in hold))
    by = {}
    for row in hold:
        by[row.get("channel") or "?"] = by.get(row.get("channel") or "?", 0) + 1
    print(json.dumps({"pairs": len(rows), "heldout": len(hold), "train": len(train), "held_by": by}))


def _draft_map() -> dict[str, str]:
    if not DRAFTS.is_file():
        return {}
    out = {}
    for row in _load_jsonl(DRAFTS):
        if row.get("id") and "draft" in row:
            out[str(row["id"])] = str(row.get("draft") or "")
    return out


def _append_draft(row: dict, draft: str, tag: str) -> None:
    line = json.dumps({
        "id": row.get("id"),
        "channel": row.get("channel"),
        "tag": tag,
        "draft": draft,
    }, ensure_ascii=False)
    with DRAFTS.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    os.chmod(DRAFTS, 0o600)


def run_model(tag: str) -> dict:
    hold = _load_jsonl(HELD)
    limit = int(os.environ.get("VOICE_EVAL_LIMIT") or "0")
    if limit > 0:
        hold = hold[:limit]
    # Resume only drafts for this tag.
    have = {}
    if DRAFTS.is_file():
        for row in _load_jsonl(DRAFTS):
            if row.get("tag") == tag and row.get("id"):
                have[str(row["id"])] = str(row.get("draft") or "")
    graded = []
    skipped = 0
    hold_replies = 0
    for i, row in enumerate(hold, 1):
        ident = str(row.get("id") or "")
        if ident in have:
            draft = have[ident]
        else:
            draft = complete_one(row)
            _append_draft(row, draft, tag)
            have[ident] = draft
            time.sleep(4.0)
        if not draft:
            skipped += 1
            continue
        if draft.strip().upper() in {"HOLD", "NOREPLY"}:
            hold_replies += 1
        graded.append(score_row(row, draft))
        if i % 5 == 0:
            print(json.dumps({"tag": tag, "done": i, "scored": len(graded), "mean": vs.mean_score(graded)["score"]}), flush=True)
    summary = vs.mean_score(graded)
    summary["tag"] = tag
    summary["skipped"] = skipped
    summary["heldout"] = len(hold)
    summary["hold_replies"] = hold_replies
    return summary


def _record(summary: dict) -> None:
    blob = {}
    if SCORES.is_file():
        try:
            blob = json.loads(SCORES.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            blob = {}
    runs = blob.get("runs") or []
    summary["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs.append(summary)
    blob["runs"] = runs
    blob["latest"] = summary
    _write_private(SCORES, json.dumps(blob, indent=2) + "\n")
    print(json.dumps(summary))


def _scrub_pin(text: str) -> str:
    cli = str(HOME / "llm-appliance/cli")
    if cli not in sys.path:
        sys.path.insert(0, cli)
    import draft_fewshot as fs
    cleaned = fs.scrub_style(text or "")
    cleaned = fs.scrub(cleaned)
    if "@" in cleaned or any(ch.isdigit() for ch in cleaned):
        return ""
    return cleaned.strip()


def build_pins(train: list[dict]) -> list[dict] | None:
    picked = []
    for channel in ("outlook", "zoom"):
        pool = [r for r in train if r.get("channel") == channel]
        pool.sort(key=lambda r: abs(len(str(r.get("reply") or "")) - 140))
        for row in pool:
            reply = _scrub_pin(str(row.get("reply") or ""))
            incoming = _scrub_pin(str(row.get("incoming") or ""))
            if len(reply) < 40 or len(incoming) < 30:
                continue
            picked.append((channel, incoming[:420], reply[:420]))
            break
    if len(picked) < 2:
        # Second shot can be another outlook reply if zoom did not scrub clean.
        for row in train:
            if any(channel == row.get("channel") and reply == _scrub_pin(str(row.get("reply") or ""))[:420] for channel, _, reply in picked):
                continue
            reply = _scrub_pin(str(row.get("reply") or ""))
            incoming = _scrub_pin(str(row.get("incoming") or ""))
            if len(reply) < 40:
                continue
            picked.append((str(row.get("channel") or "outlook"), incoming[:420] or "short question. shape only.", reply[:420]))
            if len(picked) >= 2:
                break
    if len(picked) < 1:
        return None
    stamp_s = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pins = []
    for channel, incoming, reply in picked[:2]:
        pins.append({
            "added_at": stamp_s,
            "surface": channel,
            "source": "style-" + channel,
            "style_only": True,
            "ask": "short reply on this surface. answer first. plain words. this example is shape only.",
            "before": incoming,
            "after": reply,
            "pattern": "shape and wording only. placeholders are not facts of the current thread.",
            "rule": "do not copy a name, a number, or a fact from this example.",
            "match": "",
            "note": "voice pair style",
            "alex_redo": False,
        })
    return pins


def cmd_baseline() -> None:
    if not HELD.is_file():
        cmd_split()
    _record(run_model("baseline"))


def cmd_gate() -> None:
    """Rescore the newest draft tag through the shaper. No model call."""
    if not DRAFTS.is_file():
        raise SystemExit("no drafts")
    rows = _load_jsonl(DRAFTS)
    tag = rows[-1].get("tag") or "baseline"
    latest = {}
    for row in rows:
        if row.get("tag") == tag and row.get("id"):
            latest[str(row["id"])] = str(row.get("draft") or "")
    hold = {str(r.get("id")): r for r in _load_jsonl(HELD)}
    graded = []
    for ident, draft in latest.items():
        src = hold.get(ident)
        if not src:
            continue
        shaped = vs.shape_reply(draft, str(src.get("incoming") or ""), str(src.get("channel") or "outlook"))
        if not shaped:
            continue
        graded.append(score_row(src, shaped))
    summary = vs.mean_score(graded)
    summary["tag"] = "gate:" + tag
    summary["skipped"] = len(latest) - len(graded)
    summary["heldout"] = len(hold)
    _record(summary)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    if cmd == "split":
        cmd_split()
    elif cmd == "baseline":
        cmd_baseline()
    elif cmd == "run":
        _record(run_model(sys.argv[2]))
    elif cmd == "gate":
        cmd_gate()
    elif cmd == "pins":
        rows = _load_jsonl(PAIRS)
        hold_ids = {str(r.get("id")) for r in _load_jsonl(HELD)} if HELD.is_file() else set()
        train = [r for r in rows if str(r.get("id")) not in hold_ids]
        pins = build_pins(train)
        if not pins:
            raise SystemExit("no clean pins")
        seen = stamp(PINS)
        if stamp(PINS) != seen:
            raise SystemExit("pins changed while reading")
        backup = PINS.with_name("pinned_fewshots.json.bak-voice")
        if not backup.exists():
            backup.write_bytes(PINS.read_bytes())
        # Recheck just before the write.
        if stamp(PINS) != seen:
            raise SystemExit("pins changed by another job")
        _write_private(PINS, json.dumps(pins, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"pins": len(pins), "surfaces": [p["surface"] for p in pins]}))
    else:
        raise SystemExit("usage: voice_eval.py split|baseline|gate|pins")


if __name__ == "__main__":
    main()
