#!/usr/bin/env python3
"""llama-server compatible front for Splash.

Listens where llama-server used to (:18080) and forwards to Splash on an internal port.
Adds what Splash does not serve and every local client still calls:
  GET  /health   llama-server shape ({"status":"ok"} / 503 "Loading model"), from Splash /ready
  GET  /slots    one slot, is_processing = requests in flight through this front
  GET  /props    model_path, n_ctx, modalities
  GET  /metrics  llamacpp:* counters summed from response timings, then Splash's own metrics
  POST /completion  llama-server native completion (prompt, n_predict, stream) over /v1/completions
Rewrites chat and completion bodies: any model name -> the served id, llama-only fields dropped
(cache_prompt, id_slot, n_probs, grammar, ...), n_predict -> max_tokens, repeat_penalty ->
repetition_penalty, top-level json_schema -> response_format, http/file image URLs -> data URLs.
Streams through unbuffered and closes the upstream socket when the client goes away, so Splash
cancels the request (the chat priority proxy relies on that).
"""
from __future__ import annotations

import base64
import http.client
import json
import mimetypes
import os
import select
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fit_context  # noqa: E402

UP_HOST = os.environ.get("SHIM_UP_HOST", "127.0.0.1")
UP_PORT = int(os.environ.get("SHIM_UP_PORT", "18085"))
PORT = int(os.environ.get("SHIM_PORT", "18080"))
SERVED = os.environ.get("SHIM_MODEL", "Qwen3.6-35B-A3B")
ALIASES = [a for a in os.environ.get("SHIM_ALIASES", "").split(",") if a]
MODEL_PATH = os.environ.get("SHIM_MODEL_PATH", "")
CTX = int(os.environ.get("SHIM_CTX", "65536"))
DEFAULT_MAX = int(os.environ.get("SHIM_DEFAULT_MAX_TOKENS", "2048"))
LOG = os.environ.get("SHIM_LOG", "")
PARENT = int(os.environ.get("SHIM_PARENT_PID", "0") or 0)

DROP = {"cache_prompt", "id_slot", "slot_id", "n_probs", "n_keep", "samplers", "t_max_predict_ms",
        "n_indent", "repeat_last_n", "mirostat", "mirostat_tau", "mirostat_eta", "typical_p",
        "dry_multiplier", "dry_base", "dry_allowed_length", "dry_penalty_last_n", "dry_sequence_breakers",
        "xtc_probability", "xtc_threshold", "n_cache_reuse", "return_tokens", "timings_per_token",
        "post_sampling_probs", "lora", "grammar", "grammar_lazy", "grammar_triggers", "preserved_tokens",
        "dynatemp_range", "dynatemp_exponent", "penalize_nl", "tfs_z", "n_cmpl", "response_fields",
        "return_progress_llama", "id", "local_only"}

_lock = threading.Lock()
M = {"prompt_tokens_total": 0, "prompt_seconds_total": 0.0, "tokens_predicted_total": 0,
     "tokens_predicted_seconds_total": 0.0, "n_decode_total": 0, "n_busy_slots_per_decode": 1,
     "requests_total": 0, "requests_failed": 0, "inflight": 0, "grammar_dropped": 0,
     "fit_trimmed": 0, "fit_refused": 0}
_ready = {"t": 0.0, "ok": False}
_ids = {"t": 0.0, "ids": []}


def served_id() -> str:
    """The id Splash serves (e.g. local/<name>); SHIM_MODEL if it is one of them."""
    now = time.time()
    if now - _ids["t"] > 30 or not _ids["ids"]:
        try:
            c = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=3)
            c.request("GET", "/v1/models")
            r = c.getresponse()
            if r.status == 200:
                _ids["ids"] = [m.get("id") for m in json.loads(r.read()).get("data") or [] if m.get("id")]
                _ids["t"] = now
            c.close()
        except (OSError, ValueError):
            pass
    ids = _ids["ids"]
    if SERVED in ids or not ids:
        return SERVED
    return ids[0]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')} {msg}\n"
    if LOG:
        try:
            with open(LOG, "a") as f:
                f.write(line)
        except OSError:
            pass
    else:
        sys.stderr.write(line)


def account(t: dict | None) -> None:
    if not isinstance(t, dict):
        return
    with _lock:
        M["prompt_tokens_total"] += int(t.get("prompt_n") or 0) - int(t.get("cache_n") or 0)
        M["prompt_seconds_total"] += float(t.get("prompt_ms") or 0) / 1000
        M["tokens_predicted_total"] += int(t.get("predicted_n") or 0)
        M["tokens_predicted_seconds_total"] += float(t.get("predicted_ms") or 0) / 1000
        M["n_decode_total"] += int(t.get("predicted_n") or 0)


