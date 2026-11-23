"""HTTP gateway: OpenAI /v1/chat/completions + /v1/models + Anthropic /v1/messages."""
from __future__ import annotations

import json
import socket
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .config import BIND_HOSTS, LADDER_PORT, VERSION, ensure_dirs, load_keys
from .providers import list_models
from .quota import QuotaStore
from .router import route_chat, route_stream

_QUOTA = QuotaStore()
_KEYS = load_keys()


def _anthropic_to_openai(payload: dict[str, Any]) -> dict[str, Any]:
    msgs = []
    system = payload.get("system")
    if isinstance(system, str) and system.strip():
        msgs.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = "\n".join(
            b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
        )
        if text.strip():
            msgs.append({"role": "system", "content": text})
    for m in payload.get("messages") or []:
        msgs.append({"role": m.get("role", "user"), "content": m.get("content")})
    out = {
        "model": payload.get("model") or "auto",
        "messages": msgs,
        "max_tokens": payload.get("max_tokens") or 1024,
        "temperature": payload.get("temperature"),
        "stream": bool(payload.get("stream")),
        "tools": payload.get("tools"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _openai_to_anthropic(resp: dict[str, Any]) -> dict[str, Any]:
    text = ""
    tool_use = []
    choices = resp.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        tcalls = msg.get("tool_calls") or []
        for tc in tcalls:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {"raw": fn.get("arguments")}
            tool_use.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id") or "tool",
                    "name": fn.get("name") or "tool",
                    "input": args,
                }
            )
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(tool_use)
    if not blocks:
        blocks = [{"type": "text", "text": ""}]
    stop = "end_turn"
    if tool_use:
        stop = "tool_use"
    usage = resp.get("usage") or {}
    return {
        "id": resp.get("id") or "msg_free_ladder",
        "type": "message",
        "role": "assistant",
        "model": resp.get("model") or "auto",
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
        "free_ladder": resp.get("free_ladder"),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        # keep stdout clean; details go to call jsonl
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            return {}

    def _send(self, code: int, body: dict | bytes, *, content_type: str = "application/json") -> None:
        if isinstance(body, dict):
            data = json.dumps(body).encode("utf-8")
        else:
            data = body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Free-Ladder", VERSION)
        self.end_headers()
        self.wfile.write(data)

    def _headers_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.headers.items()}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/health", "/v1/health"}:
            return self._send(
                200,
                {
                    "ok": True,
                    "gateway": "free-ladder",
                    "version": VERSION,
                    "port": LADDER_PORT,
                },
            )
        if path in {"/v1/models", "/models"}:
            return self._send(200, {"object": "list", "data": list_models(_KEYS)})
        return self._send(404, {"error": {"message": f"not found: {path}"}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path in {"/v1/chat/completions", "/chat/completions"}:
                return self._chat(payload)
            if path in {"/v1/messages", "/messages"}:
                return self._messages(payload)
            return self._send(404, {"error": {"message": f"not found: {path}"}})
        except Exception as exc:
            return self._send(
                500,
                {"error": {"message": str(exc)[:300], "trace": traceback.format_exc()[-500:]}},
            )

    def _chat(self, payload: dict[str, Any]) -> None:
        global _KEYS
        _KEYS = load_keys()
        if payload.get("stream"):
            gen, meta = route_stream(payload, keys=_KEYS, headers=self._headers_dict(), quota=_QUOTA)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Free-Ladder", VERSION)
            self.send_header("X-Free-Ladder-Provider", str(meta.get("provider") or ""))
            self.end_headers()
            for chunk in gen:
                self.wfile.write(chunk)
                self.wfile.flush()
            return
        code, body, meta = route_chat(payload, keys=_KEYS, headers=self._headers_dict(), quota=_QUOTA)
        if isinstance(body, dict):
            body.setdefault("free_ladder", meta)
        self._send(code, body)

    def _messages(self, payload: dict[str, Any]) -> None:
        global _KEYS
        _KEYS = load_keys()
        oai = _anthropic_to_openai(payload)
        if oai.get("stream"):
            # anthropic streaming shim: convert to non-stream for reliability
            oai["stream"] = False
        code, body, meta = route_chat(oai, keys=_KEYS, headers=self._headers_dict(), quota=_QUOTA)
        if code >= 400:
            return self._send(code, body if isinstance(body, dict) else {"error": {"message": str(body)}})
        anth = _openai_to_anthropic(body if isinstance(body, dict) else {})
        anth["free_ladder"] = meta
        self._send(code, anth)


class DualBindServer:
    """Bind the same handler on multiple hosts (127.0.0.1 + Tailscale IP)."""

    def __init__(self, hosts: list[str], port: int) -> None:
        self.hosts = hosts
        self.port = port
        self.servers: list[ThreadingHTTPServer] = []
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        ensure_dirs()
        load_keys()
        for host in self.hosts:
            httpd = ThreadingHTTPServer((host, self.port), Handler)
            httpd.daemon_threads = True
            t = threading.Thread(target=httpd.serve_forever, name=f"free-ladder-{host}", daemon=True)
            self.servers.append(httpd)
            self.threads.append(t)
            t.start()
            print(f"free-ladder listening on http://{host}:{self.port}", flush=True)

    def serve_forever(self) -> None:
        self.start()
        try:
            while True:
                for t in self.threads:
                    t.join(timeout=1.0)
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self) -> None:
        for s in self.servers:
            try:
                s.shutdown()
            except Exception:
                pass


def serve(hosts: list[str] | None = None, port: int | None = None) -> int:
    hosts = hosts or BIND_HOSTS
    port = port or LADDER_PORT
    usable: list[str] = []
    for h in hosts:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((h, port))
            usable.append(h)
        except OSError as e:
            print(f"free-ladder bind skip {h}:{port} ({e})", flush=True)
        finally:
            try:
                sock.close()
            except Exception:
                pass
    if not usable:
        print("free-ladder: no bindable hosts", flush=True)
        return 1
    DualBindServer(usable, port).serve_forever()
    return 0
