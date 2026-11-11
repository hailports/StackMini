"""Guided integrations. Providers are mocked. Nothing here opens the public network."""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from hailports.brain.ask import ask
from hailports.cli import main as cli_main
from hailports.doctor import run_doctor
from hailports.integrations.adapter import draft_adapter, pull_bridge, save_adapters
from hailports.integrations.approved import pull_apple, pull_imap
from hailports.integrations.index import get_cursor, open_brain
from hailports.integrations.oauth import (
    exchange_code,
    google_auth_url,
    google_device_finish,
    google_device_start,
    listen_for_code,
    take_code,
)
from hailports.integrations.store import IntegrationError, TokenBox
from hailports.integrations.sync import disconnect_surface, sync_configured
from hailports.notes import add, search
from hailports.wizard import main as wizard_main
from hailports_setup.connect_assist import kind_for, normalize_adapter, parse_approve, parse_connect_spec


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class Scripted:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers=None, body=None):
        self.calls.append((method, url, dict(headers or {}), body))
        for pred, resp in self.routes:
            if pred(method, url):
                return resp() if callable(resp) else resp
        raise AssertionError(url)


def _cfg(surfaces):
    block = {"kind": "approved", "window_days": 30, "mask": []}
    for name in ("email", "calendar", "notes", "chats"):
        block[name] = surfaces.get(name) or {"enabled": False, "via": "skip", "provider": "", "path": ""}
    block["kind"] = kind_for(block)
    return {"mode": "local", "integrations": block, "webchat": {"bind": "127.0.0.1", "port": 9}}


def _gmail_message(mid, subject, body, when):
    return {
        "id": mid,
        "internalDate": str(int(when * 1000)),
        "snippet": body,
        "payload": {"headers": [{"name": "Subject", "value": subject}], "body": {"data": _b64(body)}},
    }


def test_parse_per_surface_and_providers():
    spec = parse_connect_spec("notes=approved, email=skip")
    assert spec["mode"] == "per"
    assert spec["surfaces"]["notes"] == "approved"
    assert spec["surfaces"]["calendar"] == "skip"
    assert parse_connect_spec("approved")["via"] == "approved"
    assert parse_approve("notes=files,email=gmail")["email"] == "gmail"
    with pytest.raises(ValueError):
        parse_approve("chats=gmail")


def _block_network(monkeypatch):
    """Block the official API client. Loopback health checks stay available."""

    def boom(*args, **kwargs):
        raise AssertionError("network")

    monkeypatch.setattr("hailports.integrations.http.urlopen", boom)


def test_mask_drops_secrets_and_restricted_paths(tmp_path, monkeypatch):
    _block_network(monkeypatch)
    now = time.time()
    token = "gmail-token-value"
    state = tmp_path / "state"
    box = TokenBox(state / "integrations")
    box.put("google", {"access_token": token, "refresh_token": "refresh-1", "expiry": now + 3600, "client_id": "client-test"})
    http = Scripted([
        (lambda m, u: "/messages?" in u, (200, json.dumps({"messages": [{"id": "m1"}, {"id": "old1"}]}))),
        (lambda m, u: u.endswith("/profile"), (200, json.dumps({"historyId": "50"}))),
        (lambda m, u: "/messages/m1" in u, (200, json.dumps(_gmail_message(
            "m1", "Planning lunch", "Bring the outline. password: hush-1 " + token, now - 86400,
        )))),
        (lambda m, u: "/messages/old1" in u, (200, json.dumps(_gmail_message(
            "old1", "Ancienttopic leftover", "Too old to keep", now - (40 * 86400),
        )))),
    ])
    notes = state / "vault" / "finance"
    notes.mkdir(parents=True)
    (notes / "secret.md").write_text("hidden ledger\n", encoding="utf-8")
    cfg = _cfg({
        "email": {"enabled": True, "via": "approved", "provider": "gmail", "path": ""},
        "notes": {"enabled": True, "via": "approved", "provider": "files", "path": str(state / "vault")},
    })
    proofs = sync_configured(cfg, state, http=http, now=now)
    by_surface = {row["surface"]: row for row in proofs}
    assert by_surface["email"]["ok"] is True
    assert by_surface["notes"]["dropped"] >= 1
    con = open_brain(state / "brain.sqlite")
    try:
        blob = " ".join((hit["title"] or "") + " " + (hit["body"] or "") for hit in search(con, "outline", limit=5))
        assert token not in blob
        assert "hush-1" not in blob
        assert "password [masked]" in blob
        assert search(con, "Ancienttopic", limit=5) == []
        assert search(con, "ledger", limit=5) == []
    finally:
        con.close()
    assert "Authorization" not in json.dumps(by_surface)


