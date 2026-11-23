#!/usr/bin/env python3
"""free-ladder MCP (stdio) — ask_free_model for Cursor/Claude/Codex seats.

Offload summarize/classify/draft/search/long-context to :8790 before burning
subscription tokens. Never prints keys. Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SERVER = {"name": "free-ladder", "version": "1.0.0"}
BASE = os.environ.get("FREE_LADDER_URL", "http://127.0.0.1:8790").rstrip("/")

TOOLS = [
    {
        "name": "ask_free_model",
        "description": (
            "Send a routine subtask to the free-ladder gateway (:8790) which climbs "
            "free rungs (local→Cerebras→Groq→Gemini→OpenRouter:free→Plugsky) before "
            "any subscription. Modes: summarize, classify, draft, search, long-context, chat. "
            "Use this FIRST for mechanical/cheap work; keep Cursor/Claude/Codex for hard agentic work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["summarize", "classify", "draft", "search", "long-context", "chat"],
                    "default": "chat",
                },
                "system": {"type": "string"},
                "max_tokens": {"type": "integer", "default": 1024},
                "skip_local": {"type": "boolean", "default": False},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "free_ladder_health",
        "description": "Check free-ladder gateway /health.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_MODE_SYSTEM = {
    "summarize": "Summarize clearly and briefly. Facts only.",
    "classify": "Classify. Reply with the label and one short reason.",
    "draft": "Draft plain text. No fluff. No sign-off.",
    "search": "Extract the facts that answer the question. Cite snippets.",
    "long-context": "Read the full context. Answer with citations to sections.",
    "chat": "Be concise and direct.",
}


def _complete(prompt: str, mode: str, system: str | None, max_tokens: int, skip_local: bool) -> str:
    sys_msg = system or _MODE_SYSTEM.get(mode or "chat") or _MODE_SYSTEM["chat"]
    body = {
        "model": "auto",
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": int(max_tokens or 1024),
        "stream": False,
    }
    headers = {"Content-Type": "application/json", "User-Agent": "free-ladder-mcp/1.0"}
    if skip_local:
        headers["X-Free-Ladder-Skip-Local"] = "1"
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        # in-process fallback
        try:
            from core.free_ladder.router import route_chat
            from core.free_ladder.config import load_keys

            code, payload, meta = route_chat(body, keys=load_keys(), headers=headers)
            if code >= 400:
                return f"free-ladder error: {payload}"
        except Exception as exc2:
            return f"free-ladder unreachable ({BASE}): {exc}; fallback: {exc2}"
    text = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    meta = payload.get("free_ladder") or {}
    prov = meta.get("provider") or payload.get("model") or "?"
    return f"[free-ladder provider={prov} rung={meta.get('rung')}]\n{text.strip()}"


def _health() -> str:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=5) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc), "base": BASE})


def result(rid, res):
    return {"jsonrpc": "2.0", "id": rid, "result": res}


def handle(m):
    meth, rid = m.get("method"), m.get("id")
    if meth == "initialize":
        pv = (m.get("params") or {}).get("protocolVersion") or "2024-11-05"
        return result(
            rid,
            {
                "protocolVersion": pv,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER,
            },
        )
    if meth == "tools/list":
        return result(rid, {"tools": TOOLS})
    if meth == "tools/call":
        params = m.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "free_ladder_health":
            text = _health()
        elif name == "ask_free_model":
            text = _complete(
                str(args.get("prompt") or ""),
                str(args.get("mode") or "chat"),
                args.get("system"),
                int(args.get("max_tokens") or 1024),
                bool(args.get("skip_local")),
            )
        else:
            text = f"unknown tool: {name}"
        return result(rid, {"content": [{"type": "text", "text": text}]})
    if meth == "notifications/initialized" or meth == "ping":
        return None if rid is None else result(rid, {})
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {meth}"}}
    return None


def main() -> int:
    # load keys for in-process fallback without printing
    keys = Path.home() / ".config/free-llm/keys.env"
    if keys.is_file():
        for raw in keys.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'").strip('"')
            if k and k not in os.environ:
                os.environ[k] = v
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
