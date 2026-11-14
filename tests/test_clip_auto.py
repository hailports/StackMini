"""Machine scan and one-click setup. Fakes only, no network, no real CLIs."""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace

from hailports.clip import board


def _status(states):
    return lambda name: states.get(name, "missing")


def _scan(tmp_path, states=None, env=None, models=None, apple=False, grok=""):
    rows = models or []
    return board.scan_setup(
        home=tmp_path, read_local=lambda url: "", hosts=[], read_ssh=lambda h, u: "",
        status=_status(states or {}), find_bin=lambda name: grok if name == "grok" else "",
        env=env or {}, apple_notes=apple,
    ) if not rows else _with_models(tmp_path, states, env, rows, apple)


def _with_models(tmp_path, states, env, rows, apple):
    body = json.dumps({"data": [{"id": rows[0]}]})
    return board.scan_setup(
        home=tmp_path, read_local=lambda url: body if ":11434/v1/models" in url else "", hosts=[],
        read_ssh=lambda h, u: "", status=_status(states or {}), find_bin=lambda name: "",
        env=env or {}, apple_notes=apple,
    )


def test_scan_prefers_signed_in_app_and_names_keys_only(tmp_path):
    secret = "sk-test-SECRETVALUE123"
    doc = _scan(tmp_path, states={"claude": "login", "codex": "signed-in", "cursor": "signed-in"},
                env={"OPENAI_API_KEY": secret, "GOOGLE_API_KEY": "g-xyz"}, grok="/opt/homebrew/bin/grok")
    assert secret not in json.dumps(doc) and "g-xyz" not in json.dumps(doc)
    assert [k["env"] for k in doc["keys"]] == ["OPENAI_API_KEY", "GOOGLE_API_KEY"]
    assert doc["suggest"]["ai"]["kind"] == "app" and doc["suggest"]["ai"]["name"] == "codex"
    grok = [a for a in doc["apps"] if a["name"] == "grok"][0]
    assert grok == {"name": "grok", "installed": True, "status": "installed", "drives": False}
    assert doc["suggest"]["notes"]["kind"] == "folder"


def test_suggest_falls_back_key_then_local_then_none(tmp_path):
    doc = _scan(tmp_path, env={"XAI_API_KEY": "x"}, apple=True)
    assert doc["suggest"]["ai"]["name"] == "xai" and "x" not in doc["suggest"]["ai"].get("env", "").lower().replace("xai", "")
    assert doc["suggest"]["notes"]["kind"] == "apple-notes"
    doc = _scan(tmp_path, models=["qwen3"])
    assert doc["suggest"]["ai"]["kind"] == "local" and doc["suggest"]["ai"]["name"] == "qwen3"
    doc = _scan(tmp_path)
    assert doc["suggest"]["ai"]["kind"] == "none" and doc["suggest"]["ai"]["reason"]


def test_auto_with_env_key_stores_0600_and_never_prints(tmp_path, monkeypatch, capsys):
    from hailports.clip import run as clip_run
    secret = "sk-ant-SECRETVALUE999"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setattr(board, "make_notes", lambda dest, mail=False, calendar=False, body="": (
        dest.mkdir(parents=True, exist_ok=True), {"added": 1, "path": str(dest), "need": ""})[1])
    monkeypatch.setattr(board, "server_setup_text", lambda *a, **k: "")
    monkeypatch.setattr(clip_run.Path, "home", classmethod(lambda cls: tmp_path))
    scan = _scan(tmp_path, env={"ANTHROPIC_API_KEY": secret})
    scan["mail_apps"] = ["Mail"]
    scan["chat_apps"] = []
    route = tmp_path / "cfg" / "route.yaml"
    args = SimpleNamespace(route_out=str(route))
    assert clip_run._onboard_auto(args, {"paiste": {}}, scan=scan) == 0
    out = capsys.readouterr().out
    assert secret not in out
    summary = json.loads(out)
    assert summary["ai"] == "anthropic" and summary["mail"] == ["Mail"] and summary["needs"] == []
    key = tmp_path / "cfg" / "keys" / "anthropic.key"
    assert key.read_text().strip() == secret and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(route.stat().st_mode) == 0o600
    text = route.read_text()
    assert secret not in text and "api.anthropic.com" in text and "front" in text


