#!/usr/bin/env python3
"""Port of paid chat-model health probes to the local model door.

Was: a paid HTTP probe for a cheap health check.
Now: ask the local model one short question; alert only when the door is sick.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    url = os.environ.get("WATCH_MODEL_URL", "http://127.0.0.1:8080/v1/chat/completions")
    body = {
        "model": "local",
        "temperature": 0,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Chat-Priority": "0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
        text = str(raw["choices"][0]["message"]["content"] or "")
        ok = "OK" in text.upper()
    except Exception as exc:
        out = {
            "need": True,
            "summary": f"local chat probe failed: {type(exc).__name__}",
            "bot": "stack",
            "dedupe": "chat-probe-down",
            "force": True,
        }
        sys.stdout.write(json.dumps(out) + "\n")
        return 0
    out = {
        "need": not ok,
        "summary": "local chat probe ok" if ok else "local chat probe bad reply",
        "bot": None if ok else "stack",
        "dedupe": "chat-probe-ok" if ok else "chat-probe-bad",
        "force": not ok,
    }
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
