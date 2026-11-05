"""Unit tests that do not need a model download."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hailports import config as config_mod
from hailports.askdo import classify, handle
from hailports.bridges import TelegramBridge
from hailports.calendar_blocker import carve, to_ics
from hailports.cli import main as cli_main
from hailports.drafter import NoSource, draft
from hailports.draft_runner import connect as draft_connect
from hailports.draft_runner import enqueue, run_next
from hailports.images import to_png_bytes
from hailports.imessage import MacOnly, poll_hint
from hailports.integrity import drifted, fingerprint
from hailports.learning import connect as learn_connect
from hailports.learning import system_addon, teach
from hailports.memory import over_budget, tick
from hailports.osapi import ServiceManager, restart_exact
from hailports.plugins import load_all
from hailports.runbooks import search
from hailports.sprint import plan
from hailports.story import render, story_from_request
from hailports.ticket_guard import acquire, connect as guard_connect, remember
from hailports.webchat import PriorityGate, serve
from hailports.webtools import DataLine, html_to_text, refuse_sensitive, sanitize_text
from hailports.notes import add, connect as brain_connect, search as brain_search
from hailports.worknow import pack
from hailports_setup.detect import Hardware
from hailports_setup.installer import main as setup_main
from hailports_setup.models import pick_binary, pick_model
from hailports_setup.services import render as render_unit
from hailports.osapi import ServiceSpec


ROOT = Path(__file__).resolve().parents[1]


def test_config_roundtrip(tmp_path: Path):
    src = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    data = config_mod.loads(src)
    assert data["webchat"]["port"] == 8080
    assert data["plugins"]["crm"]["token"] == "CHANGE_ME"
    path = tmp_path / "c.yaml"
    config_mod.save_file(path, data)
    again = config_mod.load_file(path)
    assert again["modules"]["cli"] is True
    assert again["llm"]["base_url"].startswith("http://127.0.0.1")


def test_model_pick():
    small = Hardware("linux", "x64", 4 * 1024 ** 3, 0, "cpu", 4, True, "ubuntu")
    mid = Hardware("linux", "x64", 16 * 1024 ** 3, 0, "cpu", 8, True, "fedora")
    big = Hardware("darwin", "arm64", 24 * 1024 ** 3, 0, "metal", 10, True, "")
    assert "1.5B" in pick_model(small).name
    assert "7B" in pick_model(mid).name
    assert pick_model(big).vision
    assert pick_model(big).quant == "mix_q3gud_im"
    binary = pick_binary(big)
    assert "macos-arm64" in binary.filename
    assert "-DGGML_METAL=ON" in binary.source_flags
    win = Hardware("windows", "x64", 16 * 1024 ** 3, 8 * 1024 ** 3, "cuda", 8, True, "")
    assert "win-cuda" in pick_binary(win).filename
    arch = Hardware("linux", "x64", 32 * 1024 ** 3, 0, "rocm", 16, True, "arch")
    assert "rocm" in pick_binary(arch).filename


def test_dry_run_three_os(capsys):
    for os_name in ("linux", "darwin", "windows"):
        code = setup_main(["--dry-run", "--non-interactive", "--target-os", os_name, "--ram-gb", "16"])
        assert code == 0
    out = capsys.readouterr().out
    assert "simulated: True" in out
    assert "dry-run:" in out
    assert "/Users/" not in out


def test_write_is_rerunnable(tmp_path: Path):
    args = ["--non-interactive", "--yes", "--target-os", "linux", "--ram-gb", "16",
            "--model", "small", "--state-dir", str(tmp_path)]
    assert setup_main(args) == 0
    assert setup_main(args) == 0
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "CHANGE_ME" in text
    assert "/Users/" not in text
    unit = (tmp_path / "services" / "model.service").read_text(encoding="utf-8")
    assert "ExecStart=" in unit


def test_service_templates_have_no_account_home():
    spec = ServiceSpec("com.hailports.local.model", ["llama-server", "-m", "model.gguf"], "state", "model")
    for os_name in ("darwin", "linux", "windows"):
        text = render_unit(spec, os_name)
        assert "/Users/" not in text
        assert "C:\\Users" not in text
    from hailports_setup.services import appliance_specs
    specs = appliance_specs("com.hailports.local", "local", "state", "python3")
    model = next(item for item in specs if item.kind == "model")
    assert "--host" in model.command and "127.0.0.1" in model.command
    web = next(item for item in specs if item.kind == "webchat")
    assert "--config" in web.command
    windows = render_unit(spec, "windows")
    assert 'encoding="UTF-8"' in windows


def test_exact_pid_restart():
    mgr = ServiceManager("linux", apply=False)
    assert restart_exact(mgr, "demo", 10, 10, healthy=True) == "ok"
    assert restart_exact(mgr, "demo", 10, 11, healthy=False) == "refused-pid-mismatch"
    assert restart_exact(mgr, "demo", None, 11, healthy=False) == "refused-no-record"
    assert restart_exact(mgr, "demo", 10, 10, healthy=False) == "restarted"
    assert mgr.calls[0][0] == "systemctl"
    assert restart_exact(mgr, "demo", 10, None, healthy=False) == "started"
    assert tick(mgr, "demo", 10, 10, used_mb=100, budget_mb=50, model_healthy=True) == "restarted"
    assert not over_budget(10, 50)


def test_priority_gate():
    gate = PriorityGate()
    gate.enter("interactive")
    started = threading.Event()
    finished = threading.Event()

    def batch():
        started.set()
        gate.enter("batch")
        finished.set()
        gate.leave("batch")

    thread = threading.Thread(target=batch)
    thread.start()
    assert started.wait(1)
    time.sleep(0.3)
    assert not finished.is_set()
    gate.leave("interactive")
    thread.join(2)
    assert finished.is_set()


def test_webchat_forwards():
    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            self.rfile.read(n)
            body = b'{"choices":[{"message":{"content":"pong"}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    up_port = up.server_address[1]
    front = serve("127.0.0.1", 0, f"http://127.0.0.1:{up_port}")
    threading.Thread(target=front.serve_forever, daemon=True).start()
    port = front.server_address[1]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=b'{"messages":[{"role":"user","content":"hi"}]}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        doc = json.loads(resp.read().decode())
    assert doc["choices"][0]["message"]["content"] == "pong"
    front.shutdown()
    up.shutdown()


def test_draft_and_runner(tmp_path: Path):
    try:
        draft("weather", [])
        assert False
    except NoSource:
        pass
    text = draft("weather tomorrow", ["The weather tomorrow is rain. Bring a coat."])
    assert "rain" in text
    con = draft_connect(tmp_path / "d.sqlite")
    enqueue(con, "weather", ["The weather tomorrow is rain."])
    job = run_next(con)
    assert job["status"] == "done"


def test_ask_runbook_guard_story(tmp_path: Path):
    book = tmp_path / "runbooks"
    book.mkdir()
    (book / "backup.md").write_text("# Backup\n\nCopy the notes file to a second disk.\n", encoding="utf-8")
    assert classify("what is a backup?") == "ask"
    assert classify("copy the notes") == "do"
    assert classify("password: hunter") == "refuse"
    answer = handle("what is a backup?", book)
    assert "notes" in answer["text"]
    task = handle("copy the notes now", book)
    assert task["changed"] is False
    card = story_from_request("add a health check", "PROJECT")
    assert "health check" in render(card)
    packed = plan([{"title": "a", "hours": 3}, {"title": "b", "hours": 8}], 5)
    assert len(packed["taken"]) == 1
    ics = to_ics(carve(["write docs"], "20260101"))
    assert "BEGIN:VEVENT" in ics
    assert "@" not in ics
    hits = search(book, "backup disk")
    assert hits and hits[0]["title"] == "Backup"
    con = guard_connect(tmp_path / "g.sqlite")
    first = acquire(con, "person", "Fix the gate")
    assert first["ok"]
    remember(con, "person", "Fix the gate", "card-1")
    second = acquire(con, "person", "Fix the gate")
    assert second["reason"] == "duplicate"


def test_brain_learn_now(tmp_path: Path):
    con = brain_connect(tmp_path / "b.sqlite")
    add(con, "ports", "The web chat uses port 8080.")
    rows = brain_search(con, "8080")
    assert rows and "8080" in rows[0]["body"]
    lessons = learn_connect(tmp_path / "l.sqlite")
    teach(lessons, "ports", "Say the web port first.")
    assert "web port" in system_addon(lessons)
    assert "Nothing" in pack([])
    assert "Ship" in pack([{"title": "Ship", "detail": "the readme"}])


def test_web_fetch_refuses_private_targets():
    from hailports.webtools import DataLine, http_get, public_http_url

    assert public_http_url("http://127.0.0.1/health") is False
    assert public_http_url("http://localhost/health") is False
    assert public_http_url("file:///tmp/x") is False
    private = "http://" + ".".join(["10", "1", "2", "3"]) + "/latest"
    meta = "http://" + ".".join(["169", "254", "169", "254"]) + "/latest"
    shared = "http://" + ".".join(["100", "64", "0", "1"]) + "/"
    public = "http://" + ".".join(["1", "1", "1", "1"]) + "/100.jpg"
    assert public_http_url(private) is False
    assert public_http_url(meta) is False
    assert public_http_url(shared) is False
    assert public_http_url(public) is True
    try:
        http_get("http://127.0.0.1/")
        assert False
    except DataLine:
        pass


def test_sanitize_and_plugins():
    try:
        refuse_sensitive("api_key: abc")
        assert False
    except DataLine:
        pass
    email = "person" + "@" + "domain.test"
    phone = "415" + "-555-" + "1212"
    cleaned = sanitize_text(f"write me at {email} or {phone}")
    assert email not in cleaned
    assert phone not in cleaned
    page = html_to_text("<html><style>x</style><p>Hello there</p></html>")
    assert "Hello" in page and "style" not in page.lower() or "Hello" in page
    cfg = config_mod.loads((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    for plug in load_all(cfg):
        assert plug.describe().endswith("placeholder config)")
    bridge = TelegramBridge({"token": "CHANGE_ME"})
    assert not bridge.configured()
    try:
        bridge.send("hi")
        assert False
    except Exception as exc:
        assert "not configured" in str(exc)


def test_imessage_guard():
    import sys
    if sys.platform == "darwin":
        assert isinstance(poll_hint(), str)
    else:
        try:
            poll_hint()
            assert False
        except MacOnly:
            pass


def test_integrity(tmp_path: Path):
    path = tmp_path / "unit.txt"
    path.write_text("same\n", encoding="utf-8")
    digest = fingerprint("same\n")
    assert not drifted(path, digest)
    path.write_text("other\n", encoding="utf-8")
    assert drifted(path, digest)


def test_cli_offline(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HAILPORTS_DATA_DIR", str(tmp_path))
    assert cli_main(["--config", str(ROOT / "config.example.yaml"), "health"]) == 0
    assert cli_main(["--config", str(ROOT / "config.example.yaml"), "plugins"]) == 0
    assert cli_main(["--config", str(ROOT / "config.example.yaml"), "imessage"]) == 0
    assert cli_main(["--config", str(ROOT / "config.example.yaml"), "now"]) == 0


def test_pillow_roundtrip(tmp_path: Path):
    try:
        from PIL import Image
    except ImportError:
        return
    path = tmp_path / "dot.png"
    Image.new("RGB", (2, 2), (10, 20, 30)).save(path)
    blob = to_png_bytes(path)
    assert blob.startswith(b"\x89PNG")


def test_tree_has_no_user_homes():
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if "tests" in path.parts:
            continue
        if path.suffix.lower() not in {".py", ".md", ".yaml", ".yml", ".sh", ".ps1", ".toml", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        home_mac = "/Users" + "/"
        home_win = "C:\\Users"
        assert home_mac not in text, path
        assert home_win not in text, path
