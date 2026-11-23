"""Setup assist uses a configured Plugsky engine, with Cursor as fallback."""

from __future__ import annotations

import json
import os

from hailports.clip.board import cursor_setup_text, notes_setup_body
from hailports.clip.setup_assist import (
    assisted_setup_text,
    build_setup_cloud,
    plugsky_chat,
    plugsky_setup_text,
    setup_config,
    setup_facts,
    weak_plugsky_engine,
)


FACTS = setup_facts(True, True, "cursor")
NOTE = (
    "# Notes\n\nMail is connected in the AI. A click searches this folder. "
    "It does not invent a fact. paisty does not keep any data. The notes stay in this folder.\n"
)


def test_facts_are_flags_only():
    text = setup_facts(True, False, "claude")
    assert "Mail is connected" in text
    assert "Claude" in text
    assert "inbox" not in text.lower()
    assert "@" not in text


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("PAISTY_SETUP_URL", raising=False)
    monkeypatch.delenv("SETUP_MODEL_URL", raising=False)
    monkeypatch.delenv("PAISTY_SETUP_MODEL", raising=False)
    monkeypatch.delenv("SETUP_MODEL_NAME", raising=False)
    monkeypatch.delenv("PAISTY_SETUP_ENGINE", raising=False)
    cfg = setup_config()
    assert cfg["model"] == "plugsky-ultra"
    assert cfg["url"] == "https://api.plugsky.com/v1/chat/completions"
    assert cfg["engine"] == "plugsky"
    assert cfg["fallback"] == "cursor"
    assert "key" not in cfg


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("PAISTY_SETUP_MODEL", "plugsky-ultra")
    monkeypatch.setenv("PAISTY_SETUP_URL", "https://api.plugsky.com/v1/chat/completions")
    monkeypatch.setenv("PAISTY_SETUP_ENGINE", "plugsky")
    cfg = setup_config()
    assert cfg["model"] == "plugsky-ultra"
    assert cfg["fallback"] == "cursor"


def test_weak_engine_markers():
    assert weak_plugsky_engine("plugsky-lite-v1")
    assert weak_plugsky_engine("something-micro")
    assert weak_plugsky_engine("gpu-27b-host")
    assert not weak_plugsky_engine("plugsky-ultra")
    assert not weak_plugsky_engine("")


def test_plugsky_chat_logs_served_model_and_hides_key(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGSKY_API_KEY", "secret-test-key-not-real")
    monkeypatch.setenv("FREE_LLM_KEYS", str(tmp_path / "missing.env"))
    seen = {}

    def post(url, payload, headers):
        seen["url"] = url
        seen["model"] = json.loads(payload.decode("utf-8"))["model"]
        seen["auth"] = headers.get("Authorization", "")
        body = json.dumps({"choices": [{"message": {"content": NOTE}}]})
        return body, "plugsky-ultra-hosted", 200

    hit = plugsky_chat([{"role": "user", "content": FACTS}], post=post)
    assert hit["ok"] is True
    assert hit["served_model"] == "plugsky-ultra-hosted"
    assert hit["alert"] == ""
    assert seen["model"] == "plugsky-ultra"
    assert seen["url"].startswith("https://api.plugsky.com/v1")
    # the helper must send the key, but must not put it in the result
    assert "secret-test-key-not-real" not in json.dumps(hit)
    assert seen["auth"].endswith("secret-test-key-not-real")


def test_weak_served_model_sets_alert(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGSKY_API_KEY", "secret-test-key-not-real")
    monkeypatch.setenv("FREE_LLM_KEYS", str(tmp_path / "missing.env"))
    alerts = []
    monkeypatch.setattr("hailports.clip.setup_assist.alert_weak_engine", lambda served: alerts.append(served))

    def post(url, payload, headers):
        body = json.dumps({"choices": [{"message": {"content": NOTE}}]})
        return body, "plugsky-lite", 200

    hit = plugsky_chat([{"role": "user", "content": FACTS}], post=post)
    assert hit["ok"] is True
    assert hit["served_model"] == "plugsky-lite"
    assert "weak_engine" in hit["alert"]
    assert alerts == ["plugsky-lite"]


def test_assisted_falls_back_to_cursor_when_plugsky_fails():
    calls = []

    def no_plugsky(prompt):
        calls.append("plugsky")
        return ""

    def cursor(prompt):
        calls.append("cursor")
        assert "Mail is connected" in prompt
        return NOTE

    text = assisted_setup_text(FACTS, run_plugsky=no_plugsky, run_cursor=cursor)
    assert "Mail is connected" in text
    assert calls == ["plugsky", "cursor"]


def test_assisted_does_not_call_cursor_when_plugsky_works():
    calls = []

    def ok(prompt):
        calls.append("plugsky")
        return NOTE

    def cursor(prompt):
        calls.append("cursor")
        raise AssertionError("cursor should not run")

    text = assisted_setup_text(FACTS, run_plugsky=ok, run_cursor=cursor)
    assert "notes stay in this folder" in text.lower()
    assert calls == ["plugsky"]


def test_notes_setup_body_prefers_server_then_plugsky(monkeypatch):
    def post(url, payload):
        return json.dumps({"ok": True, "note": NOTE})

    called = []

    def plugsky(prompt):
        called.append("plugsky")
        return NOTE

    def cursor(prompt):
        called.append("cursor")
        return NOTE

    text = notes_setup_body(True, False, "claude", "token", "https://www.paisty.com/api/setup",
                            post=post, run_plugsky=plugsky, run_cursor=cursor)
    assert "Mail is connected" in text
    assert called == []  # server already wrote the note


def test_notes_setup_body_uses_plugsky_when_server_empty():
    def post(url, payload):
        return json.dumps({"ok": False})

    text = notes_setup_body(True, False, "claude", "token", "https://www.paisty.com/api/setup",
                            post=post, run_plugsky=lambda prompt: NOTE, run_cursor=lambda prompt: "")
    assert "Mail is connected" in text


def test_cursor_setup_text_still_drops_secrets():
    assert cursor_setup_text("x", run=lambda prompt: "password is secret and this note is long enough to pass") == ""


def test_build_setup_cloud_passthrough_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("PLUGSKY_API_KEY", raising=False)
    monkeypatch.setenv("FREE_LLM_KEYS", str(tmp_path / "missing.env"))

    class Inner:
        def chat(self, messages, max_tokens=500, temperature=0):
            return "inner"

    inner = Inner()
    assert build_setup_cloud(inner) is inner


def test_build_setup_cloud_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGSKY_API_KEY", "secret-test-key-not-real")
    monkeypatch.setenv("FREE_LLM_KEYS", str(tmp_path / "missing.env"))

    class Box:
        def __init__(self, text):
            self.text = text

    class Inner:
        def chat(self, messages, max_tokens=500, temperature=0):
            return Box("from-cursor-fallback")

    def post(url, payload, headers):
        return "{}", "", 500

    monkeypatch.setattr("hailports.clip.setup_assist.plugsky_chat", lambda *a, **k: {"ok": False, "text": "", "served_model": "", "alert": ""})
    engine = build_setup_cloud(Inner())
    reply = engine.chat([{"role": "user", "content": "hi"}])
    assert reply.text == "from-cursor-fallback"
    assert engine.last["engine"] == "fallback"


def test_key_file_is_not_required_in_repo():
    # The committed tree must not ship a key file or a literal secret name dump.
    root = os.path.dirname(os.path.dirname(__file__))
    for dirpath, dirnames, filenames in os.walk(root):
        if "/.git/" in dirpath.replace("\\", "/") + "/":
            continue
        for name in filenames:
            assert name != "keys.env"
