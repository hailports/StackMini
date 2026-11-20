#!/usr/bin/env python3
"""Brain health, Qwen 4 Max judge. launchd label from WBH_LABEL (8:00, 12:00, 20:00 CT).
collect.py + local_model.py (deterministic, read-only) -> Qwen 4 Max via hermes-qwen4 (Nous Portal, hosted)
-> report. Remediation is suggested only; set WBH_REMEDIATE=1 to run whitelisted kickstarts.
If hermes-qwen4 is unavailable (exit 3 sign-in, 4 guard, 5 upstream/free window over) the deterministic
report is still written with a line saying so. No corp Codex fallback.
Off switch: touch the WBH_OFF file (default ~/claude-stack/data/runtime/BRAIN_HEALTH_OFF)."""
import datetime as dt, json, os, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import local_model

H = Path.home(); S = H / "claude-stack"
OUT = Path(os.environ.get("WBH_OUT", str(S / "data/reports/brain_health"))); OUT.mkdir(parents=True, exist_ok=True)
OFF = Path(os.environ.get("WBH_OFF", str(S / "data/runtime/BRAIN_HEALTH_OFF")))
PAUSE = S / "data/runtime/chat_test_paused_jobs.txt"
SACRED_HINT = re.compile(r"splash|mlx|18080|18085|18820|18822|18812|9227|9237|shim|llama|chat-priority", re.I)
MODEL_HEAVY = re.compile(r"rag|embed|brain2|idle-fill|drafter", re.I)

def protected():
    """Labels never touched (query/MCP servers). One per line in protected_labels.txt next to this file."""
    f = HERE / "protected_labels.txt"
    return set(l.strip() for l in f.read_text().splitlines() if l.strip()) if f.exists() else set()

def now(): return dt.datetime.now().strftime("%Y-%m-%d %H:%M CT")

def deterministic(snap):
    bad, warn = [], []
    sv = snap.get("services", {})
    for k in ("query_8079", "ask_8079", "mcp_8089"):
        if not (sv.get(k) or {}).get("ok"): bad.append(f"{k} down")
    if not (sv.get("mcp_stdio") or {}).get("ok"): warn.append("stdio MCP lookup failed")
    if (sv.get("ask_8079") or {}).get("ms", 0) > 5000: warn.append(f"ask slow {sv['ask_8079']['ms']} ms")
    for db, v in (snap.get("freshness") or {}).items():
        for src, f in ((v or {}).get("sources") or {}).items():
            if f.get("stale"): warn.append(f"{db}:{src} stale {f['newest_age_h']}h")
    for lab, j in (snap.get("jobs") or {}).items():
        if j.get("paused_for_chat_test"): continue
        if not j.get("loaded"): warn.append(f"{lab} not loaded")
        elif j.get("last_exit") not in ("0", "never", None): warn.append(f"{lab} last exit {j['last_exit']}")
    pf = [p["id"] for p in (snap.get("probes") or []) if not p.get("ok")]
    if pf: (bad if len(pf) >= 3 else warn).append("probes failed: " + ", ".join(pf))
    return ("RED" if bad else "YELLOW" if warn else "GREEN"), bad + warn

def remediate(snap, actions):
    """Only with WBH_REMEDIATE=1: launchctl kickstart of loaded, unpaused ingest jobs. Never servers or model ports."""
    done, never, chat_test = [], protected(), PAUSE.exists()
    jobs = snap.get("jobs") or {}
    for a in (actions or [])[:4]:
        lab = str(a.get("label") or "")
        ok = (a.get("action") == "kickstart" and lab in jobs and lab not in never and not SACRED_HINT.search(lab)
              and jobs[lab].get("loaded") and not jobs[lab].get("paused_for_chat_test"))
        if ok and chat_test and MODEL_HEAVY.search(lab):
            done.append(f"DEFERRED {lab} (model job; background model jobs paused)"); continue
        if not ok:
            done.append(f"REFUSED {a.get('action')} {lab}"); continue
        r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{lab}"], capture_output=True, text=True)
        done.append(f"kickstart {lab} rc={r.returncode}")
    return done
WRAP = os.environ.get("WBH_QWEN_BIN", str(H / "claude-stack/bin/hermes-qwen4"))
LABEL = os.environ.get("WBH_LABEL", "com.claude-stack.brain-health-qwen4")
REASON = {3: "Nous sign-in needed", 4: "refused by hermes-qwen4 guard", 5: "upstream error or free window ended"}

