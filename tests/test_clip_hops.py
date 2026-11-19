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


def test_discover_finds_an_obsidian_vault(tmp_path, monkeypatch):
    vault = tmp_path / "notes"
    (vault / ".obsidian").mkdir(parents=True)
    monkeypatch.setattr("hailports.clip.board._model_up", lambda url: False)
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
    # Premium-first: the API leads, the local is the fallback.
    assert both["order"] == ["api", "local"]
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
    # Premium-first: a configured API leads; the local brain hop is the fallback behind it.
    assert saved["models"][saved["order"][0]]["base_url"] == "https://api.openai.com/v1"
    assert Path(saved["models"][saved["order"][0]]["key_file"]).read_text(encoding="utf-8").strip() == "sk-one"
    assert "brain" in saved["order"]
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
    assert kept["models"][kept["order"][0]]["base_url"] == "https://api.openai.com/v1"
    assert Path(kept["models"][kept["order"][0]]["key_file"]).read_text(encoding="utf-8").strip() == "sk-one"
    blank = probe_model("ssh nope", "sk-secret-value")
    missed = probe_model("http://127.0.0.1:1/v1", "sk-secret-value")
    assert "sk-secret" not in blank
    assert "sk-secret" not in missed
    assert missed == "Not reachable."


def test_connected_premium_leads_the_local():
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
    # A local or brain hop stays ahead of Cursor.
    assert saved["order"][0] == "ssh"
    assert saved["models"]["ssh"]["ssh_host"] == "brain"
    assert saved["models"]["cursor"]["via"] == "cursor"
    assert saved["order"][-1] == "cursor"


def test_every_hop_keeps_the_thread_past_a_long_system_prompt():
    """Codex used to join system+user and keep the first 8000 chars, which cut the thread."""
    from hailports.clip.hops import _CONTEXT_CAP, claude_argv, cursor_prompt, joined_prompt, same_context

    screen = "ONSCREEN-TOKEN out of town the whole weekend of the 21st"
    system = "S" * (_CONTEXT_CAP + 1000)
    user = ("R" * 2500) + "\nOn screen:\n" + screen
    assert len(user) <= _CONTEXT_CAP
    assert len(system) + len(user) > 8000
    kept_system, kept_user = same_context(system, user)
    assert screen in kept_user
    assert len(kept_system) == _CONTEXT_CAP and len(kept_user) <= _CONTEXT_CAP
    # Full-context drafts (thread + attachments + notes) need far more than the old 6000.
    assert _CONTEXT_CAP >= 32000
    claude = claude_argv("/usr/bin/claude", system, user)
    assert screen in claude[-1]
    assert screen in claude[claude.index("--system-prompt") + 1] or screen in claude[-1]
    packed = joined_prompt(system, user)
    assert screen in packed
    assert packed.index(screen) < packed.index("S" * 40)
    cursor = cursor_prompt(system, user)
    assert screen in cursor
    assert "viewing a communication" in cursor
    # The old slice kept the system head and dropped this tail.
    assert screen not in (system + "\n\n" + user).strip()[:8000]


def test_ssh_hop_asks_for_chat_priority_and_retries_once(monkeypatch):
    from hailports.clip import hops
    argv = hops.ssh_argv("brain", "http://127.0.0.1:8080/v1")
    assert "X-Chat-Priority: 1" in argv
    calls = []

    def run(argv, payload):
        calls.append(1)
        if len(calls) == 1:
            return b'{"choices":[{"message":{"content":""}}]}'
        return b'{"choices":[{"message":{"content":"hey there"}}]}'

    monkeypatch.setattr("time.sleep", lambda s: None)
    out = hops._ssh({"ssh_host": "brain", "base_url": "http://127.0.0.1:8080/v1", "model": "m"}, [{"role": "user", "content": "x"}], run=run)
    assert out == "hey there" and len(calls) == 2


