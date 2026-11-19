#!/usr/bin/env python3
"""Side-by-side eval: paisty (full-context path) vs a Grok Bot-quality reference draft.

Every case is synthetic. Each draft is scored 0-10 with the same rubric:
  55%  must-include facts (the specific answer, numbers, names)
  20%  no forbidden content (stale asks, false "missing" claims, invented facts, push-backs)
  15%  addresses each person with an open ask
  10%  format (plain text, no sign-off, short)

    python3 scripts/paisty_eval/run_eval.py --ssh-host mini --remote-url http://127.0.0.1:8080/v1
    python3 scripts/paisty_eval/run_eval.py --base-url http://127.0.0.1:18080/v1
    python3 scripts/paisty_eval/run_eval.py --baseline-root /path/to/older/checkout   # also score the old path
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from hailports.clip import compose, screenshot  # noqa: E402
from hailports.clip.hops import complete, endpoints_from  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "paisty_eval"
RULES = ("Draft only the reply they will paste. Reply to the newest message from the other person. "
         "Do not use markdown. Use I. Do not sign the reply. Do not invent facts, names, times, or links.")
VOICE = ("all-lowercase except names, dates and I.\nstart with hey <name> -- when writing to someone else.\n"
         "short lines. write as I, not we.\nno sign-off and no name at the end.\n")


def email_doc(case: dict) -> dict:
    parts = ["", ""]
    for msg in case["thread"]:
        parts.append(f"From: {msg['from']}")
        parts.append(f"Sent: {msg['date']}")
        parts.append("To: " + "; ".join(msg.get("to", [])))
        if msg.get("cc"):
            parts.append("Cc: " + "; ".join(msg["cc"]))
        parts.append("Subject: " + case["subject"])
        parts.append("")
        parts.append(msg["body"])
        parts.append("")
    blocks = [{"text": "Subject:", "role": "AXStaticText"}, {"text": "Re: " + case["subject"], "role": "AXTextField"}]
    for name in case.get("attachments_shown", []):
        blocks.append({"text": name, "role": "AXButton"})
    return {"app": "Mail", "title": case["subject"], "field_text": "\n".join(parts), "field_role": "AXTextArea",
            "origin": "top", "blocks": blocks, "selection": ""}


def chat_doc(case: dict) -> dict:
    blocks = [{"text": name, "role": "AXButton"} for name in case.get("attachments_shown", [])]
    return {"app": "Chat", "title": case["subject"], "field_text": case["chat"], "origin": "top", "blocks": blocks, "selection": ""}


def score(text: str, rubric: dict) -> dict:
    low = (text or "").lower().replace("’", "'")
    inc = rubric.get("include", [])
    got = [any(alt.lower() in low for alt in group) for group in inc]
    bad = [pat for pat in rubric.get("exclude", []) if re.search(pat, text or "")]
    addr = rubric.get("address", [])
    addr_ok = [name.lower() in low for name in addr]
    fmt_bad = []
    if re.search(r"(?m)^\s*(?:#|\*\*|- \*\*)", text or ""):
        fmt_bad.append("markdown")
    if re.search(r"(?im)^\s*(?:best|thanks|regards|cheers),?\s*$", text or ""):
        fmt_bad.append("sign-off")
    if len(text or "") > 1300:
        fmt_bad.append("long")
    if not (text or "").strip():
        fmt_bad.append("empty")
    s = (5.5 * (sum(got) / len(got) if got else 1) + (2.0 if not bad else 0.0)
         + 1.5 * (sum(addr_ok) / len(addr_ok) if addr_ok else 1) + (1.0 if not fmt_bad else 0.0))
    return {"score": round(s, 2), "missing": [g[0] for g, ok in zip(inc, got) if not ok], "forbidden": bad,
            "unaddressed": [n for n, ok in zip(addr, addr_ok) if not ok], "format": fmt_bad}


def old_paisty(baseline_root: Path, doc: dict, route: dict) -> str:
    """Run the older draft path from another checkout, with the same rules and model."""
    with tempfile.TemporaryDirectory() as tmp:
        scene = Path(tmp) / "scene.json"
        scene.write_text(json.dumps(doc), encoding="utf-8")
        code = (
            "import json,sys\n"
            "from hailports.clip import run\n"
            f"run._instructions = lambda cfg: {RULES!r}\n"
            "run._lookup_notes = lambda cfg, text: ''\n"
            "import hailports.clip.gate as g; g.allowed = lambda cfg: True\n"
            "import hailports.clip.lookups as L; L.facts_for_draft = lambda cfg, text, home=None: ''\n"
            f"route = json.loads({json.dumps(json.dumps(route))})\n"
            f"sys.exit(run.draft_scene(json.load(open({str(scene)!r})), '', 'local', False, route))\n"
        )
        done = subprocess.run([sys.executable, "-c", code], cwd=str(baseline_root), capture_output=True, text=True,
                              timeout=600, env={"PYTHONPATH": str(baseline_root), "HOME": tmp, "PATH": "/usr/bin:/bin:/usr/local/bin"})
        return done.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssh-host", default="")
    ap.add_argument("--remote-url", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--baseline-root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    if args.ssh_host:
        model = {"via": "ssh", "ssh_host": args.ssh_host, "remote_url": args.remote_url, "model": "local"}
    else:
        model = {"via": "http", "base_url": args.base_url or "http://127.0.0.1:18080/v1", "model": "local"}
    with tempfile.TemporaryDirectory() as tmp:
        voice = Path(tmp) / "voice.md"
        voice.write_text(VOICE, encoding="utf-8")
        cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
        results = []
        for case in cases:
            if args.only and case["id"] not in args.only.split(","):
                continue
            first = case["me"].split(",")[0]
            route = {"paiste": {"me": case["me"], "context_file": str(voice), "order": ["m"], "models": {"m": model}}}
            hops = endpoints_from(route)

            def run(messages, hops=hops):
                return complete(hops, messages)[0]

            runs = []
            if not case.get("screenshot_only"):
                runs.append(("text", chat_doc(case) if case.get("chat") else email_doc(case)))
            if case.get("screenshot"):
                runs.append(("screenshot", None))
            for mode, doc in runs:
                t0 = time.time()
                if mode == "screenshot":
                    doc = screenshot.scene_for([FIX / f"{case['id']}.png"], run)
                text, ctx, attempts = compose.draft(doc, route, RULES, run, extra_dirs=[FIX],
                                                    code_roots=[FIX / "repo" / "src", FIX / "repo" / "docs"])
                took = time.time() - t0
                row = {"id": case["id"], "mode": mode, "title": case["title"], "paisty": text,
                       "paisty_score": score(text, case["rubric"]), "reference": case["reference"],
                       "reference_score": score(case["reference"], case["rubric"]), "seconds": round(took, 1),
                       "attempts": len(attempts)}
                if args.baseline_root and mode == "text":
                    old = old_paisty(Path(args.baseline_root), doc, route)
                    row["old_paisty"] = old
                    row["old_score"] = score(old, case["rubric"])
                results.append(row)
                print(f"{case['id']:<22} {mode:<10} paisty {row['paisty_score']['score']:>5}  ref {row['reference_score']['score']:>5}"
                      + (f"  old {row['old_score']['score']:>5}" if 'old_score' in row else "") + f"  {took:.0f}s", flush=True)
        out = Path(args.out) if args.out else HERE / "last_run.json"
        out.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
        wins = sum(1 for r in results if r["paisty_score"]["score"] >= r["reference_score"]["score"])
        print(f"paisty matches or beats the reference on {wins}/{len(results)}")
    return 0 if wins == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