def test_gmail_history_is_incremental_and_refreshes(tmp_path, monkeypatch):
    _block_network(monkeypatch)
    now = time.time()
    state = tmp_path / "state"
    box = TokenBox(state / "integrations")
    box.put("google", {"access_token": "old-token", "refresh_token": "refresh-1", "expiry": now - 50, "client_id": "client-test"})
    seen = {"token": 0}

    def token_resp():
        seen["token"] += 1
        return 200, json.dumps({"access_token": "new-token", "refresh_token": "refresh-1", "expires_in": 3600})

    http = Scripted([
        (lambda m, u: u.endswith("/token"), token_resp),
        (lambda m, u: "/history?" in u, (200, json.dumps({"historyId": "60", "history": []}))),
        (lambda m, u: "/messages?" in u, (200, json.dumps({"messages": [{"id": "m1"}]}))),
        (lambda m, u: u.endswith("/profile"), (200, json.dumps({"historyId": "50"}))),
        (lambda m, u: "/messages/m1" in u, (200, json.dumps(_gmail_message("m1", "Planning lunch", "Bring the outline", now - 3600)))),
        (lambda m, u: "/messages/m2" in u, (200, json.dumps(_gmail_message("m2", "Roadmap review", "Bring the outline again", now - 100)))),
    ])
    cfg = _cfg({"email": {"enabled": True, "via": "approved", "provider": "gmail", "path": ""}})
    first = sync_configured(cfg, state, http=http, now=now)
    assert first[0]["ok"] is True
    assert seen["token"] == 1
    assert any(call[2].get("Authorization") == "Bearer new-token" for call in http.calls)
    assert not any("/history?" in call[1] for call in http.calls)
    con = open_brain(state / "brain.sqlite")
    try:
        assert get_cursor(con, "email", "gmail") == "50"
    finally:
        con.close()
    http.routes[1] = (lambda m, u: "/history?" in u, (200, json.dumps({
        "historyId": "60",
        "history": [{"messagesAdded": [{"message": {"id": "m2"}}]}],
    })))
    before = len(http.calls)
    second = sync_configured(cfg, state, http=http, now=now + 10)
    assert second[0]["ok"] is True
    later = http.calls[before:]
    assert any("/history?" in call[1] for call in later)
    assert not any("/messages?" in call[1] for call in later)
    # A history reply with no new ids must not fall back to a full list.
    http.calls.clear()
    http.routes[1] = (lambda m, u: "/history?" in u, (200, json.dumps({"historyId": "61"})))
    sync_configured(cfg, state, http=http, now=now + 20)
    assert any("/history?" in call[1] for call in http.calls)
    assert not any("/messages?" in call[1] for call in http.calls)
    stored = box.get("google") or {}
    assert stored.get("access_token") == "new-token"
    assert "new-token" not in repr(box)


