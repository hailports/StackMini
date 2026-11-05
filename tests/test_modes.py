"""Modes, providers, and the guided wizard. Providers are mocked or pointed at 127.0.0.1."""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hailports.cli import main as cli_main
from hailports.doctor import port_open, run_doctor, secret_hits
from hailports.features import answer, draft_reply, see
from hailports.promptui import PlainIO, interpret
from hailports.providers.base import Reply
from hailports.providers.factory import build_router
from hailports.router import Policy, Router, complexity_score, decide, lexical_vectors
from hailports.secrets_store import MemorySecrets
from hailports.stats import Stats
from hailports.wizard import Wizard, add_setup_args, download_with_progress, main as wizard_main, package_command
from hailports.notes import add, brain_status, connect as brain_connect

ROOT = Path(__file__).resolve().parents[1]
KEY = "unit-test-secret"


class Fake:
    def __init__(self, name: str, text: str = "ok") -> None:
        self.label = name
        self.provider = name
        self.model = name + "-model"
        self.calls = []
        self.embed_vectors = None
        self.text = text

    def chat(self, messages, max_tokens=512, temperature=0.2):
        self.calls.append(messages)
        return Reply(self.text, self.label, self.model, 8, 2, {})

    def vision(self, prompt, png, max_tokens=512):
        self.calls.append([{"role": "user", "content": "VISION", "bytes": len(png)}])
        return self.chat([{"role": "user", "content": prompt}])

    def embed(self, texts):
        if self.embed_vectors is None:
            return None
        return [list(self.embed_vectors) for _ in texts]

    def health(self):
        return True, "ok"


def _flat(calls) -> str:
    return json.dumps(calls)


def test_policy_table():
    policy = Policy()
    assert decide("local", 9, 0.0, 5000, policy) == "local"
    assert decide("api", 0, 1.0, 1, policy) == "cloud"
    assert decide("hybrid", 0, 0.9, 20, policy) == "local"
    assert decide("hybrid", 2, 0.9, 20, policy) == "cloud"
    assert decide("hybrid", 0, 0.2, 20, policy) == "cloud"
    assert decide("hybrid", 0, 0.9, 5000, policy) == "cloud"
    assert complexity_score("hello there") == 0
    assert complexity_score("implement a parser") == 2
    assert complexity_score("compare the two designs") == 2


def test_three_modes_with_fakes():
    local = Fake("local", "local answer")
    cloud = Fake("openai", "cloud answer")
    policy = Policy()
    messages = [{"role": "user", "content": "hello"}]
    routed = Router("local", local, cloud, policy).complete(messages)
    assert routed.text == "local answer"
    assert cloud.calls == []
    assert "provider=local" in routed.render()
    assert routed.stats.escalated is False

    local.calls.clear()
    routed = Router("api", local, cloud, policy).complete(messages)
    assert routed.text == "cloud answer"
    assert local.calls == []
    assert routed.stats.provider == "openai"
    assert "provider=openai" in routed.stats.line()

    local.calls.clear()
    cloud.calls.clear()
    routed = Router("hybrid", local, cloud, policy).complete(
        [{"role": "user", "content": "please summarize the notes about ports"}]
    )
    assert routed.stats.escalated is False
    assert cloud.calls == []
    assert "ports" in _flat(local.calls)


def test_hybrid_compresses_before_cloud():
    marker = "UNIQUECONTEXTTOKEN"
    long_text = ("compare the designs " + marker + " ") * 200
    local = Fake("local", "short brief")
    cloud = Fake("openai", "cloud answer")
    routed = Router("hybrid", local, cloud, Policy()).complete([{"role": "user", "content": long_text}])
    assert routed.stats.escalated is True
    assert routed.stats.saved_tokens > 0
    assert marker in _flat(local.calls)
    assert marker not in _flat(cloud.calls)
    assert "provider=openai" in routed.render()
    assert "saved=" in routed.stats.line()


def test_hybrid_low_confidence_escalates():
    local = Fake("local", "not sure")
    cloud = Fake("openai", "cloud answer")
    routed = Router("hybrid", local, cloud, Policy()).complete(
        [{"role": "user", "content": "summarize the note"}]
    )
    assert routed.stats.escalated is True
    assert routed.text == "cloud answer"
    assert cloud.calls


