"""Paiste tries models in order. ssh is not executed here."""

from pathlib import Path

from hailports.llm import ModelOffline
from hailports.clip.board import (
    BoardError, apply_board, discover, guide, ingest_files, model_ids, parse_hop, probe_model,
    scan_models, show_board, ssh_host_names, store_api_key,
)
from hailports.clip.run import lookup_query
from hailports.clip.scene import plain_reply
from hailports.clip.hops import complete, endpoints_from, ssh_argv
from hailports.clip.scene import folder_hits


class IO:
    def __init__(self, answers):
        self.answers = answers
        self.said = []

    def say(self, text):
        self.said.append(text)

    def confirm(self, key, title, explain, default=False):
        return self.answers.get(key, default)

    def ask(self, key, title, explain, default=""):
        return self.answers.get(key, default)


def test_order_skips_a_down_hop_and_uses_the_next():
    hops = endpoints_from({
        "paiste": {
            "order": ["first", "second"],
            "models": {
                "first": {"via": "http", "base_url": "http://127.0.0.1:18080/v1", "model": "local"},
                "second": {"via": "ssh", "ssh_host": "desk", "remote_url": "http://127.0.0.1:8080/v1", "model": "local"},
            },
        }
    })
    seen = []

    def http(endpoint, messages):
        seen.append(endpoint["name"])
        raise ModelOffline("down")

    def ssh(endpoint, messages):
        seen.append(endpoint["name"])
        return "bring the milk"

    text, name = complete(hops, [{"role": "user", "content": "are you around"}], http=http, ssh=ssh)
    assert text == "bring the milk"
    assert name == "second"
    assert seen == ["first", "second"]


def test_secret_is_not_sent_to_any_hop():
    called = []
    try:
        complete(
            [{"name": "local", "via": "http", "base_url": "http://127.0.0.1:18080/v1", "model": "local"}],
            [{"role": "user", "content": "password: hunter2"}],
            http=lambda *_a: called.append(1),
        )
    except ModelOffline as exc:
        assert "secret" in str(exc)
    else:
        raise AssertionError("secret should stop the route")
    assert called == []


def test_ssh_argv_is_not_a_shell_and_bad_host_is_dropped():
    argv = ssh_argv("desk", "http://127.0.0.1:8080/v1")
    assert argv[0] == "ssh"
    assert "desk" in argv
    assert argv[-1] == "http://127.0.0.1:8080/v1/chat/completions"
    assert "headless" not in " ".join(argv)
    hops = endpoints_from({"paiste": {"order": ["bad"], "models": {"bad": {
        "via": "ssh", "ssh_host": "bad host", "remote_url": "http://127.0.0.1:8080/v1",
    }}}})
    assert hops[0]["name"] == "default"


def test_setup_prompts_connect_mail_chat_notes_calendar_and_order():
    io = IO({
        "paiste_notes": True,
        "paiste_notes_path": "/tmp/notes",
        "paiste_mail": True,
        "paiste_mail_path": "front",
        "paiste_chat": True,
        "paiste_chat_path": "front",
        "paiste_calendar": True,
        "paiste_calendar_path": "/tmp/cal",
        "paiste_context": "/tmp/voice.md",
        "paiste_lookup": "/tmp/notes",
        "paiste_hop_0": "http://127.0.0.1:18080/v1",
        "paiste_hop_1": "ssh desk http://127.0.0.1:8080/v1",
    })
    out = guide(io, {})
    assert out["notes"] == "folder"
    assert out["notes_dir"] == "/tmp/notes"
    assert out["mail"] == "front"
    assert out["chat"] == "front"
    assert out["calendar_dir"] == "/tmp/cal"
    assert out["context_file"] == "/tmp/voice.md"
    assert out["lookup_cmd"] == "/tmp/notes"
    assert out["order"] == ["local", "ssh2"]
    assert out["models"]["ssh2"]["ssh_host"] == "desk"
    assert "does not send" in " ".join(io.said).lower() or "does not type" in " ".join(io.said)


