#!/usr/bin/env python3
"""Offline coop replay. Synthetic tasks only. Pass --live-url to call a local server."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from hailports.coop_route import (  # noqa: E402
    allow_local_finish,
    compression_ok,
    estimate_tokens,
    plain_answer,
)


def live_complete(url: str, prompt: str) -> tuple[str, int]:
    payload = {
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 180,
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    text = plain_answer(msg.get("content") or "")
    timings = data.get("timings") or {}
    tokens = int(timings.get("prompt_n") or 0) + int(timings.get("predicted_n") or 0)
    if not tokens:
        tokens = estimate_tokens(prompt) + estimate_tokens(text)
    return text, tokens


def stub_complete(prompt: str) -> tuple[str, int]:
    """Deterministic stand-in so the harness runs with no model."""
    if "help me" in prompt.lower():
        text = "I am not sure what to change."
    elif "email" in prompt.lower() or "customer" in prompt.lower():
        text = "Dear customer, we are sorry about the outage."
    elif "Compress" in prompt:
        text = "waiting 12, finished 3, skipped 40, unsure 7"
    else:
        text = "waiting 12, finished 3, skipped 40, unsure 7"
    return text, estimate_tokens(prompt) + estimate_tokens(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", default=str(Path(__file__).with_name("sample_tasks.jsonl")))
    parser.add_argument("--live-url", default="")
    args = parser.parse_args()
    complete = (lambda prompt: live_complete(args.live_url, prompt)) if args.live_url else stub_complete
    rows = [json.loads(line) for line in Path(args.tasks).read_text().splitlines() if line.strip()]
    cache: dict[str, str] = {}
    print("id\tarm\tprem_in\tprem_out\tlocal\tpass")
    for row in rows:
        task = row["task"]
        facts = row.get("facts") or []
        base_in = estimate_tokens(task)
        base_out = estimate_tokens(" ".join(facts) or "premium answer")
        print(f"{row['id']}\tbaseline\t{base_in}\t{base_out}\t0\t1")
        if row.get("repeat_of") and task in cache:
            print(f"{row['id']}\tcache\t0\t0\t0\t1")
            continue
        text, local = complete(task)
        passed = allow_local_finish(task, text, None) and all(f in text for f in facts)
        if passed:
            cache[task] = text
            print(f"{row['id']}\tjev_gate\t0\t0\t{local}\t1")
        else:
            print(f"{row['id']}\tjev_gate\t{base_in}\t{base_out}\t{local}\t0")
        if len(task) >= 80:
            summary, local_c = complete("Compress the context into at most 8 bullets.\n" + task)
            if compression_ok(task, summary, limit=80):
                pin = estimate_tokens(summary)
                print(f"{row['id']}\tprecompress\t{pin}\t{base_out}\t{local_c}\t1")
            else:
                print(f"{row['id']}\tprecompress\t{base_in}\t{base_out}\t{local_c}\t0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