def test_embeddings_fallback():
    local = Fake("local")
    cloud = Fake("openai")
    cloud.embed_vectors = [0.25, 0.75]
    hybrid = Router("hybrid", local, cloud, Policy())
    outcome = hybrid.embed(["ports"])
    assert outcome.provider == "openai"
    assert outcome.fallback is False

    local_only = Router("local", Fake("local"), None, Policy())
    lexical = local_only.embed(["ports"])
    assert lexical.provider == "lexical"
    assert lexical.fallback is True
    again = lexical_vectors(["ports"])
    assert again == lexical_vectors(["ports"])
    assert again != lexical_vectors(["other"])

    anthropic = Fake("anthropic")
    api = Router("api", None, anthropic, Policy())
    fell = api.embed(["ports"])
    assert fell.provider == "lexical"
    assert "provider=lexical" in fell.line("api")


def test_vision_hybrid_sends_description():
    local = Fake("local", "not sure")
    cloud = Fake("openai", "cloud saw it")
    png = b"x" * 400
    routed = Router("hybrid", local, cloud, Policy()).vision("what is this", png)
    assert routed.stats.escalated is True
    assert routed.stats.saved_tokens > 0
    assert any("VISION" in _flat([call]) for call in local.calls)
    assert "VISION" not in _flat(cloud.calls)


def test_features_use_router():
    local = Fake("local", "from notes")
    router = Router("local", local, Fake("openai"), Policy())
    reply = draft_reply(router, "weather", ["The weather tomorrow is rain."])
    assert reply.stats.provider == "local"
    assert local.calls
    seen = see(router, "describe", b"\x89PNG")
    assert "provider=local" in seen.render()
    page = answer(router, "hello")
    assert page.text == "from notes"