def test_setup_offers_a_found_vault_and_model():
    io = IO({"paiste_notes": True})
    out = guide(io, {}, {
        "obsidian": "/tmp/vault",
        "model": "http://127.0.0.1:8080/v1",
        "context_file": "",
        "lookup_cmd": "",
    })
    assert out["notes_dir"] == "/tmp/vault"
    assert out["order"] == ["local"]
    assert out["models"]["local"]["base_url"] == "http://127.0.0.1:8080/v1"
    assert any("Obsidian" in line for line in io.said)


def test_discover_finds_an_obsidian_vault(tmp_path):
    vault = tmp_path / "notes"
    (vault / ".obsidian").mkdir(parents=True)
    found = discover(tmp_path)
    assert found["obsidian"] == str(vault)
    assert found["model"] == ""


def test_setup_skip_leaves_connections_off():
    out = guide(IO({}), {})
    assert out["mail"] == "off"
    assert out["chat"] == "off"
    assert out["notes"] == "off"
    assert out["calendar"] == "off"
    assert out["context_file"] == ""
    assert out["lookup_cmd"] == ""
    assert "order" not in out
    assert parse_hop("ssh bad host http://127.0.0.1:8080/v1", 0) is None


def test_board_keeps_the_model_choice_and_not_the_key(tmp_path):
    key = store_api_key(tmp_path, "sk-local-test")
    both = apply_board({}, {
        "link": "both",
        "local": "http://127.0.0.1:8080/v1",
        "local_model": "local",
        "api_url": "https://api.example.com/v1",
        "api_model": "writer",
        "key_file": key,
        "mail": "front",
        "chat": "front",
        "notes": "off",
        "calendar": "folder",
        "calendar_path": "/tmp/cal",
    })
    assert both["order"] == ["local", "api"]
    assert both["mail"] == "front"
    assert both["chat"] == "front"
    assert both["calendar_dir"] == "/tmp/cal"
    assert both["notes"] == "off"
    assert "sk-local-test" not in str(both)
    assert Path(key).read_text(encoding="utf-8").strip() == "sk-local-test"
    assert (Path(key).stat().st_mode & 0o777) == 0o600
    kept = apply_board({
        "me": "Ada",
        "context_file": "/tmp/voice.md",
        "lookup_cmd": "/tmp/lookup",
        "mail": "front",
        "order": ["desk"],
        "models": {"desk": {
            "via": "ssh", "base_url": "", "model": "desk-model",
            "ssh_host": "desk", "remote_url": "http://127.0.0.1:8080/v1",
        }},
    }, {
        "link": "local",
        "local": "ssh desk http://127.0.0.1:8080/v1",
        "local_model": "desk-model",
        "mail": "front",
        "chat": "off",
        "notes": "off",
        "calendar": "off",
    })
    assert kept["order"] == ["desk"]
    assert kept["models"]["desk"]["model"] == "desk-model"
    assert kept["me"] == "Ada"
    assert kept["context_file"] == "/tmp/voice.md"
    shown = show_board(kept, {})
    assert shown["link"] == "local"
    assert shown["local"] == "ssh desk http://127.0.0.1:8080/v1"
    assert "sk-local-test" not in str(shown)
    try:
        apply_board({}, {"link": "api", "api_url": "http://127.0.0.1:8080/v1", "api_model": "local"})
    except BoardError:
        pass
    else:
        raise AssertionError("a local server is not the API")


