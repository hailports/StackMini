"""Local web chat in front of llama-server. Interactive turns go before batch turns."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class PriorityGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._interactive = 0
        self._cv = threading.Condition(self._lock)

    def enter(self, lane: str) -> None:
        with self._cv:
            if lane == "batch":
                while self._interactive > 0:
                    self._cv.wait(timeout=0.2)
            else:
                self._interactive += 1

    def leave(self, lane: str) -> None:
        with self._cv:
            if lane != "batch":
                self._interactive = max(0, self._interactive - 1)
                self._cv.notify_all()

    @property
    def interactive(self) -> int:
        with self._lock:
            return self._interactive


def forward_chat(upstream: str, body: bytes, timeout: float = 300.0) -> tuple[int, bytes, str]:
    url = upstream.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    req = urllib.request.Request(
        url + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "application/json")
        return resp.status, resp.read(), ctype


def make_handler(upstream: str, gate: PriorityGate) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            if self.path.startswith("/health"):
                raw = json.dumps({"status": "ok", "interactive": gate.interactive}).encode()
                self._send(200, raw, "application/json")
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length else b"{}"
            lane = (self.headers.get("X-Hailports-Lane") or "interactive").lower()
            if lane not in ("interactive", "batch"):
                lane = "interactive"
            gate.enter(lane)
            try:
                if self.path.startswith("/v1/chat/completions") or self.path.startswith("/chat"):
                    status, payload, ctype = forward_chat(upstream, body)
                    self._send(status, payload, ctype)
                    return
                self._send(404, b"not found", "text/plain")
            except Exception as exc:
                msg = json.dumps({"error": str(exc)}).encode()
                self._send(502, msg, "application/json")
            finally:
                gate.leave(lane)

        def _send(self, status: int, payload: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def make_router_handler(router, gate: PriorityGate):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            if self.path.startswith("/health"):
                raw = json.dumps({"status": "ok", "interactive": gate.interactive, "mode": router.mode}).encode()
                self._send(200, raw, "application/json")
                return
            if self.path in ("/", "/chat"):
                self._send(200, _PAGE, "text/html; charset=utf-8")
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length else b"{}"
            lane = (self.headers.get("X-Hailports-Lane") or "interactive").lower()
            if lane not in ("interactive", "batch"):
                lane = "interactive"
            gate.enter(lane)
            try:
                doc = json.loads(body.decode("utf-8") or "{}")
                if doc.get("reset"):
                    payload = {"reset": True, "choices": [{"message": {"role": "assistant", "content": "reset"}}]}
                    self._send(200, json.dumps(payload).encode(), "application/json")
                    return
                messages = doc.get("messages") or [{"role": "user", "content": str(doc.get("prompt") or "")}]
                from hailports.precompute import build_packet
                from hailports.providers.base import message_text
                full = message_text(messages)
                budget = int(getattr(getattr(router, "policy", None), "compact_char_budget", 1600) or 1600)
                if len(full) > budget:
                    messages, _saved = build_packet(router, full)
                reply = router.complete(messages)
                rendered = reply.render()
                payload = {
                    "choices": [{"message": {"role": "assistant", "content": rendered}}],
                    "hailports_stats": reply.stats.line(),
                    "model": reply.model,
                }
                self._send(200, json.dumps(payload).encode(), "application/json")
            except Exception as exc:
                msg = json.dumps({"error": str(getattr(exc, "what", exc)), "how": str(getattr(exc, "how", ""))}).encode()
                self._send(502, msg, "application/json")
            finally:
                gate.leave(lane)

        def _send(self, status: int, payload: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


_PAGE = b"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>HailPorts</title></head>
<body>
<h1>HailPorts</h1>
<form id="f">
<textarea name="q" rows="4" cols="60"></textarea>
<p><button type="submit">Send</button> <button type="button" id="reset">reset</button></p>
</form>
<pre id="out"></pre>
<script>
var history = [];
document.getElementById("reset").onclick = function () {
  history = [];
  document.getElementById("out").textContent = "reset";
  fetch("/v1/chat/completions", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({reset: true})
  });
};
document.getElementById("f").onsubmit = function (ev) {
  ev.preventDefault();
  var text = document.querySelector("textarea").value;
  history.push({role: "user", content: text});
  fetch("/v1/chat/completions", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({messages: history})
  }).then(function (r) { return r.json(); }).then(function (doc) {
    var msg = doc.choices && doc.choices[0] && doc.choices[0].message;
    var content = msg ? msg.content : JSON.stringify(doc);
    history.push({role: "assistant", content: content});
    document.getElementById("out").textContent = content;
  });
};
</script>
</body></html>
"""


def require_loopback(bind: str) -> str:
    """Refuse a non-loopback listen address unless the operator opts in."""
    host = (bind or "").strip().lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return host
    import os
    if os.environ.get("HAILPORTS_ALLOW_LAN") == "1" and host:
        return host
    raise RuntimeError(
        "Refusing to listen on that address. The safe default is 127.0.0.1. "
        "Set HAILPORTS_ALLOW_LAN=1 only when you mean to expose the port."
    )


def serve_router(bind: str, port: int, router, gate: PriorityGate | None = None) -> ThreadingHTTPServer:
    gate = gate or PriorityGate()
    host = require_loopback(bind)
    return ThreadingHTTPServer((host, port), make_router_handler(router, gate))


def serve(bind: str, port: int, upstream: str, gate: PriorityGate | None = None) -> ThreadingHTTPServer:
    gate = gate or PriorityGate()
    host = require_loopback(bind)
    httpd = ThreadingHTTPServer((host, port), make_handler(upstream, gate))
    return httpd


def serve_in_thread(bind: str, port: int, upstream: str) -> tuple[ThreadingHTTPServer, threading.Thread]:
    httpd = serve(bind, port, upstream)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def main(argv: list[str] | None = None) -> int:
    import argparse
    from hailports.config import load_file

    parser = argparse.ArgumentParser(prog="hailports.webchat")
    parser.add_argument("--config", required=True)
    parser.add_argument("--upstream", default="")
    args = parser.parse_args(argv)
    cfg = load_file(__import__("pathlib").Path(args.config))
    web = cfg.get("webchat") or {}
    upstream = args.upstream or (cfg.get("llm") or {}).get("upstream_url") or "http://127.0.0.1:18080"
    httpd = serve(str(web.get("bind") or "127.0.0.1"), int(web.get("port") or 8080), str(upstream))
    print(f"webchat {web.get('bind') or '127.0.0.1'}:{web.get('port') or 8080}")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
