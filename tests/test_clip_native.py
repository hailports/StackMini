import json
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hailports import config
from hailports.clip import hops, native, run
from hailports.draft_runner import run_round
from hailports.drafter import has_pass
from hailports.llm import ModelOffline
from hailports.providers.base import Reply


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PAISTE_ROUTE", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected external access")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(run, "_instructions", forbidden)
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        assert not path.name.lower().startswith("license")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)


@pytest.fixture
def route(tmp_path):
    path = tmp_path / ".config/paiste/route.yaml"
    path.parent.mkdir(parents=True)
    config.save_file(path, {"paiste": {
        "order": ["first", "second"],
        "models": {
            "first": {"via": "http", "base_url": "http://first.invalid/v1"},
            "second": {"via": "http", "base_url": "http://second.invalid/v1"},
        },
    }})
    return path


def row(**extra):
    return {"message_id": "m1", "subject": "Milk", "from": "Lee",
            "body": "Which milk should I bring?", "sources": ["Bring oat milk."], **extra}


def test_route_uses_hops_notes_voice_and_sanitizers(route, tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "milk.md").write_text("Oat milk is in the fridge.")
    voice = tmp_path / "voice.txt"
    voice.write_text("Keep it brief and warm.")
    (route.parent / "instructions").write_text("Use lowercase sentences.")
    cfg = config.load_file(route)
    cfg["paiste"].update(notes_dir=str(notes), context_file=str(voice), me="Pat")
    config.save_file(route, cfg)
    before = {path.name: path.read_bytes() for path in route.parent.iterdir()}
    calls = []
    lookups = []

    def lookup(cfg, source):
        lookups.append(source)
        return "The milk is on the left shelf."

    def http(endpoint, messages):
        calls.append((endpoint["name"], messages))
        if endpoint["name"] == "first":
            raise ModelOffline("offline")
        return "Search: notes\nReply: **Bring oat milk.** I'll deliver it by Friday.\n\nThanks,\nPat"

    monkeypatch.setattr(native, "_lookup_notes", lookup)
    monkeypatch.setattr(hops, "_http", http)
    result = run_round([row(sources=[], require_pass=True), row()])
    assert result["drafted"] == 1
    draft = result["drafts"][0]
    assert draft["via"] == "paiste" and draft["sent"] is False
    assert "Bring oat milk." in draft["text"] and "Friday" not in draft["text"]
    assert "Search:" not in draft["text"] and "Thanks" not in draft["text"]
    assert has_pass(draft["text"])
    assert result["skipped"][0]["skip"] == "duplicate"
    assert [name for name, _ in calls] == ["first", "second"]
    prompt = json.dumps(calls[0][1])
    for value in ("Which milk", "fridge", "left shelf", "lowercase", "brief and warm", "You are Pat"):
        assert value in prompt
    assert len(lookups) == 1
    assert before == {path.name: path.read_bytes() for path in route.parent.iterdir()}


@pytest.mark.parametrize("channel", ["mail", "chat", "account"])
def test_channel_scene(route, monkeypatch, channel):
    monkeypatch.setattr(hops, "_http", lambda *_: "Bring oat milk.")
    result = run_round([row(channel=channel, body="" if channel == "account" else "Which milk?")])
    assert result["drafts"][0]["via"] == "paiste"


def test_absent_route_is_unchanged(monkeypatch):
    monkeypatch.setattr(hops, "endpoints_from", lambda *_: pytest.fail("native route used"))
    assert native.from_config() is None
    result = run_round([row()])
    assert result == run_round([row()], cfg={"paiste": {"native": False}})
    assert result["drafts"][0]["text"] == "Bring oat milk."
    assert result["drafts"][0]["via"] == "draft"


def test_opt_out_and_explicit_overrides(route, monkeypatch):
    monkeypatch.setattr(hops, "complete", lambda *_: pytest.fail("native route used"))
    for extra in ({"scene": False}, {"scene": None}, {"scene": {}}, {"channel": "task"}):
        assert run_round([row(**extra)])["drafts"][0]["via"] == "draft"
    assert run_round([row()], cfg={"paiste": {"native": False}})["drafts"][0]["via"] == "draft"
    calls = []

    class Router:
        def complete(self, messages, **kwargs):
            calls.append(messages)
            return Reply("Bring oat milk. [1]", "test", "test", 1, 1)

    assert run_round([row()], router=Router())["drafts"][0]["via"] == "draft"
    assert "On screen" not in json.dumps(calls)
    assert native.from_config({"paiste": {"native": False}}) is None


def test_env_route_and_config_enable(route, tmp_path, monkeypatch):
    other = tmp_path / "saved.yaml"
    route.rename(other)
    monkeypatch.setenv("PAISTE_ROUTE", str(other))
    assert native.from_config().endpoints[0]["name"] == "first"
    monkeypatch.setenv("PAISTE_ROUTE", str(tmp_path / "missing.yaml"))
    assert native.from_config() is None
    monkeypatch.setattr(hops, "_http", lambda *_: "Bring oat milk.")
    assert run_round([row()], cfg={"paiste": {"enabled": True}})["drafts"][0]["via"] == "paiste"
    monkeypatch.setenv("PAISTE_ROUTE", str(other))
    cfg = config.load_file(other)
    cfg["paiste"]["native"] = False
    config.save_file(other, cfg)
    assert native.from_config() is None