def test_several_models_keep_the_remote_path(tmp_path):
    existing = {
        "me": "Ada",
        "context_file": "/tmp/voice.md",
        "lookup_cmd": "/tmp/lookup",
        "order": ["brain"],
        "models": {"brain": {
            "via": "ssh", "base_url": "", "model": "desk-model",
            "ssh_host": "brain", "remote_url": "http://127.0.0.1:8080/v1",
        }},
    }
    shown = show_board(existing, {})
    assert shown["locals"][0]["host"] == "brain"
    assert shown["locals"][0]["remote"] == "http://127.0.0.1:8080/v1"
    assert shown["locals"][0]["line"] == "ssh brain http://127.0.0.1:8080/v1"
    one = store_api_key(tmp_path, "sk-one", "api-1")
    two = store_api_key(tmp_path, "sk-two", "api-2")
    saved = apply_board(existing, {
        "link": "both",
        "locals": [
            {"host": "brain", "remote": "http://127.0.0.1:8080/v1", "model": "desk-model"},
            {"url": "http://127.0.0.1:11434/v1", "model": "llama"},
        ],
        "apis": [
            {"url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "key_file": one},
            {"url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b", "key_file": two},
        ],
        "mail": "front",
        "chat": "off",
        "notes": "off",
        "calendar": "off",
    })
    assert saved["order"][0] == "brain"
    assert saved["models"]["brain"]["ssh_host"] == "brain"
    assert saved["models"]["brain"]["remote_url"] == "http://127.0.0.1:8080/v1"
    assert saved["models"]["brain"]["model"] == "desk-model"
    assert saved["me"] == "Ada"
    assert len(saved["order"]) == 4
    assert "sk-one" not in str(saved)
    assert "sk-two" not in str(saved)
    again = show_board(saved, {})
    kept = apply_board(saved, {
        "link": "both",
        "locals": again["locals"],
        "apis": [{"url": row["url"], "model": row["model"]} for row in again["apis"]],
        "mail": "front",
        "chat": "off",
        "notes": "off",
        "calendar": "off",
    })
    assert kept["models"]["brain"]["remote_url"] == "http://127.0.0.1:8080/v1"
    assert kept["order"][0] == "brain"
    assert Path(kept["models"][kept["order"][2]]["key_file"]).read_text(encoding="utf-8").strip() == "sk-one"
    blank = probe_model("ssh nope", "sk-secret-value")
    missed = probe_model("http://127.0.0.1:1/v1", "sk-secret-value")
    assert "sk-secret" not in blank
    assert "sk-secret" not in missed
    assert missed == "Not reachable."


def test_local_keeps_a_connected_premium():
    saved = apply_board({
        "order": ["cursor"],
        "models": {"cursor": {
            "via": "cursor", "bin": "/usr/bin/agent", "model": "cursor",
            "base_url": "", "ssh_host": "", "remote_url": "",
        }},
    }, {
        "link": "local",
        "local": "ssh brain http://127.0.0.1:8080/v1",
        "local_model": "desk-model",
        "mail": "off",
        "chat": "off",
        "notes": "off",
        "calendar": "off",
    })
    assert saved["order"][-1] == "cursor"
    assert saved["models"]["cursor"]["via"] == "cursor"
    assert saved["order"][0] != "cursor"


def test_login_choice_stores_the_app_and_not_a_key():
    from hailports.clip.hops import claude_argv, codex_argv, cursor_argv, cursor_prompt, only_reply
    saved = apply_board({
        "me": "Ada",
        "context_file": "/tmp/voice.md",
        "order": ["brain"],
        "models": {"brain": {
            "via": "ssh", "base_url": "", "model": "desk-model",
            "ssh_host": "brain", "remote_url": "http://127.0.0.1:8080/v1",
        }},
    }, {
        "link": "app",
        "app": "claude",
        "bin": "/usr/bin/claude",
        "mail": "front",
        "chat": "off",
        "notes": "off",
        "calendar": "off",
    })
    assert saved["order"] == ["brain", "claude"]
    assert saved["models"]["brain"]["remote_url"] == "http://127.0.0.1:8080/v1"
    assert saved["models"]["claude"]["via"] == "claude"
    assert saved["models"]["claude"]["bin"] == "/usr/bin/claude"
    assert saved["me"] == "Ada"
    assert saved["context_file"] == "/tmp/voice.md"
    shown = show_board(saved, {})
    assert shown["app"] == "claude"
    assert shown["link"] == "app"
    argv = claude_argv("/usr/bin/claude", "rules", "hi")
    assert argv[1:5] == ["-p", "--output-format", "text", "--permission-mode"]
    assert "plan" in argv
    assert "bypass" not in " ".join(argv)
    codex = " ".join(codex_argv("/usr/bin/codex", "/tmp/out"))
    assert "--sandbox read-only" in codex
    assert "bypass" not in codex
    cursor = cursor_argv("/usr/bin/agent", "hi")
    assert cursor[1:6] == ["-p", "--mode", "ask", "--trust", "--output-format"]
    assert "--yolo" not in cursor
    assert "viewing a communication" in cursor_prompt("be brief", "Sam asked")
    assert "Search all connected mail" in cursor_prompt("be brief", "Sam asked")
    assert "every question" in cursor_prompt("be brief", "Sam asked")
    assert only_reply("Ask mode, so I didn't stage it in Zoom") == ""
    assert only_reply("hey Sam -- on it") == "hey Sam -- on it"
    plain = show_board({
        "order": ["brain"],
        "models": {"brain": {
            "via": "ssh", "model": "desk-model", "ssh_host": "brain",
            "remote_url": "http://127.0.0.1:8080/v1",
        }},
    }, {})
    assert plain["app"] == ""
    assert plain["locals"][0]["host"] == "brain"


def test_guide_says_the_model_holds_the_data():
    io = IO({})
    guide(io, {})
    said = " ".join(io.said).lower()
    assert "does not retain" in said
    assert "model" in said


def test_calendar_and_mail_files_are_read(tmp_path):
    (tmp_path / "day.ics").write_text("BEGIN:VEVENT\nSUMMARY:budget review\nDTSTART:20261112T150000\nEND:VEVENT\n", encoding="utf-8")
    (tmp_path / "note.eml").write_text("Subject: budget\n\nplease review the budget\n", encoding="utf-8")
    hits = folder_hits(tmp_path, "budget review")
    blob = " ".join(hit["body"] for hit in hits)
    assert "budget review" in blob
    assert "please review the budget" in blob


def test_scan_shows_the_server_name_and_skips_wildcards():
    import json
    body = json.dumps({"data": [{"id": "desk-model"}]})
    assert model_ids(body) == ["desk-model"]
    assert ssh_host_names("Host *\nHost brain extra\nHost bad*name\n") == ["brain", "extra"]

    def read_local(url):
        if url == "http://127.0.0.1:8080/v1/models":
            return body
        return ""

    seen = []

    def read_ssh(host, url):
        seen.append((host, url))
        if host == "brain" and url == "http://127.0.0.1:8080/v1/models":
            return body
        return ""

    rows = scan_models(read_local=read_local, hosts=["brain", "other"], read_ssh=read_ssh)
    assert rows[0] == {"where": "This Mac", "host": "", "url": "http://127.0.0.1:8080/v1", "model": "desk-model"}
    assert rows[1]["host"] == "brain"
    assert rows[1]["model"] == "desk-model"
    assert all(url.startswith("http://127.0.0.1:") for _, url in seen)
    assert "other" not in [host for host, _ in seen]


def test_lookup_keeps_the_question_and_drops_a_trailing_thanks():
    blob = ("section " * 400) + " WHERE should the export button go"
    query = lookup_query(blob)
    assert "WHERE should the export button go" in query[:1600]
    assert plain_reply("the button goes on that list. thanks for checking!") == "the button goes on that list."
    assert "**" not in plain_reply("use **Pivot Orders**.")


def test_ingest_copies_notes_and_skips_secrets(tmp_path):
    note = tmp_path / "orders.md"
    note.write_text("open machine orders live on Pivot Orders in Progress", encoding="utf-8")
    secret = tmp_path / ".env"
    secret.write_text("token=nope", encoding="utf-8")
    root = tmp_path / "sources"
    info = ingest_files([str(tmp_path)], root, hook=tmp_path / "missing-hook")
    assert info["added"] == 1
    assert info["sent"] is False
    kept = apply_board(
        {"sources_dir": str(root), "order": [], "models": {}},
        {
            "link": "local",
            "local": "http://127.0.0.1:8080/v1",
            "local_model": "local",
            "mail": "off",
            "chat": "off",
            "notes": "off",
            "calendar": "off",
        },
    )
    assert kept["sources_dir"] == str(root)