def test_a_hop_that_cannot_see_the_message_falls_through(monkeypatch):
    import hailports.clip.hops as hops_mod
    from hailports.llm import ModelOffline

    assert hops_mod.blind_reply("hey Wes -- I can’t see Wes’s on-screen message.")
    assert hops_mod.blind_reply("hey Robin -- can you send your message again? I can’t see what you asked.")
    assert hops_mod.blind_reply("hey Casey -- could you send your message again? I'll bring the table.")
    assert not hops_mod.blind_reply("hey Wes -- no, I can't staff the Sunday booth that weekend.")

    calls = []

    def fake_codex(endpoint, messages):
        calls.append("codex")
        blob = "\n".join(str(item.get("content") or "") for item in messages)
        assert "Sunday booth" in blob
        return "hey Wes -- I can't see the on-screen message."

    def fake_http(endpoint, messages):
        calls.append("local")
        blob = "\n".join(str(item.get("content") or "") for item in messages)
        assert "Sunday booth" in blob
        return "hey Wes -- no, I can't staff the Sunday booth."

    monkeypatch.setattr(hops_mod, "_codex", fake_codex)
    monkeypatch.setattr(hops_mod, "_http", fake_http)
    text, name = hops_mod.complete(
        [
            {"name": "codex", "via": "codex", "bin": "/usr/bin/codex"},
            {"name": "local", "via": "http", "base_url": "http://127.0.0.1:9/v1", "model": "x"},
        ],
        [{"role": "user", "content": "On screen:\nSunday booth"}],
    )
    assert calls == ["codex", "local"]
    assert "can't see" not in text
    assert name == "local"
    assert "Sunday booth" in text

    monkeypatch.setattr(hops_mod, "_codex", lambda endpoint, messages: "could you send your message again?")
    try:
        hops_mod.complete(
            [{"name": "codex", "via": "codex", "bin": "/usr/bin/codex"}],
            [{"role": "user", "content": "On screen:\nSunday booth"}],
        )
        raised = False
    except ModelOffline:
        raised = True
    assert raised


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
    assert "plan" not in argv and "--setting-sources" in argv
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