def test_reply_contract_citations_and_source_deadlines(route, monkeypatch):
    from hailports.features import draft_reply

    monkeypatch.setattr(hops, "_http", lambda *_: "[1] I'll bring oat milk on Friday.")
    router = native.from_config()
    reply = draft_reply(router, "Bring milk on Friday.", ["Bring oat milk on Friday."])
    assert isinstance(reply, Reply) and not reply.raw["held"]
    assert reply.text == "[1] I'll bring oat milk on Friday."
    assert reply.raw["hop"] == "first"
    assert reply.prompt_tokens > 0 and reply.completion_tokens > 0


@pytest.mark.parametrize("output", [
    "", "   ", "The message does not list the details.", "I'll finish by Friday.",
    "password: hunter2", "https://example.invalid", "Search: notes",
    '<tool_call>{"name":"search","arguments":{"query":"milk"}}</tool_call>',
    "<thinking>I should guess.</thinking>", "Reasoning", "1. Open the settings.",
    "<tool_call>search for milk</tool_call>",
])
def test_unsafe_output_never_staged_or_retained(route, monkeypatch, output):
    monkeypatch.setattr(hops, "_http", lambda *_: output)
    router = native.from_config()
    reply = router.complete([{"role": "user", "content": "Which milk?"}], max_tokens=40)
    assert reply.text == "" and reply.raw["held"] is True
    assert set(reply.raw) == {"held", "reason", "hop"}
    result = run_round([row(require_pass=True)])
    assert result["drafted"] == 0 and result["skipped"]
    assert not result["prior"]["m1"]["draft_ok"]
    assert "body_preview" not in result["prior"]["m1"]


@pytest.mark.parametrize("extra", [
    {"body": "password: hunter2"},
    {"body": "", "fetch_body": lambda: "password: hunter2"},
    {"scene": {"app": "Mail", "field_role": "AXSecureTextField", "selection": "hidden"}},
    {"memory_search": lambda _: [{"body": "password: hunter2"}]},
])
def test_secret_gate_prevents_hops(route, monkeypatch, extra):
    monkeypatch.setattr(hops, "complete", lambda *_: pytest.fail("secret reached model"))
    assert run_round([row(**extra)])["drafted"] == 0


def test_explicit_scene_and_folders_are_preserved(route, monkeypatch):
    calls = []
    monkeypatch.setattr(hops, "_http", lambda endpoint, messages: calls.append(messages) or "Bring oat milk.")
    run_round([row(scene={"app": "Notes", "field_text": "The oat milk is cold."}, memory_folders=[])])
    prompt = json.dumps(calls[0])
    assert "milk is cold" in prompt and "Which milk" not in prompt


def test_check_and_require_pass_use_native_router(route, monkeypatch):
    calls = []

    def http(endpoint, messages):
        calls.append(messages)
        if "Grade one draft" in messages[0]["content"]:
            assert "Write only the reply" not in messages[0]["content"]
            return '{"extracted_ask":"Which milk?","answers_ask":true,"pass":true,"reason":"ok"}'
        return "Bring oat milk."

    monkeypatch.setattr(hops, "_http", http)
    result = run_round([row(check=True, require_pass=True)])
    assert result["drafted"] == 1, result
    assert has_pass(result["drafts"][0]["text"])
    assert len(calls) == 2


def test_thread_and_scheduling_gates_precede_native(route, monkeypatch):
    monkeypatch.setattr(hops, "complete", lambda *_: pytest.fail("gate bypassed"))
    assert run_round([row(channel="outlook", message_id="")])["drafted"] == 0
    result = run_round([row(body="Can we hop on a call tomorrow?")])
    assert result["drafted"] == 0 and result["skipped"][0]["skip"] == "scheduling"


@pytest.mark.parametrize("review", [False, True])
def test_unsafe_check_result_is_not_staged(route, monkeypatch, review):
    monkeypatch.setattr(hops, "_http", lambda *_: "Bring oat milk.")
    monkeypatch.setattr("hailports.contextpack.apply_check", lambda *_a, **_k: {
        "ok": not review, "text": "password: hunter2", "best_text": "password: hunter2",
        "reason": "hold:off_topic",
    })
    prior = {"m1": {"grade_fails": 2, "best_text": "password: hunter2"}} if review else {}
    result = run_round([row(check=True, require_pass=True)], prior=prior)
    assert result["drafted"] == 0 and result["skipped"]


@pytest.mark.parametrize("mode", ["native", "absent", "disabled"])
def test_accounts_cli_router_selection(route, tmp_path, monkeypatch, capsys, mode):
    from hailports import cli

    calls = []

    def http(endpoint, messages):
        calls.append(messages)
        if "one JSON object" in messages[0]["content"]:
            return json.dumps({"op": "ingest", "messages": [{"from": "Lee", "text": "Which milk?"}]})
        return "Bring oat milk. [1]"

    monkeypatch.setattr(hops, "_http", http)
    fallback = []

    class Router:
        def complete(self, messages, **kwargs):
            return Reply(http({}, messages), "test", "test", 1, 1)

    monkeypatch.setattr(cli, "_router", lambda cfg: fallback.append(cfg) or Router())
    monkeypatch.setattr("hailports.accounts.find_edge", lambda *_: "edge")
    cfg = {"accounts": {"desk": {"label": "desk"}}}
    if mode == "absent":
        route.rename(route.with_suffix(".saved"))
    if mode == "disabled":
        cfg["paiste"] = {"native": False}
    page = tmp_path / "pages.json"
    page.write_text(json.dumps({"desk": "Which milk?"}))
    args = SimpleNamespace(action="tick", page=str(page))
    assert cli._cmd_accounts(cfg, tmp_path / "cfg.yaml", args) == 0
    assert json.loads(capsys.readouterr().out)["drafted"] == 1
    assert len(calls) == 2
    assert ("On screen" in json.dumps(calls[1])) is (mode == "native")
    assert bool(fallback) is (mode != "native")
