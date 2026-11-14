"""Paiste tries models in order. ssh is not executed here."""

from pathlib import Path

from hailports.llm import ModelOffline
from hailports.clip.board import (
    BoardError, apple_note_pages, apply_board, code_phrases, cursor_setup_text, discover, guide, ingest_files, latest_from_other, make_notes, server_setup_text,
    machine_context, misses_points, model_ids, monday_pages, open_points, parse_hop, probe_model, pull_source, thread_points,
    scan_models, show_board, ssh_host_names, store_api_key, thin_reply,
)
from hailports.clip.run import lookup_query
from hailports.clip.scene import overreach, plain_reply
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
    assert saved["order"] == ["claude", "brain"]
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


def test_setup_puts_the_premium_login_ahead_of_an_optional_local():
    saved = apply_board({}, {
        "link": "app",
        "app": "cursor",
        "bin": "/usr/bin/agent",
        "locals": [{"host": "brain", "remote": "http://127.0.0.1:8080/v1", "model": "desk-model"}],
        "mail": "off",
        "chat": "off",
        "notes": "off",
        "calendar": "off",
    })
    assert saved["order"][0] == "cursor"
    local = saved["models"][saved["order"][-1]]
    assert local["via"] == "ssh"
    assert local["ssh_host"] == "brain"
    assert local["remote_url"] == "http://127.0.0.1:8080/v1"
    assert saved["models"]["cursor"]["via"] == "cursor"


def test_setup_turns_on_mail_chat_littlebird_and_a_vault(tmp_path):
    vault = tmp_path / "Desk"
    (vault / ".obsidian").mkdir(parents=True)
    found = machine_context(tmp_path, present=["Outlook", "Zoom", "Littlebird"])
    assert found["mail"] == "front"
    assert found["mail_apps"] == ["Outlook"]
    assert found["chat"] == "front"
    assert found["chat_apps"] == ["Zoom", "Littlebird"]
    assert found["littlebird"] is True
    assert found["notes"] == "folder"
    assert found["notes_path"] == str(vault)
    empty = machine_context(tmp_path / "bare", present=[])
    assert empty["mail"] == "off"
    assert empty["chat"] == "off"
    assert empty["notes"] == "off"
    assert empty["littlebird"] is False


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


def test_the_screen_lists_every_ask_and_a_vague_reply_misses_the_code():
    screen = """
1. Add an Export button to download the open machine orders.
2. If the user searches or filters the list, the export should contain only the matching records.
Could you please clarify where exactly we need to add the Export button?
"""
    points = open_points(screen)
    assert len(points) == 3
    assert "export button" in code_phrases(screen)[0].lower()
    thread = """You Yesterday
yes but is the directive to not use ANY codex at all?
Killana, Dinesh B. 11:34 AM
All good as long as we use standard components with minimum custom components.
Killana, Dinesh B. 11:35 AM
Ramana to lead the team, and change dealer portal like SP portal in the next 6 months.
"""
    latest = latest_from_other(thread)
    assert any("standard components" in line for line in latest)
    assert any("6 months" in line for line in latest)
    assert not any("directive" in line for line in latest)
    assert "1." not in plain_reply("hey Dinesh -- 1. the directive is no. lmk if you need anything else!")
    notes = "lwc/valmontSalesOrdersListView/valmontSalesOrdersListView.html:27: label=Search orders"
    vague = "the export button goes on that list. the export should follow the search."
    assert thin_reply(vague, notes)
    assert not thin_reply("add it on valmontSalesOrdersListView next to Search orders", notes)
    assert misses_points("the button goes on that list.", points)
    screen_thread = """
Ada, Kim
1. Ship the standards note to the leads.
2. Wait for the guidebook before the next review.
Ada, Kim
The budget changes only if they are small enough to fit.
"""
    both = thread_points(screen_thread)
    assert any("standards" in line.lower() for line in both)
    assert any("budget" in line.lower() for line in both)
    assert misses_points("noted on the budget changes.", both)


def test_setup_sources_keep_notes_and_drop_a_secret(tmp_path):
    pages = apple_note_pages("NOTE Budget\nreview the open orders\nENDNOTE\nNOTE Login\npassword is hidden\nENDNOTE\n")
    assert pages == [("Budget", "review the open orders")]
    boards = monday_pages({"data": {"boards": [{"name": "Portal", "items_page": {"items": [{"name": "Export", "updates": [{"text_body": "on the orders list"}]}]}}]}})
    assert boards[0][0] == "Portal"
    assert "orders list" in boards[0][1]
    folder = tmp_path / "Zoom"
    folder.mkdir()
    (folder / "captions.vtt").write_text("the export follows the search box", encoding="utf-8")
    (folder / "auth.json").write_text("token nope", encoding="utf-8")
    info = pull_source("zoom", tmp_path / "out", folder=folder, hook=tmp_path / "missing")
    assert info["added"] == 1
    assert info["sent"] is False
    screen = "All good as long as we are using Salesforce standard components."
    assert not overreach("1. standard components are fine. we are aligned.", screen)
    assert overreach("1. click Settings and then deploy.", screen)
    made = make_notes(tmp_path / "notes", mail=True, calendar=False)
    body = (Path(made["path"]) / "Paisty.md").read_text(encoding="utf-8")
    assert "Mail is connected" in body
    assert "Calendar is connected" not in body
    note = server_setup_text(True, False, "claude", "token", "https://www.paisty.com/api/setup", post=lambda url, payload: "{\"ok\":true,\"note\":\"Mail is connected in the AI. A click searches this folder and does not invent a fact.\"}")
    assert "does not keep any data" in note
    assert cursor_setup_text("x", run=lambda prompt: "password is secret and this note is long enough to pass") == ""
    assert (Path(made["path"]) / ".obsidian").is_dir()