def qwen(snap, det_lines):
    prompt = ((HERE / "prompt_qwen4.md").read_text() + json.dumps(snap, separators=(",", ":"))[:60000]
              + "\n\nDETERMINISTIC FINDINGS:\n" + "\n".join(det_lines or ["none"]))
    env = dict(os.environ); env["PATH"] = str(H / ".local/bin") + ":/opt/homebrew/bin:/usr/bin:/bin"
    try:
        p = subprocess.run([WRAP, "-"], input=prompt, capture_output=True,
                           text=True, timeout=900, env=env)
    except subprocess.TimeoutExpired:
        return None, "Qwen layer unavailable: hermes-qwen4 timed out after 900 s"
    except Exception as e:
        return None, f"Qwen layer unavailable: {type(e).__name__}"
    if p.returncode != 0:
        why = REASON.get(p.returncode, f"exit {p.returncode}")
        return None, f"Qwen layer unavailable: hermes-qwen4 exit {p.returncode} ({why})"
    m = re.search(r"\{.*\}", p.stdout, re.S)
    if not m:
        return None, "Qwen layer unavailable: no JSON in the answer"
    try:
        return json.loads(m.group(0)), ""
    except json.JSONDecodeError:
        return None, "Qwen layer unavailable: bad JSON in the answer"

def det_local(lm):
    bad = []
    for k, v in lm.items():
        if not v.get("ok"): bad.append(f"local model {k} not ok (status {v.get('status')})")
    return bad

def main():
    if OFF.exists():
        print(f"{OFF.name} present; skipping"); return 0
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    t0 = time.time()
    c = subprocess.run(["/usr/bin/python3", str(HERE / "collect.py")], capture_output=True, text=True, timeout=600)
    try: snap = json.loads(c.stdout)
    except Exception: snap = {"error": "collect failed", "stderr": c.stderr[-500:]}
    try: snap["local_model"] = local_model.probe()
    except Exception as e: snap["local_model"] = {"error": type(e).__name__}
    (OUT / f"snapshot-{stamp}.json").write_text(json.dumps(snap, indent=1))
    det_status, det_lines = deterministic(snap)
    lm_bad = det_local(snap.get("local_model") or {})
    if lm_bad:
        det_lines = lm_bad + det_lines; det_status = "RED"
    verdict, err = qwen(snap, det_lines)
    sugg = (verdict or {}).get("remediate") or []
    if verdict and os.environ.get("WBH_REMEDIATE") == "1":
        actions = remediate(snap, sugg)
    else:
        actions = [f"suggested (not run): {a.get('action')} {a.get('label')} — {a.get('why', '')}" for a in sugg[:4]]
    status = (verdict or {}).get("status") or det_status
    order = {"GREEN": 0, "YELLOW": 1, "RED": 2}
    summary = (verdict or {}).get("summary") or "; ".join(det_lines[:3]) or "all checks passed"
    if order.get(det_status, 0) > order.get(status, 0):   # never let the judge hide a hard failure
        status = det_status
        summary = "; ".join(det_lines[:3]) + f" (judge said {verdict.get('status')}, overruled)"
    lines = [f"# Brain health — {now()}", "", f"**Status:** {status}  ", f"**Summary:** {summary}  ",
             f"**Judge:** {'Qwen 4 Max (Nous Portal via hermes-qwen4)' if verdict else 'deterministic only'}  "]
    if not verdict:
        lines.append(f"**{err}.** Deterministic checks below are complete; no corp Codex fallback.  ")
    lines += [f"**Took:** {int(time.time() - t0)} s", "", "## Deterministic findings"]
    lines += [f"- {l}" for l in det_lines] or ["- none"]
    if verdict:
        lines += ["", "## Qwen 4 Max findings"] + [f"- {l}" for l in verdict.get("findings") or ["none"]]
        lines += ["", f"**Confidence blocker:** {verdict.get('confidence', '')}"]
    lines += ["", "## Remediation"] + ([f"- {a}" for a in actions] or ["- none"])
    lines += ["", "## Local model", "```", json.dumps(snap.get("local_model"), indent=1), "```",
              "", "## Services", "```", json.dumps(snap.get("services", {}), indent=1), "```", "", "## Probes", "```",
              json.dumps(snap.get("probes"), indent=1), "```", "", f"Snapshot: snapshot-{stamp}.json",
              "", f"Turn off: `touch {OFF}` (or `launchctl bootout gui/$(id -u)/{LABEL}`)."]
    rep = OUT / f"report-{stamp}.md"; rep.write_text("\n".join(lines) + "\n")
    latest = OUT / "latest.md"
    if latest.is_symlink() or latest.exists(): latest.unlink()
    latest.symlink_to(rep.name)
    (OUT / "STATUS").write_text(f"{now()} {status} {summary}\n")
    with open(OUT / "history.jsonl", "a") as f:
        f.write(json.dumps({"ts": stamp, "status": status, "summary": summary, "judge": "qwen4" if verdict else "none",
                            "qwen_error": err, "actions": actions}) + "\n")
    print(f"{status} {summary}" + ("" if verdict else f" [{err}]"))
    return 0

if __name__ == "__main__":
    sys.exit(main())