def test_graph_imap_caldav_and_apple(tmp_path, monkeypatch):
    _block_network(monkeypatch)
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
    page = "https://graph.microsoft.com/v1.0/me/onenote/pages/p1/content"
    foreign = "https://notgraph.example/page"

    def graph_http(method, url, headers=None, body=None):
        graph_http.urls.append(url)
        if url.endswith("/me/messages") or "/me/messages?" in url:
            return 200, json.dumps({"value": [{"id": "gm1", "subject": "Planning lunch", "bodyPreview": "Bring the outline", "receivedDateTime": stamp}]})
        if "/calendarView" in url:
            return 200, json.dumps({"value": [{"id": "ge1", "subject": "Roadmap review", "start": {"dateTime": stamp}, "body": {"content": "Bring the outline"}}]})
        if url.endswith("/pages") or "/onenote/pages?" in url:
            return 200, json.dumps({"value": [
                {"id": "p1", "title": "Grocery list", "contentUrl": page, "lastModifiedDateTime": stamp},
                {"id": "p2", "title": "Skip foreign", "contentUrl": foreign},
            ]})
        if url == page:
            return 200, "<p>Buy oats today</p>"
        if "caldav" in url:
            assert method == "REPORT"
            raw = body.decode() if isinstance(body, bytes) else ""
            assert "time-range" in raw
            assert "app-secret" not in raw
            return 200, "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:evt-1\nSUMMARY:Roadmap review\nDESCRIPTION:Bring the outline\nDTSTART:20261101T150000Z\nEND:VEVENT\nEND:VCALENDAR\n"
        if "carddav" in url:
            return 200, "BEGIN:VCARD\nFN:North Contact\nUID:card-1\nNOTE:Bring the outline\nEND:VCARD\n"
        raise AssertionError(url)

    graph_http.urls = []
    state = tmp_path / "state"
    box = TokenBox(state / "integrations")
    box.put("graph", {"access_token": "graph-token", "expiry": now + 3600, "client_id": "client-test"})
    box.put("imap", {"host": "imap.fastmail.com", "account": "box", "password": "app-secret", "service": "fastmail"})
    box.put("caldav", {"url": "https://caldav.fastmail.com/dav/box/", "account": "box", "password": "app-secret", "service": "fastmail"})
    box.put("carddav", {"url": "https://carddav.fastmail.com/dav/box/", "account": "box", "password": "app-secret", "service": "fastmail"})
    cfg = _cfg({
        "email": {"enabled": True, "via": "approved", "provider": "graph", "path": ""},
        "calendar": {"enabled": True, "via": "approved", "provider": "graph", "path": ""},
        "notes": {"enabled": True, "via": "approved", "provider": "graph", "path": ""},
    })
    proofs = sync_configured(cfg, state, http=graph_http, now=now)
    assert {row["surface"] for row in proofs if row["ok"]} == {"email", "calendar", "notes"}
    assert foreign not in graph_http.urls
    assert page in graph_http.urls
    con = open_brain(state / "brain.sqlite")
    try:
        assert search(con, "oats", limit=3)
        mail = ask(con, "Planning lunch outline", scope="email")
        assert any("Planning" in (hit.get("title") or "") for hit in mail["hits"])
        wide = ask(con, "Roadmap review outline", scope="all")
        assert wide["hits"]
    finally:
        con.close()

    class Client:
        def login(self, account, password):
            assert password == "app-secret"
            return "OK", []

        def select(self, mailbox, readonly=True):
            return "OK", []

        def search(self, charset, *criteria):
            return "OK", [b"1"]

        def fetch(self, num, spec):
            blob = b"Subject: Planning lunch\r\nDate: 01 Nov 2026 15:00 +0000\r\n\r\nBring the outline"
            return "OK", [(b"1", blob)]

        def logout(self):
            return "BYE", []

    imap_cfg = _cfg({"email": {"enabled": True, "via": "approved", "provider": "imap", "path": ""}})
    imap_state = tmp_path / "imap"
    TokenBox(imap_state / "integrations").put("imap", box.get("imap"))
    imap_proof = sync_configured(imap_cfg, imap_state, imap_factory=lambda host: Client(), now=now)
    assert imap_proof[0]["ok"] is True

    class Bad:
        def login(self, account, password):
            raise RuntimeError("nope " + password)

        def logout(self):
            return None

    with pytest.raises(IntegrationError, match="imap login failed") as caught:
        pull_imap("imap.fastmail.com", "box", "app-secret", since=now - 86400, factory=lambda host: Bad())
    assert "app-secret" not in str(caught.value)

    dav_state = tmp_path / "dav"
    TokenBox(dav_state / "integrations").put("caldav", box.get("caldav"))
    dav_cfg = _cfg({"calendar": {"enabled": True, "via": "approved", "provider": "caldav", "path": ""}})
    assert sync_configured(dav_cfg, dav_state, http=graph_http, now=now)[0]["ok"] is True
    card_state = tmp_path / "card"
    TokenBox(card_state / "integrations").put("carddav", box.get("carddav"))
    card_cfg = _cfg({"notes": {"enabled": True, "via": "approved", "provider": "carddav", "path": ""}})
    assert sync_configured(card_cfg, card_state, http=graph_http, now=now)[0]["ok"] is True

    def runner(script):
        if 'application "Notes"' in script:
            return "n1\nGrocery list\nBuy oats"
        if 'application "Calendar"' in script:
            return "e1\nRoadmap review\nMonday\nBring the outline"
        if 'application "Mail"' in script:
            return "m1\nPlanning lunch\nMonday\nBring the outline"
        if 'application "Messages"' in script:
            return "c1\nTeam chat\nSee the outline"
        return ""

    if sys.platform != "darwin":
        with pytest.raises(IntegrationError, match="macOS"):
            pull_apple("apple-notes")
    apple_state = tmp_path / "apple"
    apple_cfg = _cfg({
        "email": {"enabled": True, "via": "approved", "provider": "apple-mail", "path": ""},
        "calendar": {"enabled": True, "via": "approved", "provider": "apple-calendar", "path": ""},
        "notes": {"enabled": True, "via": "approved", "provider": "apple-notes", "path": ""},
        "chats": {"enabled": True, "via": "approved", "provider": "apple-messages", "path": ""},
    })
    apple = sync_configured(apple_cfg, apple_state, osascript=runner, now=now)
    assert all(row["ok"] for row in apple)


