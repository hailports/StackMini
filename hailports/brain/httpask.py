"""Loopback or overlay HTTP ask endpoint.

Bind addresses other than loopback are refused unless `allow_overlay` is set,
and then only an overlay address is accepted. A public address is never accepted.
When a token is configured it is required. When it is not, binding to loopback
or to the overlay is the access check. The tailscale command is not required.
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from hailports.brain.ask import ask


def overlay_network() -> ipaddress.IPv4Network:
    # Carrier-grade range, built without a dotted literal so the source stays plain.
    return ipaddress.IPv4Network((100 << 24 | 64 << 16, 10))


def bind_allowed(host: str, *, allow_overlay: bool = False) -> bool:
    name = (host or "").strip().lower()
    if name in ("localhost",):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if allow_overlay and isinstance(address, ipaddress.IPv4Address) and address in overlay_network():
        return True
    return False


class AskServer(ThreadingHTTPServer):
    def __init__(self, host: str, port: int, con: sqlite3.Connection, token: str = "", allow_overlay: bool = False) -> None:
        if not bind_allowed(host, allow_overlay=allow_overlay):
            raise OSError(f"refusing bind {host}")
        self.con = con
        self.db_path = _db_path(con)
        self.token = token or ""
        self.allow_overlay = allow_overlay
        super().__init__((host, port), AskHandler)


class AskHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _authorized(self) -> bool:
        expected = self.server.token
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not supplied:
            supplied = self.headers.get("X-API-Key", "").strip()
        return supplied == expected

    def _send(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _params(self) -> dict:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            raw = self.rfile.read(length)
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    body = {}
        def one(name, default=None):
            if name in body:
                return body[name]
            values = query.get(name)
            if values:
                return values[0]
            return default
        graph_raw = one("graph", False)
        if isinstance(graph_raw, str):
            graph = graph_raw.lower() in ("1", "true", "yes")
        else:
            graph = bool(graph_raw)
        try:
            k = int(one("k", 8))
        except (TypeError, ValueError):
            k = 8
        return {
            "q": str(one("q", "") or ""),
            "scope": str(one("scope", "all") or "all"),
            "k": max(1, min(k, 50)),
            "graph": graph,
        }

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def _route(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"ok": True})
            return
        if path != "/v1/ask":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        params = self._params()
        if not params["q"].strip():
            self._send(400, {"ok": False, "error": "q is required"})
            return
        try:
            result = self._ask(params)
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
            return
        self._send(200, result)

    def _ask(self, params: dict) -> dict:
        path = getattr(self.server, "db_path", None)
        if path:
            con = sqlite3.connect(path)
            try:
                return ask(con, params["q"], k=params["k"], scope=params["scope"], graph=params["graph"])
            finally:
                con.close()
        return ask(self.server.con, params["q"], k=params["k"], scope=params["scope"], graph=params["graph"])


def _db_path(con: sqlite3.Connection) -> str | None:
    try:
        rows = con.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for _seq, name, filename in rows:
        if name == "main" and filename:
            return str(filename)
    return None


def serve(host: str, port: int, con: sqlite3.Connection, token: str = "", allow_overlay: bool = False) -> AskServer:
    return AskServer(host, port, con, token=token, allow_overlay=allow_overlay)