def up_ready() -> bool:
    now = time.time()
    if now - _ready["t"] < 1.0:
        return _ready["ok"]
    ok = False
    try:
        c = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=2)
        c.request("GET", "/ready")
        ok = c.getresponse().status == 200
        c.close()
    except OSError:
        ok = False
    _ready.update(t=now, ok=ok)
    return ok


def _data_url(url: str) -> str:
    if url.startswith("data:"):
        return url
    if url.startswith("file://") or url.startswith("/"):
        p = Path(url[7:] if url.startswith("file://") else url)
        raw = p.read_bytes()
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
    else:
        with urllib.request.urlopen(url, timeout=20) as r:
            raw = r.read(32 * 1024 * 1024)
            mime = (r.headers.get_content_type() or "image/png")
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _up_json(path: str, body: dict, timeout: float = 30):
    try:
        c = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=timeout)
        c.request("POST", path, body=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return json.loads(data) if r.status == 200 else None
    except (OSError, ValueError):
        return None


def count_chat(obj: dict):
    body = {k: obj[k] for k in ("model", "messages", "tools", "tool_choice", "chat_template_kwargs",
                                "reasoning_effort", "response_format") if k in obj}
    r = _up_json("/v1/chat/completions/count_tokens", body)
    return int(r["input_tokens"]) if isinstance(r, dict) and "input_tokens" in r else None


def count_text(text: str):
    r = _up_json("/tokenize", {"content": text, "add_special": True})
    return len(r.get("tokens") or []) if isinstance(r, dict) else None


FIT = os.environ.get("SHIM_FIT", "1") != "0"


def fit_request(obj: dict, chat: bool):
    """Trim to the context cap. Returns (obj, refused_message_or_None). Logs counts only."""
    if not FIT:
        return obj, None
    t0 = time.time()
    try:
        if chat:
            obj, st = fit_context.fit(obj, CTX, count_chat, default_out=DEFAULT_MAX)
        else:
            obj, st = fit_context.fit_prompt(obj, CTX, count_text, default_out=DEFAULT_MAX)
    except Exception as e:  # noqa: BLE001
        log(f"fit error {type(e).__name__}; forwarding unchanged")
        return obj, None
    if st:
        with _lock:
            M["fit_trimmed"] += 0 if st.get("refused") else 1
            M["fit_refused"] += 1 if st.get("refused") else 0
        log("fit " + " ".join(f"{k}={v}" for k, v in st.items() if v not in (0, False, None)) + f" ms={int((time.time() - t0) * 1000)}")
        if st.get("refused"):
            return obj, f"request needs {st.get('after')} tokens after trimming; the context window allows {st.get('budget')}"
    return obj, None


def rewrite(obj: dict, chat: bool) -> dict:
    if obj.get("grammar"):
        with _lock:
            M["grammar_dropped"] += 1
    if "n_predict" in obj and "max_tokens" not in obj and "max_completion_tokens" not in obj:
        n = obj.get("n_predict")
        obj["max_tokens"] = DEFAULT_MAX if (n is None or int(n) < 0) else max(1, int(n))
    obj.pop("n_predict", None)
    if "repeat_penalty" in obj:
        rp = obj.pop("repeat_penalty")
        if rp not in (None, 1, 1.0) and "repetition_penalty" not in obj:
            obj["repetition_penalty"] = rp
    if "json_schema" in obj and "response_format" not in obj:
        obj["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": obj["json_schema"]}}
    obj.pop("json_schema", None)
    rf = obj.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_object" and isinstance(rf.get("schema"), dict):
        obj["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": rf["schema"]}}
    for k in list(obj):
        if k in DROP:
            obj.pop(k, None)
    if obj.get("logit_bias") in ({},):
        obj.pop("logit_bias", None)
    if obj.get("top_k") is not None and int(obj["top_k"]) < -1:
        obj["top_k"] = -1
    if obj.get("min_p") is not None and float(obj["min_p"]) == 0:
        obj.pop("min_p", None)
    obj["model"] = served_id()
    if chat:
        for msg in obj.get("messages") or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    iu = part.get("image_url")
                    url = iu.get("url") if isinstance(iu, dict) else iu
                    if isinstance(url, str) and not url.startswith("data:"):
                        part["image_url"] = {"url": _data_url(url)}
    return obj


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "splash-shim/1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, obj, ctype="application/json") -> None:
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- GET ----------
    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/health":
            if up_ready():
                return self._send(200, {"status": "ok"})
            return self._send(503, {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}})
        if p == "/slots":
            with _lock:
                busy = M["inflight"] > 0
            return self._send(200, [{"id": 0, "id_task": -1, "n_ctx": CTX, "speculative": True,
                                     "is_processing": busy, "params": {}, "next_token": [{"has_next_token": busy}]}])
        if p == "/props":
            return self._send(200, {"default_generation_settings": {"n_ctx": CTX, "params": {}},
                                    "total_slots": 1, "model_path": MODEL_PATH, "model_alias": SERVED,
                                    "modalities": {"vision": True, "audio": False},
                                    "build_info": "splash (via splash_shim)", "chat_template": ""})
        if p == "/metrics":
            with _lock:
                m = dict(M)
            lines = []
            for k in ("prompt_tokens_total", "prompt_seconds_total", "tokens_predicted_total",
                      "tokens_predicted_seconds_total", "n_decode_total", "n_busy_slots_per_decode"):
                lines += [f"# TYPE llamacpp:{k} counter", f"llamacpp:{k} {m[k]}"]
            pt = m["prompt_tokens_total"] / m["prompt_seconds_total"] if m["prompt_seconds_total"] else 0
            gt = m["tokens_predicted_total"] / m["tokens_predicted_seconds_total"] if m["tokens_predicted_seconds_total"] else 0
            lines += [f"llamacpp:prompt_tokens_seconds {pt:.3f}", f"llamacpp:predicted_tokens_seconds {gt:.3f}",
                      f"llamacpp:requests_processing {m['inflight']}", "llamacpp:requests_deferred 0",
                      f"llamacpp:kv_cache_usage_ratio 0", f"splash_shim_requests_total {m['requests_total']}",
                      f"splash_shim_requests_failed {m['requests_failed']}",
                      f"splash_shim_grammar_dropped_total {m['grammar_dropped']}",
                      f"splash_shim_fit_trimmed_total {m['fit_trimmed']}",
                      f"splash_shim_fit_refused_total {m['fit_refused']}"]
            extra = b""
            try:
                c = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=3)
                c.request("GET", "/metrics")
                r = c.getresponse()
                if r.status == 200:
                    extra = r.read()
                c.close()
            except OSError:
                pass
            return self._send(200, ("\n".join(lines) + "\n").encode() + extra, "text/plain; version=0.0.4")
        return self._proxy(None)

    def do_HEAD(self):
        return self._proxy(None)

    def do_DELETE(self):
        return self._proxy(self._body())

    def do_OPTIONS(self):
        return self._proxy(None)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            return self.rfile.read(n)
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            out = b""
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        return b""

    # ---------- POST ----------
    def do_POST(self):
        p = self.path.split("?", 1)[0]
        raw = self._body()
        if p in ("/v1/chat/completions", "/chat/completions", "/v1/completions", "/completions", "/completion"):
            try:
                obj = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": {"code": 400, "message": "invalid JSON", "type": "invalid_request_error"}})
            native = p == "/completion"
            chat = "chat" in p
            try:
                obj = rewrite(obj, chat)
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": {"code": 400, "message": f"image fetch failed: {type(e).__name__}", "type": "invalid_request_error"}})
            if native and isinstance(obj.get("prompt"), list) and obj["prompt"] and isinstance(obj["prompt"][0], str):
                obj["prompt"] = obj["prompt"][0]
            obj, refused = fit_request(obj, chat)
            if refused:
                return self._send(400, {"error": {"code": 400, "message": refused, "type": "invalid_request_error",
                                                  "code_name": "context_length_exceeded"}})
            if native:
                if isinstance(obj.get("prompt"), list) and obj["prompt"] and isinstance(obj["prompt"][0], str):
                    obj["prompt"] = obj["prompt"][0]
                obj.setdefault("max_tokens", DEFAULT_MAX)
                if obj.get("stream"):
                    obj["stream_options"] = {"include_usage": True}
            path = "/v1/chat/completions" if chat else "/v1/completions"
            return self._proxy(json.dumps(obj).encode(), path=path, native=native, account_it=True)
        return self._proxy(raw)

    # ---------- relay ----------
    def _proxy(self, body: bytes | None, path: str | None = None, native: bool = False, account_it: bool = False):
        path = path or self.path
        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length", "transfer-encoding", "connection", "accept-encoding")}
        if body is not None:
            hdrs["Content-Length"] = str(len(body))
            if account_it:
                for k in [k for k in hdrs if k.lower() == "content-type"]:
                    hdrs.pop(k)
                hdrs["Content-Type"] = "application/json"
        with _lock:
            M["inflight"] += 1
            M["requests_total"] += int(account_it)
        conn = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=3600)
        try:
            try:
                conn.request(self.command, path, body=body, headers=hdrs)
                # wait for headers, but notice a client that hangs up during prefill
                while True:
                    r, _, _ = select.select([conn.sock, self.connection], [], [], 1.0)
                    if self.connection in r:
                        try:
                            peek = self.connection.recv(1, socket.MSG_PEEK)
                        except OSError:
                            peek = b""
                        if not peek:
                            raise ConnectionAbortedError("client gone")
                        if conn.sock not in r:
                            time.sleep(0.2)
                    if conn.sock in r:
                        break
                resp = conn.getresponse()
            except ConnectionAbortedError:
                return
            except OSError as e:
                with _lock:
                    M["requests_failed"] += int(account_it)
                return self._send(503, {"error": {"code": 503, "message": f"model not reachable: {type(e).__name__}", "type": "unavailable_error"}})
            ctype = resp.getheader("Content-Type") or ""
            if resp.status >= 400 and account_it:
                with _lock:
                    M["requests_failed"] += 1
            if native:
                return self._native_out(resp)
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("content-length", "transfer-encoding", "connection", "date", "server"):
                    self.send_header(k, v)
            if "text/event-stream" in ctype:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                buf = b""
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    if account_it:
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            if b'"timings"' in line and line.startswith(b"data:"):
                                try:
                                    account(json.loads(line[5:]).get("timings"))
                                except ValueError:
                                    pass
                    try:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    except OSError:
                        return  # client gone: finally closes upstream -> Splash cancels
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass
            else:
                data = resp.read()
                if account_it and resp.status == 200:
                    try:
                        account(json.loads(data).get("timings"))
                    except ValueError:
                        pass
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
        finally:
            conn.close()
            with _lock:
                M["inflight"] -= 1

    def _native_out(self, resp):
        """OpenAI text completion -> llama-server /completion shape."""
        ctype = resp.getheader("Content-Type") or ""
        if resp.status != 200:
            data = resp.read()
            return self._send(resp.status, data)
        if "text/event-stream" not in ctype:
            o = json.loads(resp.read())
            t = o.get("timings") or {}
            account(t)
            ch = (o.get("choices") or [{}])[0]
            u = o.get("usage") or {}
            return self._send(200, {"content": ch.get("text") or "", "stop": True, "model": SERVED,
                                    "tokens_predicted": u.get("completion_tokens", t.get("predicted_n", 0)),
                                    "tokens_evaluated": u.get("prompt_tokens", t.get("prompt_n", 0)),
                                    "stop_type": "limit" if ch.get("finish_reason") == "length" else "eos",
                                    "truncated": False, "timings": t})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        buf = b""
        last_t: dict = {}
        usage: dict = {}

        def emit(obj):
            b = b"data: " + json.dumps(obj).encode() + b"\n\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))
            self.wfile.flush()
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:") or line == b"data: [DONE]":
                        continue
                    try:
                        o = json.loads(line[5:])
                    except ValueError:
                        continue
                    if o.get("timings"):
                        last_t = o["timings"]
                    if o.get("usage"):
                        usage = o["usage"]
                    for ch in o.get("choices") or []:
                        if ch.get("text"):
                            emit({"content": ch["text"], "stop": False})
            account(last_t)
            emit({"content": "", "stop": True, "model": SERVED, "timings": last_t,
                  "tokens_predicted": usage.get("completion_tokens", last_t.get("predicted_n", 0)),
                  "tokens_evaluated": usage.get("prompt_tokens", last_t.get("prompt_n", 0))})
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            return


def _watch_parent():
    while PARENT:
        try:
            os.kill(PARENT, 0)
        except OSError:
            log(f"parent {PARENT} gone; exiting")
            os._exit(0)
        time.sleep(1)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        if isinstance(e, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        log(f"handler error {type(e).__name__}: {e}")
    allow_reuse_address = True
    request_queue_size = 128


def main():
    threading.Thread(target=_watch_parent, daemon=True).start()
    for i in range(60):
        try:
            srv = Server(("127.0.0.1", PORT), H)
            break
        except OSError as e:
            if i == 59:
                raise
            log(f"bind :{PORT} failed ({e}); retry")
            time.sleep(1)
    log(f"shim :{PORT} -> {UP_HOST}:{UP_PORT} model={SERVED} ctx={CTX}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
