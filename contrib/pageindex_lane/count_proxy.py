"""Serializing, call-counting reverse proxy for a local OpenAI-compatible server.

- one upstream request at a time (global lock)
- refuses (503) when a hold flag exists or inside a configured quiet window
- refuses any upstream that is not loopback
- GET /__stats returns {"calls": n, "by_path": {...}}
Env: UPSTREAM (default http://127.0.0.1:18080), PORT (18990), HOLD_FILE, QUIET="01:45-03:00", LOG
"""
import json, os, threading, time, urllib.request, urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

UP = os.environ.get("UPSTREAM", "http://127.0.0.1:18080")
assert urlparse(UP).hostname in ("127.0.0.1", "localhost"), "upstream must be loopback"
HOLD = os.path.expanduser(os.environ.get("HOLD_FILE", "~/.local_engine_hold"))
QUIET = os.environ.get("QUIET", "")  # e.g. "01:45-03:00" (local time)
LOG = os.environ.get("LOG", "proxy_calls.jsonl")
LOCK = threading.Lock()
STATS = {"calls": 0, "by_path": {}, "refused": 0}

def blocked():
    if os.path.exists(HOLD):
        return "hold flag present"
    if not QUIET:
        return None
    a, b = QUIET.split("-")
    now = datetime.now().strftime("%H:%M")  # set TZ for the operator's zone
    if a <= now < b:
        return f"quiet window {QUIET}"
    return None

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fwd(self, method):
        if self.path == "/__stats":
            return self._send(200, json.dumps(STATS).encode())
        why = blocked()
        if why:
            STATS["refused"] += 1
            return self._send(503, json.dumps({"error": {"message": f"refused: {why}"}}).encode())
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if n else None
        hdrs = {k: v for k, v in self.headers.items() if k.lower() in ("content-type", "authorization", "accept")}
        with LOCK:
            t0 = time.time()
            req = urllib.request.Request(UP + self.path, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    code, body, ctype = r.status, r.read(), r.headers.get("Content-Type", "application/json")
            except urllib.error.HTTPError as e:
                code, body, ctype = e.code, e.read(), e.headers.get("Content-Type", "application/json")
            dt = time.time() - t0
            if method == "POST":
                STATS["calls"] += 1
                STATS["by_path"][self.path] = STATS["by_path"].get(self.path, 0) + 1
            with open(LOG, "a") as f:
                f.write(json.dumps({"ts": time.time(), "m": method, "path": self.path, "code": code,
                                    "sec": round(dt, 2), "req_bytes": n, "resp_bytes": len(body)}) + "\n")
        self._send(code, body, ctype)

    def do_GET(self):
        self._fwd("GET")

    def do_POST(self):
        self._fwd("POST")

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "18990"))), H).serve_forever()
