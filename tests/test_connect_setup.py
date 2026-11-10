"""Native app setup and subscription-drafted loopback bridges."""

from __future__ import annotations

import json

from hailports.providers.base import Reply
from hailports.wizard import main as wizard_main
from hailports_setup.connect_assist import (
    apply_connection,
    assist_bridges,
    build_prompt,
    connection_status,
    default_bridges,
    detect_native,
    parse_bridge_reply,
    parse_ports,
)

KEY = "unit-test-secret"


class Cloud:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts = []

    def chat(self, messages, max_tokens=512, temperature=0.2):
        self.prompts.append(messages[0]["content"])
        return Reply(self.text, "openai", "mock", 4, 2, {})


def test_prompt_has_no_secret_or_mailbox():
    ports = {"mail": 9222, "meetings": 9223, "tracker": 9224, "cdp": 9222}
    prompt = build_prompt("darwin", ["mail"], ports)
    assert KEY not in prompt
    assert "Do not include keys" in prompt
    assert "9222" in prompt
    assert "darwin" in prompt


def test_public_url_and_secret_step_are_dropped():
    ports = {"mail": 9222, "meetings": 9223, "tracker": 9224, "cdp": 9222}
    body = json.dumps({
        "bridges": {
            "mail": {"enabled": True, "base_url": "https://evil.example/hook"},
            "meetings": {"enabled": True, "base_url": "http://127.0.0.1:1/"},
        },
        "steps": ["Open https://evil.example now", "Keep mail on 127.0.0.1."],
    })
    plan = parse_bridge_reply(body, ["mail"], ports)
    assert plan is not None
    assert plan["bridges"]["mail"]["enabled"] is True
    assert plan["bridges"]["mail"]["base_url"] == "http://127.0.0.1:9222"
    assert plan["bridges"]["meetings"]["enabled"] is False
    assert all("evil.example" not in step for step in plan["steps"])
    assert any("127.0.0.1" in step for step in plan["steps"])


def test_reply_that_echoes_the_key_is_discarded():
    cloud = Cloud('{"steps": ["' + KEY + '"]}')
    ports = {"mail": 9222, "meetings": 9223, "tracker": 9224, "cdp": 9222}
    plan = assist_bridges(cloud, os_name="linux", enabled=["mail"], ports=ports, secret=KEY)
    assert plan["source"] == "defaults"
    assert KEY not in json.dumps(plan)
    assert KEY not in cloud.prompts[0]


def test_detect_native_uses_the_first_app_that_exists(tmp_path, monkeypatch):
    mail = tmp_path / "Mail.app"
    notes = tmp_path / "Notes.app"
    mail.mkdir()
    notes.mkdir()
    monkeypatch.setattr(
        "hailports_setup.connect_assist.app_candidates",
        lambda os_name: {
            "email": [("Mail", str(mail))],
            "notes": [("Notes", str(notes))],
            "chats": [("Messages", str(tmp_path / "missing"))],
        },
    )
    found = detect_native("darwin")
    assert found["email"]["enabled"] is True
    assert found["notes"]["app"] == "Notes"
    assert found["chats"]["enabled"] is False
    assert "not found" in found["chats"]["reason"] or "Linux" not in found["chats"]["reason"]


def test_linux_native_reports_each_surface_missing():
    found = detect_native("linux", exists=lambda path: False)
    assert [found[name]["enabled"] for name in ("email", "notes", "chats")] == [False, False, False]
    assert "Linux" in found["email"]["reason"]


def test_apply_native_enables_imessage_only_on_mac():
    answers = {
        "connect": "native",
        "native_json": json.dumps({
            "email": {"enabled": True, "app": "Mail", "path": "/Apps/Mail.app"},
            "notes": {"enabled": False, "app": "", "path": ""},
            "chats": {"enabled": True, "app": "Messages", "path": "/Apps/Messages.app"},
        }),
    }
    cfg = {"modules": {"imessage": False}}
    apply_connection(cfg, answers, "darwin")
    assert cfg["integrations"]["kind"] == "native"
    assert cfg["modules"]["imessage"] is True
    assert cfg["local_bridges"]["mail"]["enabled"] is False
    other = {"modules": {"imessage": False}}
    apply_connection(other, answers, "linux")
    assert other["modules"]["imessage"] is False


