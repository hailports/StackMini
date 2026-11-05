"""Working paths for each inventory feature. Providers are fakes or 127.0.0.1."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hailports.alerts import deliver, is_loopback, probe_login
from hailports.askdo import gather, respond
from hailports.cli import main as cli_main
from hailports.drafter import citations_ok
from hailports.features import draft_reply
from hailports.idle import next_job, run_job
from hailports.imessage import compose
from hailports.images import to_png_bytes
from hailports.learning import connect as learn_connect
from hailports.learning import prompt_with_lessons, teach, write_runbook
from hailports.memory import watch_pid
from hailports.osapi import ServiceManager, process_rss_mb
from hailports.plugins.tracker import TrackerPlugin
from hailports.precompute import build_packet
from hailports.router import Policy, Router
from hailports.runbooks import search as search_runbooks
from hailports.sprint import packet
from hailports.story import story_reply
from hailports.threads import append, connect as thread_connect, context_messages, maybe_summarize, reset
from hailports.ticket_guard import acquire, connect as guard_connect, remember
from hailports.webtools import gather_web, news, parse_feed, parse_search_html, search
from hailports.notes import (
    add,
    connect as brain_connect,
    embed,
    hybrid_search,
    ingest_path,
    ingest_url,
    note_meta,
    reembed_stale,
    stale_ids,
    store_vector,
)
from hailports.worknow import brief
from hailports.sentinel import run_once
from hailports_setup.models import embed_server_args
from tests.test_modes import Fake

ROOT = Path(__file__).resolve().parents[1]


def _router(text: str = "model said this unique phrase") -> tuple[Router, Fake]:
    local = Fake("local", text)
    return Router("local", local, None, Policy()), local


def test_hybrid_search_prefers_the_closer_vector(tmp_path: Path):
    con = brain_connect(tmp_path / "b.sqlite")
    fruit = add(con, "fruit", "apple fruit grows on trees")
    chip = add(con, "chip", "apple computers sit on desks")
    store_vector(con, fruit, "bge", [1.0, 0.0])
    store_vector(con, chip, "bge", [0.0, 1.0])
    rows = hybrid_search(con, "apple", [1.0, 0.0], limit=2)
    assert rows[0]["id"] == fruit
    assert rows[0]["score"] > rows[1]["score"]


def test_ingest_connectors_and_embed_server(tmp_path: Path):
    con = brain_connect(tmp_path / "b.sqlite")
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "ports.md").write_text("The web chat uses port 8080.\n", encoding="utf-8")
    (folder / "page.html").write_text("<html><p>Hello notes</p></html>", encoding="utf-8")
    ids = ingest_path(con, folder)
    assert len(ids) == 2
    again = ingest_path(con, folder)
    assert again == ids

    class Embed(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            self.rfile.read(n)
            body = json.dumps({"data": [{"embedding": [0.2, 0.8]}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            body = b"The fetched page mentions ports."
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Embed)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    vector = embed(f"http://127.0.0.1:{port}", "ports")
    assert vector == [0.2, 0.8]
    doc = ingest_url(con, f"http://127.0.0.1:{port}/page", lambda url: "fetched ports page")
    assert doc
    argv = embed_server_args("models/bge-small-en-v1.5-q8_0.gguf", 8097)
    assert "--embeddings" in argv
    assert "bge-small-en-v1.5-q8_0.gguf" in " ".join(argv)
    assert "8097" in argv
    httpd.shutdown()


def test_freshness_and_embed_autoheal(tmp_path: Path):
    con = brain_connect(tmp_path / "b.sqlite")
    doc_id = add(con, "old", "stale note about ports")
    note_meta(con, doc_id, "old", "paste", "stale note about ports", updated=10)
    assert doc_id in stale_ids(con, now=100000, max_age_s=100)
    calls = []

    def embed_fn(texts):
        calls.append(texts[0])
        return [[0.5, 0.5]]

    assert reembed_stale(con, embed_fn, now=100000, max_age_s=100) == 1
    assert calls and doc_id not in stale_ids(con, now=100000, max_age_s=100)
    mgr = ServiceManager("linux", apply=False)
    lines = run_once(
        {"mode": "local", "label": "com.hailports.local", "llm": {"upstream_url": "http://127.0.0.1:9"},
         "notes": {"embed_url": "http://127.0.0.1:9", "max_age_s": 10}},
        mgr,
        lambda kind: 10,
        now=100000,
        brain=con,
    )
    assert any(line.startswith("embed ") for line in lines)
    assert "stale 0" in lines
    assert any(call[0] == "systemctl" for call in mgr.calls)


def test_learning_loop_updates_prompt_and_runbook(tmp_path: Path):
    con = learn_connect(tmp_path / "l.sqlite")
    teach(con, "ports", "Say the web port first.")
    root = tmp_path / "runbooks"
    path = write_runbook(con, root)
    assert "web port" in path.read_text(encoding="utf-8")
    assert "web port" in prompt_with_lessons(con, root)
    hits = search_runbooks(root, "web port")
    assert hits and hits[0]["title"] == "Corrections"


def test_citation_hold_and_model_draft():
    held_router, held_local = _router("from notes")
    held = draft_reply(held_router, "weather", ["The weather tomorrow is rain."])
    assert held.raw["held"] is True
    assert held.text == ""
    assert len(held_local.calls) == 2
    assert "provider=local" in held.render()

    ok_router, ok_local = _router("Rain is likely. [1]")
    ok = draft_reply(ok_router, "weather", ["The weather tomorrow is rain."])
    assert ok.raw["held"] is False
    assert ok.text == "Rain is likely. [1]"
    assert citations_ok(ok.text, 1)
    assert not citations_ok("Rain is likely.", 1)
    assert len(ok_local.calls) == 1


def test_ask_do_reads_then_drafts(tmp_path: Path):
    book = tmp_path / "runbooks"
    book.mkdir()
    target = book / "backup.md"
    target.write_text("# Backup\n\nCopy the notes file to a second disk.\n", encoding="utf-8")
    before = target.read_bytes()
    bundle = gather("copy the notes", book, allow_root=book)
    assert bundle["changed"] is False
    assert "read-runbook" in bundle["actions"]
    assert target.read_bytes() == before
    router, local = _router("Draft only. No file was changed.")
    reply = respond(router, "copy the notes", bundle)
    assert reply.text == "Draft only. No file was changed."
    assert local.calls
    assert "changed" in json.dumps(local.calls).lower() or "notes" in json.dumps(local.calls).lower()
    refused = gather("password: hunter", book)
    assert refused["kind"] == "refuse"
    assert refused["notes"] == []


def test_idle_skips_when_busy_and_does_not_delete(tmp_path: Path):
    assert next_job(1, {}, 100, 0) is None
    assert next_job(0, {}, 100, 0) == "freshness"
    assert next_job(0, {"freshness": 100}, 100, 0) == "reembed"
    con = brain_connect(tmp_path / "b.sqlite")
    doc = add(con, "long", "port " * 200)
    note_meta(con, doc, "long", "paste", "port " * 200, updated=1)
    marker = tmp_path / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    router, _local = _router("three lines")
    result = run_job("reembed", router=router, brain=con, thread_con=None, now=100000, max_age_s=10)
    assert result["changed_user_files"] is False
    assert result["count"] == 1
    assert marker.read_text(encoding="utf-8") == "keep"
    assert (tmp_path / "keep.txt").exists()


def test_precompute_packet_drops_the_long_tail():
    marker = "UNIQUECONTEXTTOKEN"
    long_text = ("compare the designs " + marker + " ") * 80
    router, local = _router("short brief")
    packet_messages, saved = build_packet(router, long_text)
    assert saved > 0
    blob = json.dumps(packet_messages)
    assert marker not in blob
    assert local.calls


def test_vision_png_and_heic_dependency(tmp_path: Path, monkeypatch):
    from PIL import Image
    png = tmp_path / "dot.png"
    Image.new("RGB", (2, 2), (10, 20, 30)).save(png)
    raw = to_png_bytes(png)
    assert raw.startswith(b"\x89PNG")
    router, local = _router("a red dot")
    from hailports.features import see
    reply = see(router, "what is this", raw)
    assert "provider=local" in reply.render()
    assert any("VISION" in json.dumps(call) for call in local.calls)

    real_import = __import__

    def boom(name, *args, **kwargs):
        if name == "pillow_heif":
            raise ImportError("nope")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", boom)
    heic = tmp_path / "shot.heic"
    heic.write_bytes(b"not-a-photo")
    with pytest.raises(RuntimeError, match="pillow-heif"):
        to_png_bytes(heic)


def test_web_search_and_news(tmp_path: Path):
    html = '<div class="result"><a href="https://example.invalid/a">Alpha</a><p>snippet one</p></div>'
    feed = "<rss><channel><item><title>Headline</title><link>https://example.invalid/n</link><description>Today</description></item></channel></rss>"
    assert parse_search_html(html)[0]["title"] == "Alpha"
    assert parse_feed(feed)[0]["title"] == "Headline"

    class Web(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            body = feed.encode() if path.endswith("/news") else html.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Web)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def fetch(url: str) -> str:
        import urllib.request
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.read().decode()

    hits = search("ports", base + "/search?q={query}", fetch=fetch)
    assert hits[0]["url"].endswith("/a")
    items = news(base + "/news", fetch=fetch)
    assert items[0]["title"] == "Headline"
    notes = gather_web("search the web for ports and the news", base + "/search?q={query}", base + "/news", fetch=fetch)
    assert "Alpha" in notes and "Headline" in notes
    httpd.shutdown()


def test_thread_summary_and_reset(tmp_path: Path):
    con = thread_connect(tmp_path / "t.sqlite")
    for i in range(6):
        append(con, "user", "word " * 80 + str(i))
        append(con, "assistant", "reply " * 80 + str(i))
    router, local = _router("short summary of the thread")
    text = maybe_summarize(con, router, budget_chars=200)
    assert text == "short summary of the thread"
    summary, history = context_messages(con)
    assert summary == text
    assert len(history) == 2
    assert local.calls
    reset(con)
    summary, history = context_messages(con)
    assert summary == ""
    assert history == []


def test_memory_watch_restarts_only_matching_pid(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("hailports.osapi.pid_alive", lambda pid: pid == 10)
    mgr = ServiceManager("linux", apply=False)
    pid_path = tmp_path / "model.pid"
    pid_path.write_text("10\n", encoding="utf-8")
    outcome = watch_pid(mgr, "demo", pid_path, budget_mb=5, model_healthy=True, rss_fn=lambda pid: 50.0)
    assert outcome == "restarted"
    assert mgr.calls[-1][0] == "systemctl"
    mismatch = ServiceManager("linux", apply=False)
    from hailports.memory import tick
    assert tick(mismatch, "demo", 10, 11, used_mb=80, budget_mb=5, model_healthy=True) == "refused-pid-mismatch"
    assert mismatch.calls == []
    assert process_rss_mb(0) is None


def test_login_alert_stays_on_a_private_channel(tmp_path: Path):
    assert probe_login("http://127.0.0.1:9/nope", status_fn=lambda url: 401) == "login-broken"
    assert probe_login("http://127.0.0.1:9/ok", status_fn=lambda url: 200) == "ok"
    outbox = tmp_path / "alerts.log"
    posted = []

    def post(url, text):
        posted.append((url, text))

    info = deliver("the login check failed", "http://127.0.0.1:9/alert", outbox, post=post)
    assert info["posted"] is True
    assert "login check failed" in outbox.read_text(encoding="utf-8")
    refused = deliver("the login check failed", "https://example.invalid/hook", outbox, post=post)
    assert refused["reason"] == "channel-not-private"
    assert len(posted) == 1
    assert is_loopback("http://127.0.0.1:9/alert")
    router, local = _router("The login check failed.")
    from hailports.alerts import alert_text
    reply = alert_text(router, "probe")
    assert reply.text == "The login check failed."
    assert "provider=local" in reply.render()
    assert local.calls


def test_sprint_packet_and_near_duplicate_guard(tmp_path: Path):
    packed = packet([{"title": "Write docs", "hours": 3}, {"title": "Later", "hours": 8}], 5, "20260101")
    assert packed["taken"][0]["title"] == "Write docs"
    assert "BEGIN:VEVENT" in packed["ics"]
    assert packed["blocks"][0]["title"] == "Write docs"
    con = guard_connect(tmp_path / "g.sqlite")
    first = acquire(con, "person", "Fix the gate")
    assert first["ok"]
    remember(con, "person", "Fix the gate", "card-1")
    near = acquire(con, "person", "Fix the gate please")
    assert near["reason"] == "duplicate"
    other = acquire(con, "person", "Write the readme")
    assert other["ok"]
    plug = TrackerPlugin({"base_url": "https://tracker.example.invalid", "token": "CHANGE_ME"})
    created = plug.create(con, "person", "Fix the gate now")
    assert created["ok"] is False
    assert created["remote"] is False


def test_model_replies_are_not_canned_templates():
    banned = (
        "No local notes match that question.",
        "No matching runbook. Nothing was changed.",
        "Nothing on the focus list.",
    )
    phrase = "model said this unique phrase"
    router, _local = _router(phrase)
    bundle = {"kind": "ask", "notes": ["The web chat uses port 8080."], "actions": ["search-notes"], "changed": False}
    replies = [
        respond(router, "what port", bundle).text,
        story_reply(router, "add a health check", "PROJECT").text,
        brief(router, [{"title": "Ship", "detail": "the readme"}]).text,
        compose(router, "hello").text,
    ]
    for text in replies:
        assert text == phrase
        for line in banned:
            assert line not in text
        assert "hailports-stats" in respond(router, "what port", bundle).render()


def test_cli_thread_reset_and_idle(tmp_path, monkeypatch):
    monkeypatch.setenv("HAILPORTS_DATA_DIR", str(tmp_path / "data"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join([
            "mode: api",
            "label: com.hailports.local",
            "llm:",
            "  base_url: http://127.0.0.1:9/v1",
            "webchat:",
            "  bind: 127.0.0.1",
            "  port: 9",
            "cloud:",
            "  provider: openai",
            "  base_url: http://127.0.0.1:9/v1",
            "  model: mock",
            "idle:",
            "  interval_s: 0",
            "modules:",
            "  notes: true",
        ]) + "\n",
        encoding="utf-8",
    )
    assert cli_main(["--config", str(cfg), "chat", "--reset"]) == 0
    assert cli_main(["--config", str(cfg), "idle", "--once"]) == 0
    assert cli_main(["--config", str(cfg), "sprint", "4"]) == 0
    page = (ROOT / "hailports" / "webchat.py").read_text(encoding="utf-8")
    assert "reset" in page
    assert "history" in page