def test_auto_app_vault_and_needs(tmp_path, monkeypatch):
    vault = tmp_path / "Vault"
    (vault / ".obsidian").mkdir(parents=True)
    scan = _scan(tmp_path, states={"claude": "signed-in"})
    scan["suggest"]["notes"] = {"kind": "vault", "path": str(vault), "reason": ""}
    updated, summary = board.auto_setup({}, scan, tmp_path / "cfg", tmp_path / "notes", binary="/usr/bin/true")
    assert summary["ai"] == "claude" and summary["notes"] == str(vault) and summary["needs"] == []
    assert updated["models"]["claude"]["via"] == "claude" and updated["obsidian_vault"] == str(vault)

    other = tmp_path / "other-home"
    other.mkdir()
    scan = _scan(other, states={"cursor": "login"}, apple=True)
    calls = []
    updated, summary = board.auto_setup({}, scan, tmp_path / "cfg", tmp_path / "notes",
                                        setup_text=lambda mail, app: calls.append((mail, app)) or "")
    assert summary["ai"] == "" and summary["needs"][0] == "sign in to Cursor"
    assert any("Apple Notes" in n for n in summary["needs"])
    assert (tmp_path / "notes" / "Paisty.md").is_file() and [app for _, app in calls] == [""]


def test_auto_local_model(tmp_path):
    scan = _scan(tmp_path, models=["qwen3"])
    updated, summary = board.auto_setup({}, scan, tmp_path / "cfg", tmp_path / "notes")
    assert summary["ai"] == "local"
    spec = updated["models"][updated["order"][0]]
    assert spec["base_url"] == "http://127.0.0.1:11434/v1" and spec["model"] == "qwen3"


def test_versioned_api_paths_keep_their_path(monkeypatch):
    from hailports import llm
    seen = []

    class Boom(Exception):
        pass

    def fake_open(req, timeout=0):
        seen.append(req.full_url)
        raise OSError("no network in tests")

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_open)
    for base in ("https://generativelanguage.googleapis.com/v1beta/openai", "https://api.groq.com/openai/v1",
                 "http://127.0.0.1:8080", "https://api.x.ai/v1/"):
        try:
            llm.chat(base, "m", "hi")
        except Exception:
            pass
    assert seen == [
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "https://api.groq.com/openai/v1/chat/completions",
        "http://127.0.0.1:8080/v1/chat/completions",
        "https://api.x.ai/v1/chat/completions",
    ]


def test_auto_keeps_existing_notes_file(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "Paisty.md").write_text("my own notes\n", encoding="utf-8")
    scan = _scan(tmp_path, states={"claude": "signed-in"})
    calls = []
    board.auto_setup({}, scan, tmp_path / "cfg", notes, binary="/usr/bin/true",
                     setup_text=lambda mail, app: calls.append(app) or "# starter from server")
    assert (notes / "Paisty.md").read_text(encoding="utf-8") == "my own notes\n"
    assert calls == []


def test_auto_keeps_user_mail_and_chat_choice(tmp_path):
    scan = _scan(tmp_path, states={"claude": "signed-in"})
    scan["mail_apps"] = ["Mail"]
    scan["chat_apps"] = ["Slack"]
    mail_dir = tmp_path / "mailbox"
    mail_dir.mkdir()
    current = {"mail": "folder", "email_dir": str(mail_dir), "chat": "off"}
    updated, _ = board.auto_setup(current, scan, tmp_path / "cfg", tmp_path / "notes", binary="/usr/bin/true")
    assert updated["mail"] == "folder" and updated["email_dir"] == str(mail_dir)
    assert updated["chat"] == "front"
    updated, _ = board.auto_setup({}, scan, tmp_path / "cfg", tmp_path / "notes", binary="/usr/bin/true")
    assert updated["mail"] == "front" and updated["chat"] == "front"


def test_onboard_auto_board_error_prints_json_exit_2(tmp_path, monkeypatch, capsys):
    from hailports.clip import run as clip_run

    def boom(*a, **k):
        raise board.BoardError("notes folder is not usable")

    monkeypatch.setattr(board, "auto_setup", boom)
    args = SimpleNamespace(route_out=str(tmp_path / "cfg" / "route.yaml"))
    assert clip_run._onboard_auto(args, {"paiste": {}}, scan={"suggest": {}}) == 2
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc == {"ai": "", "notes": "", "mail": "", "chat": "", "needs": ["notes folder is not usable"]}
    assert "Traceback" not in captured.err

    def denied(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(board, "auto_setup", denied)
    assert clip_run._onboard_auto(args, {"paiste": {}}, scan={"suggest": {}}) == 2
    assert json.loads(capsys.readouterr().out)["needs"] == ["could not write settings"]