class MockAPI(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits.append(("GET", self.path, self._auth()))
        body = {"data": [{"id": "mock"}], "models": []}
        self._send(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        self.server.hits.append(("POST", self.path, self._auth(), "Bearer " in str(self.headers.get("Authorization"))))
        if "embeddings" in self.path or "embedContent" in self.path:
            self._send({"data": [{"embedding": [0.2, 0.8]}], "embedding": {"values": [0.2, 0.8]}})
            return
        if self.path.endswith("/messages"):
            self._send({"content": [{"type": "text", "text": "anthropic-ok"}], "usage": {"input_tokens": 3, "output_tokens": 1}})
            return
        if "generateContent" in self.path:
            self._send({
                "candidates": [{"content": {"parts": [{"text": "google-ok"}]}}],
                "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 1},
            })
            return
        self._send({
            "choices": [{"message": {"content": "openai-ok"}}],
            "usage": {"prompt_tokens": 6, "completion_tokens": 2},
        })
        self.server.bodies.append(raw)

    def _auth(self):
        return bool(self.headers.get("Authorization") or self.headers.get("x-api-key") or self.headers.get("x-goog-api-key"))

    def _send(self, doc):
        raw = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return


def start_mock():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
    httpd.hits = []
    httpd.bodies = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_api_mode_against_mock(monkeypatch):
    monkeypatch.setenv("HAILPORTS_API_KEY", KEY)
    httpd = start_mock()
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}/v1"
    cfg = {
        "mode": "api",
        "cloud": {"provider": "openai", "base_url": base, "model": "mock", "key_env": "HAILPORTS_API_KEY"},
        "webchat": {"bind": "127.0.0.1", "port": 9},
    }
    router = build_router(cfg)
    reply = router.complete([{"role": "user", "content": "hi"}])
    assert reply.text == "openai-ok"
    assert reply.stats.provider == "openai"
    assert reply.stats.prompt_tokens == 6
    assert "provider=openai" in reply.render()
    assert any(hit[2] for hit in httpd.hits)
    embed = router.embed(["note"])
    assert embed.provider == "openai"
    assert embed.vectors[0] == [0.2, 0.8]
    checks = run_doctor(cfg)
    assert all(check.ok for check in checks), checks
    httpd.shutdown()


def test_anthropic_and_google_adapters(monkeypatch):
    monkeypatch.setenv("HAILPORTS_API_KEY", KEY)
    httpd = start_mock()
    port = httpd.server_address[1]
    root = f"http://127.0.0.1:{port}"
    anthropic = {
        "mode": "api",
        "cloud": {"provider": "anthropic", "base_url": root, "model": "claude-haiku", "key_env": "HAILPORTS_API_KEY"},
    }
    reply = build_router(anthropic).complete([{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}])
    assert reply.text == "anthropic-ok"
    assert reply.stats.provider == "anthropic"
    assert reply.stats.prompt_tokens == 3
    fell = build_router(anthropic).embed(["note"])
    assert fell.fallback is True
    google = {
        "mode": "api",
        "cloud": {
            "provider": "google",
            "base_url": root + "/v1beta",
            "model": "gemini-2.0-flash",
            "key_env": "HAILPORTS_API_KEY",
        },
    }
    grew = build_router(google).complete([{"role": "user", "content": "hi"}])
    assert grew.text == "google-ok"
    assert grew.stats.provider == "google"
    vectors = build_router(google).embed(["note"])
    assert vectors.vectors[0][0] == 0.2
    httpd.shutdown()


def test_secret_not_in_config():
    cfg = {"cloud": {"key_env": "HAILPORTS_API_KEY"}, "plugins": {"crm": {"token": "CHANGE_ME"}}}
    assert secret_hits(cfg) == []
    dirty = {"plugins": {"crm": {"token": KEY}}}
    assert secret_hits(dirty)


def _args(tmp_path: Path, extra: list[str]):
    parser = argparse.ArgumentParser()
    add_setup_args(parser)
    return parser.parse_args(["--state-dir", str(tmp_path), *extra])


def test_wizard_yes_three_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("HAILPORTS_API_KEY", KEY)
    monkeypatch.setenv("HAILPORTS_PROMPT", "plain")
    httpd = start_mock()
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}/v1"
    suffix = { "darwin": ".plist", "windows": ".xml" }.get(os.name if False else __import__("hailports.osapi", fromlist=["current_os"]).current_os(), ".service")
    local_dir = tmp_path / "local"
    assert wizard_main([
        "--yes", "--mode", "local", "--allow-missing-model", "--state-dir", str(local_dir),
        "--web-port", "19171", "--model-port", "19172", "--embed-port", "19173", "--model", "small",
    ]) == 0
    local_cfg = (local_dir / "config.yaml").read_text(encoding="utf-8")
    assert "mode: local" in local_cfg
    assert KEY not in local_cfg
    assert (local_dir / "services" / f"model{suffix}").exists()

    api_dir = tmp_path / "api"
    assert wizard_main([
        "--yes", "--mode", "api", "--provider", "openai", "--base-url", base, "--cloud-model", "mock",
        "--state-dir", str(api_dir), "--web-port", "19174",
    ]) == 0
    api_text = (api_dir / "config.yaml").read_text(encoding="utf-8")
    assert "mode: api" in api_text
    assert KEY not in api_text
    assert "model.gguf" not in api_text or "model_url: \"\"" in api_text or "model_url:" in api_text
    assert not (api_dir / "services" / f"model{suffix}").exists()
    assert (api_dir / "services" / f"webchat{suffix}").exists()
    assert cli_main(["--config", str(api_dir / "config.yaml"), "doctor"]) == 0
    assert cli_main(["--config", str(api_dir / "config.yaml"), "chat", "hello"]) == 0

    hybrid_dir = tmp_path / "hybrid"
    assert wizard_main([
        "--yes", "--mode", "hybrid", "--provider", "openai", "--base-url", base, "--cloud-model", "mock",
        "--allow-missing-model", "--state-dir", str(hybrid_dir),
        "--web-port", "19175", "--model-port", "19176", "--embed-port", "19177",
    ]) == 0
    hybrid_text = (hybrid_dir / "config.yaml").read_text(encoding="utf-8")
    assert "mode: hybrid" in hybrid_text
    assert KEY not in hybrid_text
    assert (hybrid_dir / "services" / f"model{suffix}").exists()
    httpd.shutdown()


