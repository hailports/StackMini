"""Edge account sessions stay headful, in the lower right, and unsent."""

from __future__ import annotations

import json

from hailports.accounts import (
    apply_adapter,
    assist_accounts,
    corner_frame,
    edge_argv,
    edge_install_argv,
    edge_install_script,
    interpret,
    parse_accounts,
    place_accounts,
    tick,
)
from hailports.providers.base import Reply
from hailports.wizard import main as wizard_main


def test_first_window_is_flush_to_the_lower_right():
    frame = corner_frame(1440, 900, 0)
    assert frame["x"] + frame["w"] == 1440
    assert frame["y"] + frame["h"] == 900
    nxt = corner_frame(1440, 900, 1)
    assert nxt["x"] < frame["x"]
    assert nxt["y"] < frame["y"]


def test_edge_argv_is_headful_and_install_is_not_a_bundled_browser():
    frame = corner_frame(1440, 900, 0)
    argv = edge_argv("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "sessions/desk", "https://notes.example/login", frame, 9340)
    assert "--headless" not in argv
    assert not any(part.startswith("--headless=") for part in argv)
    assert "--window-position=1020,580" in argv
    assert "--window-size=420,320" in argv
    assert argv[0].endswith("Microsoft Edge")
    mac = edge_install_argv("darwin")
    assert mac[:3] == ["brew", "install", "--cask"]
    assert "microsoft-edge" in mac
    win = " ".join(edge_install_argv("windows"))
    assert "Microsoft.Edge" in win
    script = edge_install_script("linux", "ubuntu")
    assert "microsoft-edge-stable" in script
    assert "packages.microsoft.com" in script
    assert len(script) < 2000


def test_model_configures_read_and_write_and_cannot_send():
    sessions = place_accounts(parse_accounts("desk=https://notes.example/login"), (1440, 900))
    reply = json.dumps({
        "accounts": {
            "desk": {"read_list": "Read the visible thread.", "write_fill": "Fill the composer.", "send": True},
        }
    })

    class Cloud:
        def chat(self, messages, max_tokens=512, temperature=0):
            assert "unit-test-secret" not in messages[0]["content"]
            return Reply(reply, "local", "local", 1, 1, {})

    updated = assist_accounts(Cloud(), sessions, secret="unit-test-secret")
    assert updated["desk"]["send"] is False
    assert updated["desk"]["headful"] is True
    assert updated["desk"]["browser"] == "edge"
    assert updated["desk"]["adapt"] == "model"
    assert "thread" in updated["desk"]["read_list"]
    forced = apply_adapter(sessions, reply)
    assert forced["desk"]["send"] is False


def test_tick_ingests_and_drops_a_send_or_a_secret():
    session = place_accounts(parse_accounts("desk=https://notes.example/login"), (1440, 900))["desk"]
    calls = []

    def complete(messages):
        calls.append(messages)
        return json.dumps({"op": "ingest", "send": False, "messages": [{"from": "Lee", "text": "bring milk"}]})

    got = interpret(session, "visible thread", complete)
    assert got["sent"] is False
    assert got["messages"][0]["text"] == "bring milk"
    refused = interpret(session, "visible thread", lambda _m: json.dumps({"op": "send", "send": True}))
    assert refused["op"] == "none"
    assert interpret(session, "BEGIN PRIVATE KEY\nabc", complete)["reason"] == "secret"
    assert len(calls) == 1
    ticked = tick({"desk": session}, {"desk": "hello there"}, lambda _m: json.dumps({"op": "draft", "text": "on it", "send": False}))
    assert ticked[0]["sent"] is False
    assert ticked[0]["text"] == "on it"


def test_setup_plans_edge_sessions_without_installing_or_opening(tmp_path, monkeypatch):
    monkeypatch.setenv("HAILPORTS_NO_KEYCHAIN", "1")
    monkeypatch.setenv("HAILPORTS_PROMPT", "plain")
    monkeypatch.delenv("HAILPORTS_API_KEY", raising=False)
    state = tmp_path / "state"
    assert wizard_main([
        "--yes", "--mode", "local", "--connect", "accounts", "--allow-missing-model",
        "--accounts", "desk=https://notes.example/login", "--screen", "1440x900",
        "--state-dir", str(state), "--web-port", "19341", "--model-port", "19342", "--embed-port", "19343",
    ]) == 0
    text = (state / "config.yaml").read_text(encoding="utf-8")
    assert "kind: accounts" in text
    assert "browser: edge" in text
    assert "headful: true" in text
    assert "bundled: false" in text
    assert "microsoft-edge" in text
    assert "x: 1020" in text
    assert "y: 580" in text
    assert "send: false" in text
    assert "https://notes.example/login" in text
    assert not (state / "edge-launched").exists()
