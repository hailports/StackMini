#!/usr/bin/env python3
"""Real-scenario eval for `l` and the drafter.

Tune split picks the config. Held-out is scored once and is the headline.
Deterministic rubric plus two judge passes (stricter rule judge, and a
local-model pair when :8080 is idle). Does not send, does not touch bridges,
does not restart llama-server, does not train.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

CLI = Path.home() / "llm-appliance/cli"
sys.path.insert(0, str(CLI))
HERE = Path.home() / "llm-appliance/runtime/realeval"
sys.path.insert(0, str(HERE))

import activity_lead as al  # noqa: E402

CASES = HERE / "cases.jsonl"
REPORT = HERE / "REPORT.md"
DIRECT = "http://127.0.0.1:8080/v1"

GRID = {
    "days": [7, 14, 21],
    "k": [4, 8, 12],
    "half_life_days": [2.0, 4.0, 7.0],
    "bm25_weight": [0.0, 0.35, 0.7],
    "dossier_bg_chars": [0, 280],
    "ambiguous_margin": [1, 3],
    "template": ["sections", "prose"],
}


def load_cases() -> list[dict]:
    rows = [json.loads(l) for l in CASES.read_text().splitlines() if l.strip()]
    if len(rows) < 250:
        raise SystemExit(f"need >=250 cases, have {len(rows)}")
    return rows


def lcs(a: str, b: str) -> int:
    a = (a or "").lower()[:360]
    b = (b or "").lower()[:360]
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def toks(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]{4,}", (text or "").lower())


def f1(a: str, b: str) -> float:
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    from collections import Counter
    ca, cb = Counter(ta), Counter(tb)
    overlap = sum((ca & cb).values())
    if not overlap:
        return 0.0
    p, r = overlap / len(ta), overlap / sum(cb.values())
    return 2 * p * r / (p + r) if (p + r) else 0.0


def lead_ok(case: dict, text: str, cfg: dict) -> bool:
    kind, expect = case.get("kind"), case.get("expect")
    low = (text or "").lower()
    if kind == "waiting" and expect == "answer":
        return low.startswith("waiting on you")
    if kind == "update" and expect == "answer" and not case.get("trap"):
        if (cfg or {}).get("template") == "prose":
            return "what changed" in low[:500]
        wc = low.find("what changed")
        bg = low.find("background")
        return wc >= 0 and (bg < 0 or wc < bg)
    if kind == "ado" and expect == "answer" and case.get("trap") != "hallucination":
        return bool(re.search(r"\b\d{5,7}\b", text or "") and re.search(r"(?i)\bstate:", text or ""))
    return True


def score_one(case: dict, text: str, cfg: dict) -> dict:
    low = (text or "").lower()
    expect = case.get("expect") or "answer"
    miss = [m for m in (case.get("must") or []) if m.lower() not in low]
    bad = [m for m in (case.get("must_not") or []) if m.lower() in low]
    echo = False
    style = True
    incoming = case.get("incoming") or ""
    if incoming and expect == "answer" and lcs(text, incoming) >= 40:
        echo = True
    if case.get("kind") == "draft" and expect == "answer" and case.get("trap") != "echo":
        first = (text or "").splitlines()[0].lower() if text else ""
        if not (first.startswith("hey ") and "--" in first):
            style = False
    lead = lead_ok(case, text, cfg)
    if expect == "refuse":
        ok = ("data line" in low) and not bad and not miss
    elif expect == "no_reply":
        ok = ("no reply" in low) and not bad
    elif expect == "not_found":
        ok = (not miss) and (not bad)
    elif expect == "ambiguous":
        ok = ("more than one project" in low) and (not miss) and (not bad) and ("what changed" not in low)
    elif expect == "fact":
        ok = (not miss) and (not bad)
    elif case.get("trap") == "echo":
        ok = (not echo) and (not bad) and (not miss) and len(text or "") < max(80, len(incoming))
    else:
        ok = (not miss) and (not bad) and lead and style and not echo
    return {
        "ok": bool(ok),
        "miss": miss,
        "bad": bad,
        "lead": bool(lead),
        "style": bool(style),
        "echo": bool(echo),
        "f1": round(f1(text, case.get("gold") or ""), 3) if case.get("gold") else None,
    }


def judge_strict(case: dict, text: str, base: dict) -> bool:
    """Second rubric: dates and ids on live updates, no invented state words on traps."""
    if not base["ok"]:
        return False
    if case.get("trap") in {"hallucination", "dataline", "departed", "org", "ambiguous"}:
        return True
    if case.get("kind") == "update" and case.get("expect") == "answer" and not case.get("trap"):
        head = (text or "")[:500]
        return bool(re.search(r"20\d\d-\d\d-\d\d", head) and re.search(r"\b(?:SF-\d{3,6}|\d{5,7})\b", head))
    if case.get("kind") == "ado" and case.get("expect") == "answer":
        return "state:" in (text or "").lower()
    return True


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    i = min(len(ys) - 1, max(0, int(round((len(ys) - 1) * p))))
    return ys[i]


def rate(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if r["ok"]) / len(rows)


def cfg_key(cfg: dict) -> tuple:
    return tuple(cfg.get(k) for k in (
        "days", "k", "half_life_days", "bm25_weight", "dossier_bg_chars",
        "ambiguous_margin", "template",
    ))


def answer_cached(q: str, cfg: dict, cache: dict) -> tuple[str, float]:
    key = (q, cfg_key(cfg))
    if key in cache:
        return cache[key], 0.0
    t0 = time.perf_counter()
    text = al.answer(q, cfg)
    dt = time.perf_counter() - t0
    cache[key] = text
    return text, dt


def baseline_text(case: dict) -> str:
    if case.get("kind") == "draft":
        return (case.get("incoming") or "")[:180]
    try:
        import dossier_route as dr
        return dr.pack(case["q"], use_model=False, wait_s=0.0) or ""
    except Exception as e:
        return f"(baseline pack failed {type(e).__name__})"


def expand_grid() -> list[dict]:
    base = dict(al.DEFAULTS)
    out = [{}]
    for name, values in GRID.items():
        nxt = []
        for cfg in out:
            for v in values:
                row = dict(cfg)
                row[name] = v
                nxt.append(row)
        out = nxt
    full = []
    for row in out:
        cfg = dict(base)
        cfg.update(row)
        cfg["embed_weight"] = 0.0
        cfg["draft_model"] = False
        cfg["thinking"] = False
        full.append(cfg)
    return full


def sweep(tune: list[dict]) -> tuple[dict, dict]:
    for row in tune:
        if row.get("split") != "tune":
            raise SystemExit("sweep saw a non-tune row")
    cache: dict = {}
    best = None
    best_key = None
    tried = 0
    t0 = time.perf_counter()
    for cfg in expand_grid():
        tried += 1
        scored = []
        lats = []
        for case in tune:
            text, dt = answer_cached(case["q"], cfg, cache)
            rec = score_one(case, text, cfg)
            rec["lat"] = dt
            scored.append(rec)
        trap = [s for s, c in zip(scored, tune) if c.get("trap")]
        lats = [s["lat"] for s in scored if s["lat"] > 0]
        key = (
            round(rate(scored), 3),
            round(rate(trap) if trap else 1.0, 3),
            -abs(int(cfg["days"]) - 14),
            1 if cfg["template"] == "sections" else 0,
            1 if int(cfg["dossier_bg_chars"]) == 280 else 0,
            -abs(int(cfg["k"]) - 8),
            -abs(float(cfg["bm25_weight"]) - 0.35),
            -pct(lats, 0.50),
        )
        if best_key is None or key > best_key:
            best_key = key
            best = cfg
    return best, {
        "tried": tried,
        "secs": round(time.perf_counter() - t0, 2),
        "tune_pass": best_key[0],
        "tune_trap": best_key[1],
        "embed_weight": "fixed at 0 — query vectors would start a second model; 144/19824 chunks have vectors",
        "thinking": False,
        "draft_model": False,
        "temperature": 0.0,
        "top_p": 0.9,
        "repeat_penalty": 1.05,
        "fewshot_k": 3,
    }


def run_set(cases: list[dict], cfg: dict, mode: str) -> list[dict]:
    out = []
    for case in cases:
        t0 = time.perf_counter()
        if mode == "after":
            text = al.answer(case["q"], cfg)
        else:
            text = baseline_text(case)
        dt = time.perf_counter() - t0
        rec = score_one(case, text, cfg if mode == "after" else {"template": "sections"})
        rec.update({
            "id": case["id"],
            "kind": case["kind"],
            "split": case["split"],
            "trap": case.get("trap") or "",
            "project": case.get("project") or "",
            "channel": case.get("channel") or "",
            "difficulty": case.get("difficulty") or "",
            "q": case["q"][:180],
            "lat": round(dt, 4),
            "text": text[:900],
            "judge2": judge_strict(case, text, rec),
        })
        if case.get("gold"):
            echo = (case.get("incoming") or "")[:180]
            fa, fe = f1(text, case["gold"]), f1(echo, case["gold"])
            rec["pair"] = "system" if fa >= fe else "echo"
            rec["f1_echo"] = round(fe, 3)
        out.append(rec)
    return out


def agg(rows: list[dict]) -> dict:
    def pack(group: list[dict]) -> dict:
        lats = [r["lat"] for r in group]
        return {
            "n": len(group),
            "pass": round(rate(group), 4),
            "facts": round(sum(1 for r in group if not r["miss"] and not r["bad"]) / len(group), 4) if group else 0,
            "judge2": round(rate([{"ok": r["judge2"]} for r in group]), 4) if group else 0,
            "p50": round(pct(lats, 0.50), 4),
            "p95": round(pct(lats, 0.95), 4),
        }
    by_kind = {}
    for kind in sorted({r["kind"] for r in rows}):
        by_kind[kind] = pack([r for r in rows if r["kind"] == kind])
    traps = [r for r in rows if r["trap"]]
    return {"all": pack(rows), "by_kind": by_kind, "traps": pack(traps)}


def slot_idle() -> bool:
    try:
        import chat_priority as cp
        if cp.blocked() or cp.server_busy():
            return False
    except Exception:
        pass
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/slots", timeout=2) as r:
            data = json.loads(r.read().decode())
        slots = data if isinstance(data, list) else []
        if any(bool(s.get("is_processing")) for s in slots):
            return False
    except Exception:
        return False
    return True


def model_judge(cases: list[dict], answers: dict[str, str], limit: int = 16) -> dict:
    """Two prompts, two temperatures. Skips immediately if the slot is busy."""
    if not slot_idle():
        return {"ran": 0, "skipped": "slot busy or unreachable", "agree": None}
    sample = [c for c in cases if c.get("split") == "heldout"][:limit]
    a_pass, b_pass, agree, ran = 0, 0, 0, 0
    prompts = [
        (0.0, "You score answers. Output one word only, PASS or FAIL. PASS only when every required fact is present and no forbidden fact is stated."),
        (0.4, "You are a second reviewer. Output one word only, PASS or FAIL. PASS only if a teammate could act on this without a stale owner, a fake ticket, or a secret."),
    ]
    for case in sample:
        text = answers.get(case["id"]) or ""
        user = (
            f"ANSWER:\n{text[:700]}\n\nREQUIRED: {case.get('must')}\n"
            f"FORBIDDEN: {case.get('must_not')}\nEXPECT: {case.get('expect')}\n"
            "Reply with exactly one word: PASS or FAIL."
        )
        votes = []
        for temp, system in prompts:
            if not slot_idle():
                break
            body = {
                "model": "local",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 4,
                "temperature": temp,
                "top_p": 0.9,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            req = urllib.request.Request(
                DIRECT + "/chat/completions",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            try:
                import chat_priority as cp
                opener = cp.urlopen_bg
            except Exception:
                opener = urllib.request.urlopen
            try:
                t0 = time.perf_counter()
                with opener(req, timeout=25) as r:
                    data = json.loads(r.read().decode())
                if time.perf_counter() - t0 > 20:
                    return {"ran": ran, "skipped": "slow", "agree": None, "a": a_pass, "b": b_pass}
                msg = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                votes.append("PASS" in msg.upper() and "FAIL" not in msg.upper())
            except Exception as e:
                return {"ran": ran, "skipped": type(e).__name__, "agree": None, "a": a_pass, "b": b_pass}
        if len(votes) == 2:
            ran += 1
            a_pass += int(votes[0])
            b_pass += int(votes[1])
            agree += int(votes[0] == votes[1])
    return {
        "ran": ran,
        "a_pass": a_pass,
        "b_pass": b_pass,
        "agree": round(agree / ran, 3) if ran else None,
        "skipped": "",
    }


def gallery(rows: list[dict], n: int = 20) -> list[dict]:
    bad = [r for r in rows if not r["ok"]]
    bad.sort(key=lambda r: (r["split"] != "heldout", -len(r["miss"]) - len(r["bad"]), r["id"]))
    return bad[:n]


def _lat(seconds: float) -> str:
    seconds = float(seconds or 0)
    if seconds < 1:
        return f"{seconds * 1000:.1f} ms"
    return f"{seconds:.2f} s"


def write_report(meta: dict) -> None:
    b, a = meta["before"], meta["after"]
    held_b, held_a = meta["held_before"], meta["held_after"]
    lines = []
    lines.append(f"# Real-scenario eval — {meta['when']}")
    lines.append("")
    lines.append(
        f"Headline held-out rubric **{held_a['all']['pass']:.1%}** "
        f"(facts {held_a['all'].get('facts', 0):.1%}, n={held_a['all']['n']}) vs baseline "
        f"**{held_b['all']['pass']:.1%}** (facts {held_b['all'].get('facts', 0):.1%}). "
        f"Tune was {meta['sweep']['tune_pass']:.1%} over {meta['sweep']['tried']} configs "
        f"in {meta['sweep']['secs']}s. Held-out was not used to pick."
    )
    lines.append("")
    lines.append(
        f"Ship: **{meta['ship']}**. {meta['ship_why']} "
        f"Rollback: `ACTIVITY_LEAD=0` or `runtime/realeval/live_config.json` enabled false. "
        f"Backups: `*.bak-20261015-nextlvl`."
    )
    lines.append("")
    lines.append("## Held-out by kind")
    lines.append("")
    lines.append("| kind | n | before rubric | before facts | after rubric | after facts | after p50 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    kinds = sorted(set(held_b["by_kind"]) | set(held_a["by_kind"]))
    for k in kinds:
        bb, aa = held_b["by_kind"].get(k, {}), held_a["by_kind"].get(k, {})
        lines.append(
            f"| {k} | {aa.get('n', 0)} | {bb.get('pass', 0):.1%} | {bb.get('facts', 0):.1%} | {aa.get('pass', 0):.1%} | {aa.get('facts', 0):.1%} | {_lat(aa.get('p50', 0))} |"
        )
    lines.append("")
    lines.append(
        f"Held-out traps: before {meta['held_trap_before']:.1%}, after {meta['held_trap_after']:.1%}. "
        f"All traps: before {b['traps']['pass']:.1%} ({b['traps']['n']}), after {a['traps']['pass']:.1%}."
    )
    lines.append("")
    lines.append("## All cases")
    lines.append("")
    lines.append("| | pass | judge2 agree with pass | p50 | p95 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for label, block in (("before", b), ("after", a)):
        j = block["all"]["judge2"]
        # judge2 rate is the strict-judge pass rate; agreement is computed below
        lines.append(
            f"| {label} | {block['all']['pass']:.1%} | {meta['agree_'+label]:.1%} | {_lat(block['all']['p50'])} | {_lat(block['all']['p95'])} |"
        )
    lines.append("")
    lines.append("## By kind (all splits)")
    lines.append("")
    lines.append("| kind | n | before | after |")
    lines.append("| --- | --- | --- | --- |")
    for k in sorted(set(b["by_kind"]) | set(a["by_kind"])):
        lines.append(
            f"| {k} | {a['by_kind'].get(k, {}).get('n', 0)} | {b['by_kind'].get(k, {}).get('pass', 0):.1%} | {a['by_kind'].get(k, {}).get('pass', 0):.1%} |"
        )
    lines.append("")
    lines.append("## Judges")
    lines.append("")
    lines.append(
        "Judge 1 is the deterministic rubric (facts, lead, no echo, no departed-as-owner, refuse/not-found). "
        "Judge 2 is stricter (a date and an id in the first 500 chars on plain updates). "
        f"Agreement before {meta['agree_before']:.1%}, after {meta['agree_after']:.1%}."
    )
    mj = meta["model_judge"]
    if mj.get("ran"):
        lines.append(
            f"Local model, two prompts (temp 0 and 0.4), {mj['ran']} held-out cases: "
            f"pass {mj.get('a_pass')}/{mj['ran']} and {mj.get('b_pass')}/{mj['ran']}, "
            f"agreement {mj.get('agree')}."
        )
    else:
        lines.append(f"Local model judges did not run ({mj.get('skipped') or 'not attempted'}).")
    lines.append("")
    drafts = [r for r in meta["after_rows"] if r.get("f1") is not None]
    if drafts:
        sys_w = sum(1 for r in drafts if r.get("pair") == "system")
        mean_f = statistics.mean(r["f1"] for r in drafts)
        lines.append(
            f"Draft pairwise vs operator's sent excerpt (n={len(drafts)}): "
            f"system {sys_w}, echo-copy {len(drafts) - sys_w}. "
            f"Mean token F1 vs his text {mean_f:.2f}. "
            "Excerpts in the sidecar are capped at 180 characters, so this is overlap, not a full-mail match."
        )
        lines.append("")
    lines.append("## Config")
    lines.append("")
    lines.append("Winning config (grid on tune only):")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(meta["cfg"], indent=2))
    lines.append("```")
    lines.append("")
    lines.append(meta["sweep"]["embed_weight"] + ".")
    lines.append(
        "Temperature, top_p, repeat penalty, and thinking stay at 0 / 0.9 / 1.05 / off "
        "because the shipped status path does not decode. Few-shot exemplars are saved under lora/ "
        "and are not pasted into answers (that would copy other threads)."
    )
    lines.append("")
    lines.append("## Latency")
    lines.append("")
    lines.append(
        f"After p50 {_lat(a['all']['p50'])}, p95 {_lat(a['all']['p95'])} on {a['all']['n']} cases "
        f"(short status turns are in-process sqlite, no llama decode). Target was p50 under 10s."
    )
    lines.append("")
    lines.append("## Failure gallery (20 worst)")
    lines.append("")
    if not meta["gallery"]:
        lines.append("No failures.")
    for r in meta["gallery"]:
        lines.append(
            f"### {r['id']} ({r['split']}, {r['kind']}, trap={r['trap'] or '-'})"
        )
        lines.append("")
        lines.append(f"Q: {r['q']}")
        lines.append("")
        lines.append(f"Miss: {r['miss']} Bad: {r['bad']} lead={r['lead']} echo={r['echo']} style={r['style']}")
        lines.append("")
        lines.append("```")
        lines.append((r.get("text") or "")[:500])
        lines.append("```")
        lines.append("")
    lines.append("## LoRA")
    lines.append("")
    lines.append(meta["lora_note"])
    lines.append("")
    lines.append("## Nightly")
    lines.append("")
    lines.append(
        "launchd `com.claude-stack.realeval` at 04:40, beside the 04:30 golden job. "
        "It re-scores the frozen cases with the live config. "
        "An alarm is written only to `runtime/realeval/ALARM.txt`, which the morning brief appends. "
        "No separate email. Stable gates: trap pass, data-line refuse, update lead rate, p50 under 10s. "
        "Id lists go stale as the 14-day window moves, so a missed id alone does not page."
    )
    lines.append("")
    lines.append("## Limits")
    lines.append("")
    lines.append(
        "ADO state is the latest ingested sidecar row, not a fresh read of :18820 (that bridge is off limits). "
        "operator was not queried. Draft F1 cannot recover facts that exist only in operator's sent sentence "
        "and not in the incoming text. The answer path is deterministic on purpose: the failure mode was "
        "the dossier leading the pack, and a decode would put p50 back over 10s."
    )
    lines.append("")
    REPORT.write_text("\n".join(lines))


MAIL_BASELINE = "0/20"
SWAP_LINE_MB = 5600
MAIL_CAP = 10


def swap_used_mb() -> float:
    try:
        raw = __import__("subprocess").run(
            ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        part = raw.split("used =")[1].split()[0]
        return float(part.rstrip("M"))
    except Exception:
        return 99999.0


def mail_gate(swap_mb: float, busy: bool, preempt: bool) -> str:
    """Empty string means this one item may run. Chat preempts. Swap over the line skips."""
    if swap_mb > SWAP_LINE_MB:
        return "swap"
    if preempt:
        return "preempt"
    if busy:
        return "busy"
    return ""


def mail_replay(cap: int = MAIL_CAP) -> dict:
    """Held-out mail drafts only. One at a time, idle slot, swap under the line.

    Chat preempts through chat_priority. No text is logged. Baseline is the
    2026-10-18 alone-eval, 0/20.
    """
    swap = swap_used_mb()
    base = {
        "ran": 0,
        "passed": 0,
        "pass": None,
        "baseline": MAIL_BASELINE,
        "cap": cap,
        "swap_mb": round(swap) if swap < 99999 else None,
        "skipped": "",
    }
    if swap > SWAP_LINE_MB:
        base["skipped"] = "swap"
        return base
    path = Path.home() / "claude-stack/data/learning/eval_frozen/heldout_v1.jsonl"
    items = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("task_type") or "").startswith("draft_reply_"):
                items.append(row)
    except Exception:
        base["skipped"] = "no_set"
        return base
    items.sort(key=lambda row: str(row.get("id") or ""))
    items = items[: max(0, int(cap))]
    if not items:
        base["skipped"] = "no_mail"
        return base
    import chat_priority as cp
    import qwen_alone_eval as qa
    passed = 0
    ran = 0
    for item in items:
        why = mail_gate(swap_used_mb(), (cp.server_busy() or not slot_idle()), cp.blocked())
        if why:
            base["skipped"] = why
            break
        system, user, max_tokens = qa.prompt_for(item)
        body = {
            "model": os.environ.get("DIRECT_INBOX_QWEN_MODEL") or "local",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": int(max_tokens or 200),
            "temperature": 0.1,
            "stream": False,
            "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(
            DIRECT + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with cp.urlopen_bg(req, timeout=240) as resp:
                data = json.loads(resp.read().decode())
        except cp.Defer:
            base["skipped"] = "preempt"
            break
        except Exception as exc:
            base["skipped"] = type(exc).__name__
            break
        text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        scored = qa.score(item, text)
        ran += 1
        passed += int(bool(scored.get("pass")))
    base["ran"] = ran
    base["passed"] = passed
    base["pass"] = "{0}/{1}".format(passed, ran) if ran else None
    base["swap_mb"] = round(swap_used_mb())
    return base


def _weekly_join() -> dict:
    try:
        import draft_context as dc
        now = time.localtime()
        path = dc.learning_dir() / "draft_selfcheck_join.json"
        stale = True
        if path.is_file():
            stale = time.time() - path.stat().st_mtime > 6 * 86400
        if now.tm_wday == 6 or stale or not path.is_file():
            summary = dc.join_selfcheck()
            return {"matched": summary.get("matched"), "rows": summary.get("selfcheck_rows")}
    except Exception as exc:
        return {"error": type(exc).__name__}
    return {"skipped": "not_due"}


def nightly() -> None:
    rows = load_cases()
    cfg = al.load_cfg()
    if not cfg.get("enabled", True):
        HERE.mkdir(parents=True, exist_ok=True)
        (HERE / "nightly.log").write_text(time.strftime("%Y-%m-%d %H:%M") + " disabled\n")
        return
    after = run_set(rows, cfg, "after")
    traps = [r for r in after if r["trap"]]
    refuse = [r for r in after if r["trap"] == "dataline"]
    updates = [r for r in after if r["kind"] == "update" and r["split"] == "heldout" and not r["trap"]]
    lead = rate([{"ok": r["lead"] and r["ok"]} for r in updates]) if updates else 1
    # lead gate: fraction of plain held-out updates that still lead
    lead_rate = sum(1 for r in updates if r["lead"]) / len(updates) if updates else 1
    bar_path = HERE / "baseline.json"
    bar = json.loads(bar_path.read_text()) if bar_path.is_file() else {}
    reasons = []
    if bar:
        if rate(traps) + 1e-9 < float(bar.get("trap_pass") or 0):
            reasons.append(f"traps {rate(traps):.1%} < {bar.get('trap_pass')}")
        if refuse and rate(refuse) + 1e-9 < float(bar.get("refuse_pass") or 1):
            reasons.append("data-line refuse dropped")
        if lead_rate + 0.05 < float(bar.get("update_lead") or 0):
            reasons.append(f"update lead {lead_rate:.1%}")
        p50 = pct([r["lat"] for r in after], 0.50)
        if p50 > 10:
            reasons.append(f"p50 {p50:.1f}s")
    mail = {"ran": 0, "skipped": "flag", "baseline": MAIL_BASELINE, "pass": None}
    if "--no-model" not in sys.argv:
        mail = mail_replay()
    join = _weekly_join()
    mail_bit = "mail {0} baseline {1}".format(mail.get("pass") or ("skipped:" + str(mail.get("skipped") or "")), MAIL_BASELINE)
    alarm = HERE / "ALARM.txt"
    bits = list(reasons)
    bits.append(mail_bit)
    if reasons:
        alarm.write_text(
            "REALEVAL REGRESSION " + time.strftime("%Y-%m-%d") + ": " + "; ".join(bits) + "\n"
        )
    else:
        alarm.write_text(
            "REALEVAL " + time.strftime("%Y-%m-%d") + ": " + mail_bit + "\n"
        )
    with (HERE / "nightly.log").open("a") as fh:
        fh.write(json.dumps({
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "pass": round(rate(after), 4),
            "traps": round(rate(traps), 4),
            "lead": round(lead_rate, 4),
            "alarm": reasons,
            "mail_pass": mail.get("pass"),
            "mail_baseline": MAIL_BASELINE,
            "mail_ran": mail.get("ran"),
            "mail_skipped": mail.get("skipped") or "",
            "mail_swap_mb": mail.get("swap_mb"),
            "selfcheck_join": join,
        }) + "\n")
    print(json.dumps({"alarm": reasons, "pass": round(rate(after), 3), "mail": mail.get("pass"), "mail_skipped": mail.get("skipped") or ""}))


def main() -> None:
    if "--nightly" in sys.argv:
        nightly()
        return
    if "--rebuild" in sys.argv or not CASES.is_file():
        import build_cases
        build_cases.main()
    cases = load_cases()
    tune = [c for c in cases if c["split"] == "tune"]
    held = [c for c in cases if c["split"] == "heldout"]
    if any(c["split"] != "tune" for c in tune):
        raise SystemExit("split leak")
    print(f"cases {len(cases)} tune {len(tune)} held {len(held)}", flush=True)
    winner, sweep_meta = sweep(tune)
    print("winner", json.dumps({k: winner[k] for k in GRID}), flush=True)
    before = run_set(cases, winner, "before")
    after = run_set(cases, winner, "after")
    b, a = agg(before), agg(after)
    held_b = agg([r for r in before if r["split"] == "heldout"])
    held_a = agg([r for r in after if r["split"] == "heldout"])
    def trap_rate(rows, split=None):
        group = [r for r in rows if r["trap"] and (split is None or r["split"] == split)]
        return rate(group)
    held_pass_up = held_a["all"]["pass"] > held_b["all"]["pass"]
    trap_ok = trap_rate(after) + 1e-12 >= trap_rate(before)
    p50_ok = a["all"]["p50"] < 10
    ship = held_pass_up and trap_ok and p50_ok
    prev_path = HERE / "live_config.json"
    prev = {}
    if prev_path.is_file():
        try:
            prev = json.loads(prev_path.read_text())
        except Exception:
            prev = {}
    prev_bar = {}
    bar_path = HERE / "baseline.json"
    if bar_path.is_file():
        try:
            prev_bar = json.loads(bar_path.read_text())
        except Exception:
            prev_bar = {}
    bak = HERE / "live_config.bak-20261015-nextlvl"
    if prev_path.is_file() and not bak.is_file():
        bak.write_text(prev_path.read_text())
    regress_live = (
        bool(prev.get("enabled"))
        and prev_bar.get("held_pass") is not None
        and held_a["all"]["pass"] + 1e-9 < float(prev_bar["held_pass"])
    )
    why = (
        f"held-out rubric {held_b['all']['pass']:.1%} -> {held_a['all']['pass']:.1%}, "
        f"facts {held_b['all']['facts']:.1%} -> {held_a['all']['facts']:.1%}, "
        f"traps {trap_rate(before):.1%} -> {trap_rate(after):.1%}, "
        f"p50 {_lat(a['all']['p50'])}."
    )
    if ship and regress_live:
        ship = False
        why += " Kept the previous live config; this held-out was lower."
    elif ship:
        live = dict(winner)
        live["enabled"] = True
        prev_path.write_text(json.dumps(live, indent=2) + "\n")
        al._CFG["data"] = None
    elif not prev.get("enabled"):
        prev_path.write_text(json.dumps({"enabled": False}, indent=2) + "\n")
        al._CFG["data"] = None
    else:
        why += " Kept the previous live config."
        al._CFG["data"] = None
    answers = {r["id"]: r["text"] for r in after if r["split"] == "heldout"}
    mj = {"ran": 0, "skipped": "flag"} if "--no-model" in sys.argv else model_judge(cases, answers)
    def agreement(rows):
        if not rows:
            return 0.0
        return sum(1 for r in rows if bool(r["ok"]) == bool(r["judge2"])) / len(rows)
    lora_stats = {}
    stats_path = HERE / "lora" / "stats.json"
    if stats_path.is_file():
        lora_stats = json.loads(stats_path.read_text())
    lora_note = (
        f"Saved `runtime/realeval/lora/train.jsonl` ({lora_stats.get('train', 0)}) and "
        f"`heldout.jsonl` ({lora_stats.get('heldout', 0)}), about {lora_stats.get('tokens_est', 0)} tokens. "
        "Training was not started. See lora/ESTIMATE.md. Needs operator's OK."
    )
    updates = [r for r in after if r["kind"] == "update" and r["split"] == "heldout" and not r["trap"]]
    refuse = [r for r in after if r["trap"] == "dataline"]
    bar = {
        "held_pass": held_a["all"]["pass"],
        "trap_pass": trap_rate(after),
        "refuse_pass": rate(refuse) if refuse else 1,
        "update_lead": (sum(1 for r in updates if r["lead"]) / len(updates)) if updates else 1,
        "p50": a["all"]["p50"],
        "when": time.strftime("%Y-%m-%d %H:%M"),
        "cfg": {k: winner[k] for k in list(GRID) + ["embed_weight", "thinking", "temperature", "top_p", "repeat_penalty", "fewshot_k", "draft_model"]},
    }
    if ship:
        (HERE / "baseline.json").write_text(json.dumps(bar, indent=2) + "\n")
    (HERE / "sweep.json").write_text(json.dumps({
        "winner_tune": sweep_meta,
        "cfg": bar["cfg"],
        "shipped": bool(ship),
    }, indent=2) + "\n")
    # drop bulky text from the jsonl after the gallery is built
    gal = gallery(after, 20)
    for path, rows in ((HERE / "scores_before.jsonl", before), (HERE / "scores_after.jsonl", after)):
        with path.open("w") as fh:
            for r in rows:
                slim = {k: v for k, v in r.items() if k != "text"}
                fh.write(json.dumps(slim) + "\n")
    meta = {
        "when": time.strftime("%Y-%m-%d %H:%M"),
        "before": b,
        "after": a,
        "held_before": held_b,
        "held_after": held_a,
        "held_trap_before": trap_rate(before, "heldout"),
        "held_trap_after": trap_rate(after, "heldout"),
        "sweep": sweep_meta,
        "cfg": bar["cfg"],
        "ship": "yes" if ship else "no",
        "ship_why": why,
        "agree_before": agreement(before),
        "agree_after": agreement(after),
        "model_judge": mj,
        "after_rows": after,
        "gallery": gal,
        "lora_note": lora_note,
    }
    write_report(meta)
    print(json.dumps({
        "ship": meta["ship"],
        "why": why,
        "held_before": held_b["all"],
        "held_after": held_a["all"],
        "model": mj,
    }, indent=2))


if __name__ == "__main__":
    main()