def test_wizard_back_resume_and_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("HAILPORTS_PROMPT", "plain")
    monkeypatch.delenv("HAILPORTS_API_KEY", raising=False)
    progress = {
        "mode": "api",
        "provider": "openai",
        "base_url": "http://127.0.0.1:9/v1",
        "cloud_model": "mock",
        "key_source": "env",
        "_i": 0,
    }
    from hailports import config as config_mod
    state = tmp_path / "resume"
    state.mkdir()
    config_mod.save_file(state / "setup-progress.yaml", progress)
    code = wizard_main([
        "--yes", "--resume", "--mode", "api", "--skip-network", "--state-dir", str(state), "--web-port", "19178",
    ])
    assert code == 0
    assert "mode: api" in (state / "config.yaml").read_text(encoding="utf-8")

    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy_port = busy.getsockname()[1]
    free = 19179
    assert port_open("127.0.0.1", free)
    args = _args(tmp_path / "retry", [])
    io = PlainIO(replies=[str(busy_port), "yes", str(free)])
    wiz = Wizard(args, io, secrets=MemorySecrets())
    wiz.answers["mode"] = "api"
    wiz._order = lambda: ["ports"]
    assert wiz.run() == 0
    assert wiz.answers["web_port"] == str(free)
    busy.close()


def test_keychain_value_is_not_written(tmp_path, monkeypatch):
    monkeypatch.setenv("HAILPORTS_PROMPT", "plain")
    monkeypatch.delenv("HAILPORTS_API_KEY", raising=False)
    secrets = MemorySecrets()
    httpd = start_mock()
    port = httpd.server_address[1]
    args = _args(tmp_path, ["--web-port", "19180"])
    io = PlainIO(replies=[
        "api",
        "openai",
        f"http://127.0.0.1:{port}/v1",
        "mock",
        "keychain",
        KEY,
        "19180",
        "cli,webchat",
        "yes",
    ])
    wiz = Wizard(args, io, secrets=secrets)
    assert wiz.run() == 0
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert KEY not in text
    assert KEY not in (tmp_path / "setup.log").read_text(encoding="utf-8")
    assert secrets.get({"cloud": {}}) == KEY
    httpd.shutdown()


def test_cli_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("HAILPORTS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HAILPORTS_API_KEY", KEY)
    monkeypatch.setenv("HAILPORTS_PROMPT", "plain")
    httpd = start_mock()
    port = httpd.server_address[1]
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "\n".join([
            "mode: api",
            "label: com.hailports.local",
            "os: linux",
            "llm:",
            "  base_url: \"\"",
            "  model: \"\"",
            "webchat:",
            "  bind: 127.0.0.1",
            "  port: 19181",
            "notes:",
            "  embed_url: \"\"",
            "cloud:",
            "  provider: openai",
            f"  base_url: http://127.0.0.1:{port}/v1",
            "  model: mock",
            "  key_env: HAILPORTS_API_KEY",
            "plugins:",
            "  crm:",
            "    base_url: https://crm.example.invalid",
            "    token: CHANGE_ME",
            "runbooks:",
            "  dir: runbooks",
        ]) + "\n",
        encoding="utf-8",
    )
    assert cli_main(["--config", str(cfg_path), "chat", "hello"]) == 0
    assert cli_main(["--config", str(cfg_path), "mode"]) == 0
    assert cli_main(["--config", str(cfg_path), "mode", "local"]) == 0
    assert "mode: local" in cfg_path.read_text(encoding="utf-8")
    assert cli_main(["--config", str(cfg_path), "mode", "api"]) == 0
    note = tmp_path / "note.txt"
    note.write_text("The weather tomorrow is rain.", encoding="utf-8")
    assert cli_main(["--config", str(cfg_path), "drafter", "weather", "--source", str(note)]) == 0
    assert cli_main(["--config", str(cfg_path), "brain", "ingest", "ports", "Web chat uses port 8080."]) == 0
    assert cli_main(["--config", str(cfg_path), "brain", "search", "8080"]) == 0
    assert cli_main(["--config", str(cfg_path), "brain", "status"]) == 0
    assert cli_main(["--config", str(cfg_path), "services", "install"]) == 0
    assert cli_main(["--config", str(cfg_path), "services", "status"]) == 0
    assert cli_main(["--config", str(cfg_path), "services", "start"]) == 0
    assert cli_main(["--config", str(cfg_path), "plugins", "list"]) == 0
    assert cli_main(["--config", str(cfg_path), "plugins", "configure", "crm", "--token", KEY]) == 2
    assert KEY not in cfg_path.read_text(encoding="utf-8")
    assert cli_main(["--config", str(cfg_path), "plugins", "configure", "crm", "--base-url", "https://crm.example.invalid"]) == 0
    assert cli_main(["--config", str(cfg_path), "plugins", "enable", "crm"]) == 0
    assert cli_main(["--config", str(cfg_path), "update"]) == 0
    assert cli_main(["--config", str(cfg_path), "ask", "why did the build fail"]) == 0
    httpd.shutdown()