def test_files_incremental_and_bridge_export(tmp_path, monkeypatch):
    _block_network(monkeypatch)
    folder = tmp_path / "notes"
    folder.mkdir()
    target = folder / "checklist.md"
    target.write_text("alphaunique launch checklist\n", encoding="utf-8")
    state = tmp_path / "state"
    cfg = _cfg({"notes": {"enabled": True, "via": "approved", "provider": "files", "path": str(folder)}})
    first = sync_configured(cfg, state, now=time.time())
    assert first[0]["added"] == 1
    second = sync_configured(cfg, state, now=time.time())
    assert second[0]["skipped"] == 1 and second[0]["added"] == 0
    target.write_text("betaunique launch checklist\n", encoding="utf-8")
    third = sync_configured(cfg, state, now=time.time())
    assert third[0]["updated"] == 1
    con = open_brain(state / "brain.sqlite")
    try:
        assert search(con, "alphaunique", limit=3) == []
        assert search(con, "betaunique", limit=3)
    finally:
        con.close()

    public = normalize_adapter(
        {"base_url": "https://notgraph.example/hook", "steps": ["Type the password here", "Keep mail on 127.0.0.1."]},
        "email",
        9222,
    )
    assert public["base_url"] == "http://127.0.0.1:9222"
    assert all("password" not in step for step in public["steps"])
    secret = "unit-test-secret"

    class Cloud:
        def chat(self, messages, max_tokens=500, temperature=0):
            from hailports.providers.base import Reply
            return Reply('{"adapter": {"base_url": "http://127.0.0.1:9222", "steps": ["' + secret + '"]}}', "openai", "mock", 1, 1, {})

    drafted = draft_adapter(Cloud(), os_name="linux", surface="email", port=9222, secret=secret)
    assert drafted["source"] == "defaults"
    assert secret not in json.dumps(drafted)
    bridge_state = tmp_path / "bridge"
    save_adapters(bridge_state / "adapters", {"notes": {"base_url": "https://notgraph.example/hook", "export_path": "/export", "steps": ["Keep notes on 127.0.0.1."]}})
    seen = {}

    def fetch(url):
        seen["url"] = url
        return json.dumps([{"id": "b1", "title": "Bridge note", "body": "Loopback outline"}])

    bridge_cfg = _cfg({"notes": {"enabled": True, "via": "bridges", "provider": "bridge", "path": "", "base_url": "http://127.0.0.1:9224"}})
    proof = sync_configured(bridge_cfg, bridge_state, fetch=fetch, now=time.time())
    assert proof[0]["ok"] is True
    assert seen["url"].startswith("http://127.0.0.1:")
    with pytest.raises(IntegrationError):
        pull_bridge("https://notgraph.example", "/export", "notes", "bridge", fetch)