def test_setup_puts_cursor_after_the_local_hop():
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
    assert saved["order"][-1] == "cursor"
    local = saved["models"][saved["order"][0]]
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
Lee, Sam B. 11:34 AM
All good as long as we use standard components with minimum custom components.
Lee, Sam B. 11:35 AM
Jordan to lead the team, and change partner portal like the store portal in the next 6 months.
"""
    latest = latest_from_other(thread)
    assert any("standard components" in line for line in latest)
    assert any("6 months" in line for line in latest)
    assert not any("directive" in line for line in latest)
    assert "1." not in plain_reply("hey Sam -- 1. the directive is no. lmk if you need anything else!")
    notes = "lwc/acmeSalesOrdersListView/acmeSalesOrdersListView.html:27: label=Search orders"
    vague = "the export button goes on that list. the export should follow the search."
    assert thin_reply(vague, notes)
    assert not thin_reply("add it on acmeSalesOrdersListView next to Search orders", notes)
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
    screen = "All good as long as we are using CRM standard components."
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


def test_premium_hops_skip_local_first_router():
    from hailports.clip.hops import claude_argv, premium_env
    argv = claude_argv("/usr/bin/claude", "rules", "hi")
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
    assert "--disable-slash-commands" in argv and "--no-chrome" in argv
    assert premium_env()["LOCAL_FIRST_ROUTER"] == "0"


def test_package_docs_and_cache_junk_are_not_notes(tmp_path):
    from hailports.clip.scene import folder_hits, junk_name, junk_text
    assert junk_name("02-README.txt") and junk_name("18-LICENSE.txt") and junk_name("CHANGELOG.md")
    assert junk_name("07-HISTORY.txt") and not junk_name("crm-standards.md")
    assert junk_text(" https://newassets.hcaptcha.com/3https://newassets.hcaptcha.com/^0https://strip" + "e.com (")
    assert not junk_text("Jordan is drafting the CRM standards doc for Pat, Chris, and Robin.")
    (tmp_path / "02-README.txt").write_text("CRM standards readme", encoding="utf-8")
    (tmp_path / "standards.md").write_text("CRM standards owner Jordan", encoding="utf-8")
    hits = folder_hits(tmp_path, "CRM standards")
    assert [hit["title"] for hit in hits] == ["standards"]


def test_meta_narration_is_trimmed_and_asked_work_is_kept():
    from hailports.clip.scene import overreach, trim_overreach
    text = (
        "Sam, I see the request for the standards doc. "
        "The text mentions presenting this to Pat, Chris, and Robin. "
        "It does not list the specific steps. "
        "I'll pull the Standard and Custom approach together with Jordan."
    )
    kept = trim_overreach(text, "Sam asked for a standards doc")
    assert "I see the request" not in kept and "does not list" not in kept and "text mentions" not in kept
    assert "I'll pull the Standard and Custom approach together with Jordan." in kept
    assert overreach(text, "")
    assert not overreach("I'll pull it together with Jordan.", "")
    assert overreach("I'll send it by Friday.", "standards doc")
    assert not overreach("I'll send it by Friday.", "can you send it by Friday")


def test_review_edge_cases_for_cleanup():
    from hailports.clip.scene import junk_name, overreach, plain_reply, trim_overreach
    assert not overreach("I'll focus on monitoring the deploy.", "")
    assert not overreach("I'll cut it by 10%.", "")
    assert not overreach("I'll send it by Friday.", "Can you have it Friday?")
    assert overreach("I'll send it tomorrow at 3pm.", "standards doc")
    assert trim_overreach("Freight is extra. It doesn't include freight.", "") == "Freight is extra. It doesn't include freight."
    assert plain_reply("Thanks Maria, I'll send the PO today.") == "Thanks Maria, I'll send the PO today."
    assert not junk_name("Q3 Security Audit.md") and not junk_name("Project history.md")
    assert junk_name("07-HISTORY.txt") and junk_name("SECURITY.md")


def test_menu_cfg_loads_saved_route_without_env(tmp_path, monkeypatch):
    import importlib.machinery
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "contrib" / "clip" / "run"
    loader = importlib.machinery.SourceFileLoader("paiste_clip_run_cfg", str(path))
    spec = importlib.util.spec_from_loader("paiste_clip_run_cfg", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    home = tmp_path / "home"
    saved = home / ".config" / "paiste" / "route.yaml"
    saved.parent.mkdir(parents=True)
    saved.write_text(
        "paiste:\n  order:\n    - local\nllm:\n  base_url: http://127.0.0.1:8080/v1\n  model: qwen\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("PAISTE_ROUTE", raising=False)
    cfg = mod._cfg("", "http://127.0.0.1:18080/v1", "local")
    assert cfg["llm"]["base_url"] == "http://127.0.0.1:8080/v1"
    assert cfg["paiste"]["order"] == ["local"]
    monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: tmp_path / "empty-home"))
    assert mod._cfg("", "http://127.0.0.1:18080/v1", "local") == {
        "llm": {"base_url": "http://127.0.0.1:18080/v1", "model": "local"},
    }
    monkeypatch.setenv("PAISTE_ROUTE", str(tmp_path / "missing.yaml"))
    assert mod._cfg("", "http://127.0.0.1:18080/v1", "local")["llm"]["model"] == "local"


def test_setup_save_orders_claude_codex_local_then_cursor():
    saved = apply_board({
        "order": ["cursor", "codex", "brain", "claude"],
        "models": {
            "claude": {"via": "claude", "bin": "/usr/bin/claude", "model": "claude", "base_url": "", "ssh_host": "", "remote_url": ""},
            "codex": {"via": "codex", "bin": "/usr/bin/codex", "model": "codex", "base_url": "", "ssh_host": "", "remote_url": ""},
            "brain": {"via": "http", "base_url": "http://127.0.0.1:8080/v1", "model": "qwen", "ssh_host": "", "remote_url": ""},
            "cursor": {"via": "cursor", "bin": "/usr/bin/agent", "model": "cursor", "base_url": "", "ssh_host": "", "remote_url": ""},
        },
    }, {
        "link": "local",
        "local": "http://127.0.0.1:8080/v1",
        "local_model": "qwen",
        "mail": "off",
        "chat": "off",
        "notes": "off",
        "calendar": "off",
    })
    assert saved["order"] == ["claude", "codex", "brain", "cursor"]


def test_setup_yes_keeps_claude_codex_and_cursor():
    current = {
        "mail": "front",
        "order": ["cursor", "local", "codex", "claude"],
        "models": {
            "claude": {"via": "claude", "bin": "/usr/bin/claude", "model": "claude", "base_url": "", "ssh_host": "", "remote_url": ""},
            "codex": {"via": "codex", "bin": "/usr/bin/codex", "model": "codex", "base_url": "", "ssh_host": "", "remote_url": ""},
            "local": {"via": "http", "base_url": "http://127.0.0.1:8080/v1", "model": "qwen", "ssh_host": "", "remote_url": ""},
            "cursor": {"via": "cursor", "bin": "/usr/bin/agent", "model": "cursor", "base_url": "", "ssh_host": "", "remote_url": ""},
        },
    }
    out = guide(IO({"paiste_hop_0": "http://127.0.0.1:8080/v1"}), current, {"model": "http://127.0.0.1:8080/v1"})
    assert out["order"] == ["claude", "codex", "local", "cursor"]
    assert out["models"]["codex"]["bin"] == "/usr/bin/codex"
    assert out["models"]["cursor"]["via"] == "cursor"


def test_not_logged_in_and_empty_models_are_not_ready(monkeypatch):
    from hailports.clip import board as board_mod

    class Done:
        def __init__(self, code, out):
            self.returncode = code
            self.stdout = out
            self.stderr = b""

    monkeypatch.setattr(board_mod, "app_bin", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(board_mod.subprocess, "run", lambda *a, **k: Done(0, b"Not logged in\n"))
    assert board_mod.app_status("codex") == "login"
    monkeypatch.setattr(board_mod.subprocess, "run", lambda *a, **k: Done(1, b"logged in\n"))
    assert board_mod.app_status("codex") == "login"
    monkeypatch.setattr(board_mod.subprocess, "run", lambda *a, **k: Done(0, b"Logged in\n"))
    assert board_mod.app_status("codex") == "signed-in"
    rows = scan_models(read_local=lambda url: '{"data":[]}', hosts=[], read_ssh=lambda host, url: "")
    assert rows == []


def test_retry_keeps_the_conversation_and_drops_an_emptied_reply(tmp_path, monkeypatch, capsys):
    import json
    from hailports.clip import run as clip_run
    from hailports.clip.hops import _split_messages

    system, user = _split_messages([
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "On screen: create the account"},
        {"role": "assistant", "content": "I will finish it by Friday."},
        {"role": "user", "content": "Rewrite that reply"},
    ])
    assert system == "rules"
    assert "create the account" in user and "Friday" in user and "Rewrite that reply" in user
    assert _split_messages([{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}])[1] == "hi"

    monkeypatch.setenv("PAISTE_DRAFTS", str(tmp_path / "drafts.jsonl"))
    monkeypatch.setattr(clip_run.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("hailports.clip.gate.allowed", lambda cfg=None, urlopen=None: True)
    monkeypatch.setattr(clip_run, "_instructions", lambda cfg: (
        "You are viewing a communication on the screen. "
        "Read the whole conversation on screen, not only the last line. "
        "Search all connected mail, all connected calendar events, and all connected notes."
    ))
    seen = []

    def fake_complete(endpoints, messages, http=None, ssh=None):
        seen.append(messages)
        if len(seen) == 1:
            return "I will finish it by Friday.", "local"
        return "I can create the account.", "local"

    monkeypatch.setattr(clip_run, "complete", fake_complete)
    doc = json.loads((Path(__file__).resolve().parents[1] / "contrib" / "clip" / "fixtures" / "mail.json").read_text())
    cfg = {"llm": {"base_url": "http://127.0.0.1:8080/v1", "model": "local"}}
    code = clip_run.draft_scene(doc, "http://127.0.0.1:8080/v1", "local", False, cfg)
    out = capsys.readouterr().out
    assert code == 0
    assert "Friday" not in out
    assert "create the account" in out.lower()
    assert any(item.get("role") == "assistant" and "Friday" in str(item.get("content")) for item in seen[1])
    assert any(item.get("role") == "user" and "account" in str(item.get("content")).lower() for item in seen[1])

    seen.clear()

    def always_bad(endpoints, messages, http=None, ssh=None):
        seen.append(messages)
        return "I will finish it by Friday.", "local"

    monkeypatch.setattr(clip_run, "complete", always_bad)
    code = clip_run.draft_scene(doc, "http://127.0.0.1:8080/v1", "local", False, cfg)
    assert code == 2
    assert capsys.readouterr().out.strip() == "empty"
    assert len(seen) == 2


def test_lookup_query_carries_the_whole_thread_ask_first():
    thread = "Subject: dealer goals " + ("earlier reply text " * 300) + " middle fact ZEBRA42 " + ("more " * 200) + " can you confirm the cap?"
    query = lookup_query(thread)
    assert query.startswith(query[:600]) and "can you confirm the cap?" in query[:600]
    assert "Subject: dealer goals" in query
    assert "ZEBRA42" in query
    assert len(query) <= 6000