def test_help_exits_clean():
    for cmd in ("setup", "doctor", "mode", "chat", "serve", "brain", "drafter", "services", "plugins", "update", "uninstall", "ask"):
        try:
            cli_main([cmd, "--help"])
            raise AssertionError(cmd)
        except SystemExit as exc:
            assert exc.code == 0


def test_webchat_router_stats():
    from hailports.webchat import serve_router
    import urllib.request
    router = Router("local", Fake("local", "pong"), None, Policy())
    httpd = serve_router("127.0.0.1", 0, router)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=b'{"messages":[{"role":"user","content":"hi"}]}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        doc = json.loads(resp.read().decode())
    text = doc["choices"][0]["message"]["content"]
    assert "pong" in text
    assert "provider=local" in text
    assert "provider=local" in doc["hailports_stats"]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
        page = resp.read().decode()
    assert "HailPorts" in page
    httpd.shutdown()


def test_brain_lexical_and_download(tmp_path):
    con = brain_connect(tmp_path / "b.sqlite")
    doc_id = add(con, "ports", "The web chat uses port 8080.")
    router = Router("local", Fake("local"), None, Policy())
    outcome = router.embed(["The web chat uses port 8080."])
    from hailports.notes import store_vector
    store_vector(con, doc_id, outcome.provider, outcome.vectors[0])
    status = brain_status(con)
    assert status["docs"] == 1
    assert status["embeddings"][0]["backend"] == "lexical"

    class Feed(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"z" * 64
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Feed)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    seen = []
    io = PlainIO()
    io.progress = lambda label, frac: seen.append(frac)
    dest = tmp_path / "blob.bin"
    download_with_progress(f"http://127.0.0.1:{httpd.server_address[1]}/blob", dest, io)
    assert dest.read_bytes() == b"z" * 64
    assert seen[-1] == 1
    httpd.shutdown()


def test_package_commands_and_prompt_words():
    assert package_command("linux", "ubuntu", "cmake")[0] == "sudo"
    assert "dnf" in package_command("linux", "fedora", "cmake")
    assert package_command("windows", "", "cmake")[0] == "winget"
    assert package_command("darwin", "", "cmake")[0] == "brew"
    assert interpret("2", [("local", ""), ("api", "")], "local") == "api"
    assert interpret("back", [("local", "")], "local") == "back"
    assert interpret("", [("local", "")], "local") == "local"


def test_questionary_branch(monkeypatch):
    monkeypatch.setenv("HAILPORTS_PROMPT", "questionary")

    class FakeQuestionary:
        @staticmethod
        def select(title, choices, default=None):
            class Asked:
                def ask(self):
                    return "api"
            return Asked()

    import sys
    monkeypatch.setitem(sys.modules, "questionary", FakeQuestionary)
    picked = PlainIO().choose("mode", "title", "explain", [("local", "a"), ("api", "b")], "local")
    assert picked == "api"


def test_stats_shape():
    line = Stats("groq", "api", "mock", 3, 4, False, 0).line()
    assert line.startswith("hailports-stats provider=groq mode=api")
    assert "escalated=no" in line