def test_token_file_mode_disconnect_and_doctor(tmp_path):
    root = tmp_path / "integrations"
    box = TokenBox(root)
    box.put("google", {"access_token": "gmail-token-value", "client_id": "client-test"})
    mode = stat.S_IMODE(os.stat(box.file).st_mode)
    assert mode == 0o600
    assert "gmail-token-value" not in repr(box)
    state = tmp_path / "state"
    other = TokenBox(state / "integrations")
    other.put("google", {"access_token": "gmail-token-value", "client_id": "client-test"})
    cfg = _cfg({
        "email": {"enabled": True, "via": "approved", "provider": "gmail", "path": ""},
        "calendar": {"enabled": True, "via": "approved", "provider": "gcal", "path": ""},
    })
    kept = disconnect_surface(cfg, state, "email", purge=False)
    assert kept["token"] == "kept"
    assert other.get("google")
    dropped = disconnect_surface(cfg, state, "calendar", purge=False)
    assert dropped["token"] == "removed"
    assert other.get("google") is None

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "checklist.md").write_text("checklist launch outline\n", encoding="utf-8")
    brain = tmp_path / "brainstate"
    files = _cfg({"notes": {"enabled": True, "via": "approved", "provider": "files", "path": str(folder)}})
    assert sync_configured(files, brain, now=time.time())[0]["ok"] is True
    con = open_brain(brain / "brain.sqlite")
    try:
        add(con, "keeper", "unrelatedkeeper stays")
    finally:
        con.close()
    (brain / "adapters").mkdir()
    (brain / "adapters" / "notes.json").write_text("{}\n", encoding="utf-8")
    result = disconnect_surface(files, brain, "notes", purge=True)
    assert result["purged"] == 1
    assert not (brain / "adapters" / "notes.json").exists()
    con = open_brain(brain / "brain.sqlite")
    try:
        assert search(con, "checklist", limit=3) == []
        assert search(con, "unrelatedkeeper", limit=3)
    finally:
        con.close()

    bare = tmp_path / "doctor"
    checks = run_doctor(_cfg({"email": {"enabled": True, "via": "approved", "provider": "gmail", "path": ""}}), live=False, root=bare)
    row = {check.name: check for check in checks}
    assert row["integration-email"].ok is False
    assert "token" in row["integration-email"].detail
    assert row["integration-calendar"].ok is True


def test_oauth_device_and_loopback(monkeypatch):
    _block_network(monkeypatch)
    pending = {"n": 0}

    def token():
        pending["n"] += 1
        if pending["n"] == 1:
            return 200, json.dumps({"error": "authorization_pending"})
        return 200, json.dumps({"access_token": "new-token", "refresh_token": "refresh-1", "expires_in": 3600})

    http = Scripted([
        (lambda m, u: "device/code" in u, (200, json.dumps({
            "device_code": "dev-1",
            "user_code": "ABCD-EFGH",
            "verification_url": "https://www.google.com/device",
            "interval": 1,
        }))),
        (lambda m, u: u.endswith("/token"), token),
    ])
    prompt = google_device_start(http, "client-test")
    assert "dev-1" not in repr(prompt)
    token_doc = google_device_finish(http, "client-test", prompt, attempts=3, sleep=lambda _n: None)
    assert token_doc["access_token"] == "new-token"
    assert token_doc["client_id"] == "client-test"
    traded = exchange_code(http, "client-test", "code-1", "http://127.0.0.1:8765/callback")
    assert traded["access_token"] == "new-token"
    with pytest.raises(IntegrationError):
        google_auth_url("client-test", "https://notgraph.example/back", "state1")

    server, queue = listen_for_code("state1", port=0)
    try:
        port = int(server.server_address[1])
        with urlopen(f"http://127.0.0.1:{port}/callback?code=abc&state=state1", timeout=2) as resp:
            assert resp.status == 200
        assert take_code(queue, 2) == "abc"
        with pytest.raises(HTTPError):
            urlopen(f"http://127.0.0.1:{port}/callback?code=nope&state=other", timeout=2)
        with pytest.raises(IntegrationError):
            take_code(queue, 0.2)
    finally:
        server.shutdown()
        server.server_close()