def test_connection_status_rejects_a_removed_app(tmp_path):
    path = tmp_path / "Mail.app"
    path.mkdir()
    cfg = {"integrations": {"kind": "native", "email": {"enabled": True, "app": "Mail", "path": str(path)}, "notes": {"enabled": False}, "chats": {"enabled": False}}}
    assert connection_status(cfg)[0] is True
    path.rmdir()
    ok, detail = connection_status(cfg)
    assert ok is False
    assert "email" in detail


def test_parse_ports_accepts_one_number_or_four():
    assert parse_ports("9300")["mail"] == 9300
    assert parse_ports("9300")["cdp"] == 9300
    assert parse_ports("1,2,3,4") == {"mail": 1, "meetings": 2, "tracker": 3, "cdp": 4}


def test_wizard_native_records_apps(tmp_path, monkeypatch):
    monkeypatch.setenv("HAILPORTS_PROMPT", "plain")
    monkeypatch.delenv("HAILPORTS_API_KEY", raising=False)
    mail = tmp_path / "Mail.app"
    messages = tmp_path / "Messages.app"
    mail.mkdir()
    messages.mkdir()
    monkeypatch.setattr(
        "hailports.wizard.detect_native",
        lambda os_name: {
            "email": {"enabled": True, "app": "Mail", "path": str(mail)},
            "notes": {"enabled": False, "app": "", "path": "", "reason": "not found"},
            "chats": {"enabled": True, "app": "Messages", "path": str(messages)},
        },
    )
    state = tmp_path / "state"
    assert wizard_main([
        "--yes", "--mode", "local", "--connect", "native", "--allow-missing-model",
        "--state-dir", str(state), "--web-port", "19211", "--model-port", "19212", "--embed-port", "19213",
    ]) == 0
    text = (state / "config.yaml").read_text(encoding="utf-8")
    assert "kind: native" in text
    assert "app: Mail" in text
    assert "app: Messages" in text
    assert KEY not in text
    assert "enabled: false" in text


def test_wizard_bridges_call_the_subscription_and_keep_the_key_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HAILPORTS_API_KEY", KEY)
    monkeypatch.setenv("HAILPORTS_PROMPT", "plain")
    monkeypatch.setenv("HAILPORTS_NO_KEYCHAIN", "1")
    seen = {}

    def fake(cloud, **kwargs):
        prompt = build_prompt(kwargs["os_name"], kwargs["enabled"], kwargs["ports"])
        assert KEY not in prompt
        assert kwargs["secret"] == KEY
        assert cloud is not None
        seen["called"] = True
        return {
            "bridges": default_bridges(kwargs["enabled"], kwargs["ports"]),
            "steps": ["Keep the mail bridge on 127.0.0.1."],
            "source": "subscription",
        }

    monkeypatch.setattr("hailports.wizard.assist_bridges", fake)
    state = tmp_path / "state"
    assert wizard_main([
        "--yes", "--mode", "local", "--connect", "bridges", "--allow-missing-model",
        "--provider", "openai", "--base-url", "https://api.openai.com/v1", "--cloud-model", "mock",
        "--state-dir", str(state), "--web-port", "19221", "--model-port", "19222", "--embed-port", "19223",
    ]) == 0
    assert seen.get("called") is True
    text = (state / "config.yaml").read_text(encoding="utf-8")
    assert "kind: bridges" in text
    assert "assist: subscription" in text
    assert "http://127.0.0.1:9222" in text
    assert KEY not in text
    assert KEY not in (state / "setup.log").read_text(encoding="utf-8")


def test_wizard_bridges_stop_without_a_subscription_key(tmp_path, monkeypatch):
    monkeypatch.delenv("HAILPORTS_API_KEY", raising=False)
    monkeypatch.setenv("HAILPORTS_NO_KEYCHAIN", "1")
    monkeypatch.setenv("HAILPORTS_PROMPT", "plain")
    state = tmp_path / "state"
    code = wizard_main([
        "--yes", "--mode", "local", "--connect", "bridges", "--allow-missing-model",
        "--state-dir", str(state), "--web-port", "19231", "--model-port", "19232", "--embed-port", "19233",
    ])
    assert code == 2
    assert not (state / "config.yaml").exists()