def test_wizard_files_pull_and_approved_stays_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HAILPORTS_PROMPT", "plain")
    monkeypatch.setenv("HAILPORTS_NO_KEYCHAIN", "1")
    monkeypatch.delenv("HAILPORTS_API_KEY", raising=False)
    _block_network(monkeypatch)
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "checklist.md").write_text("Pack the launch checklist outline\n", encoding="utf-8")
    state = tmp_path / "state"
    assert wizard_main([
        "--yes", "--mode", "local", "--skip-network", "--allow-missing-model",
        "--connect", "notes=approved,email=skip,calendar=skip,chats=skip",
        "--approve", "notes=files", "--notes-dir", str(folder), "--pull",
        "--state-dir", str(state), "--web-port", "19311", "--model-port", "19312", "--embed-port", "19313",
    ]) == 0
    log = (state / "setup.log").read_text(encoding="utf-8")
    text = (state / "config.yaml").read_text(encoding="utf-8")
    assert "proof notes pass" in log
    assert "provider: files" in text
    assert "unit-test-secret" not in text
    con = open_brain(state / "brain.sqlite")
    try:
        assert search(con, "checklist", limit=3)
    finally:
        con.close()

    quiet = tmp_path / "quiet"
    assert wizard_main([
        "--yes", "--mode", "local", "--skip-network", "--allow-missing-model",
        "--connect", "approved",
        "--state-dir", str(quiet), "--web-port", "19321", "--model-port", "19322", "--embed-port", "19323",
    ]) == 0
    quiet_text = (quiet / "config.yaml").read_text(encoding="utf-8")
    assert "kind: approved" in quiet_text
    assert not (quiet / "integrations" / "tokens.json").exists()
    assert "Ingest stays off" in capsys.readouterr().out

    skipped = tmp_path / "skipped"
    assert wizard_main([
        "--yes", "--mode", "local", "--skip-network", "--allow-missing-model",
        "--state-dir", str(skipped), "--web-port", "19331", "--model-port", "19332", "--embed-port", "19333",
    ]) == 0
    assert "kind: off" in (skipped / "config.yaml").read_text(encoding="utf-8")


def test_cli_connect_files_then_disconnect(tmp_path, capsys):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "checklist.md").write_text("Remember hushterm and the launch checklist\n", encoding="utf-8")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("mode: local\nwebchat:\n  bind: 127.0.0.1\n  port: 9\n", encoding="utf-8")
    code = cli_main([
        "--config", str(cfg_path), "integrations", "connect", "notes",
        "--provider", "files", "--notes-dir", str(folder), "--mask", "hushterm",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "proof notes pass" in out
    stored = cfg_path.read_text(encoding="utf-8")
    assert "hushterm" not in stored
    mask = json.loads((tmp_path / "integrations" / "mask.json").read_text(encoding="utf-8"))
    assert "hushterm" in mask
    assert stat.S_IMODE(os.stat(tmp_path / "integrations" / "mask.json").st_mode) == 0o600
    con = open_brain(tmp_path / "brain.sqlite")
    try:
        hits = search(con, "checklist", limit=3)
        assert hits and "hushterm" not in hits[0]["body"]
        add(con, "keeper", "unrelatedkeeper stays")
    finally:
        con.close()
    doctor = cli_main(["--config", str(cfg_path), "doctor"])
    report = capsys.readouterr().out
    assert "integration-notes" in report
    assert "proof pass" in report
    assert doctor in (0, 1)
    gone = cli_main(["--config", str(cfg_path), "integrations", "disconnect", "notes", "--purge"])
    assert gone == 0
    assert "disconnected notes" in capsys.readouterr().out
    con = open_brain(tmp_path / "brain.sqlite")
    try:
        assert search(con, "checklist", limit=3) == []
        assert search(con, "unrelatedkeeper", limit=3)
    finally:
        con.close()
