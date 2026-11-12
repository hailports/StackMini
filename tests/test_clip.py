"""Paiste reads the text nearest the box and stages a reply. It does not send."""

from __future__ import annotations

import json
from pathlib import Path

from hailports.draft_runner import run_round
from hailports.clip.scene import choose, copy_text, folder_hits, prepare, run_inbox, scene_from
from hailports.providers.base import Reply


class _Router:
    def __init__(self) -> None:
        self.messages = []

    def complete(self, messages, task=None, max_tokens=None):
        self.messages = messages
        return Reply("bring the milk", "local", "local", 1, 1, {})


def _scene(**extra):
    base = {
        "app": "Mail",
        "title": "thread",
        "origin": "top",
        "field_role": "AXTextArea",
        "field_text": "",
        "field_box": {"x": 10, "y": 200, "w": 400, "h": 80},
        "blocks": [
            {"text": "can you bring milk", "role": "AXStaticText", "box": {"x": 10, "y": 40, "w": 400, "h": 40}},
            {"text": "unrelated footer", "role": "AXStaticText", "box": {"x": 10, "y": 320, "w": 400, "h": 20}},
        ],
    }
    base.update(extra)
    return scene_from(base)


def test_text_above_the_box_is_the_subject_and_text_below_is_not():
    reading = choose(_scene())
    assert reading.kind == "above"
    assert "milk" in reading.subject
    assert "footer" in reading.subject


def test_selection_wins_and_a_note_uses_the_caret():
    selected = choose(_scene(selection="only this"))
    assert selected.kind == "selection"
    assert "only this" in selected.subject
    assert "milk" in selected.subject
    note = choose(scene_from({
        "app": "Obsidian",
        "field_role": "AXTextArea",
        "field_text": "older\n\nmilk is on the list\n\ncursor is here",
        "caret": len("older\n\nmilk is on the list"),
        "field_box": {"x": 0, "y": 0, "w": 100, "h": 400},
    }))
    assert "milk" in note.subject
    assert "cursor is here" in note.subject
    screen = choose(scene_from({
        "app": "Terminal",
        "field_role": "AXTextArea",
        "field_text": "alex@host ~ %\n\n╭──────╮\n│ ❯ hi │\n╰──────╯\n",
        "field_box": {"x": 0, "y": 0, "w": 800, "h": 600},
    }))
    assert screen.kind == "empty"
    output = choose(scene_from({
        "app": "Terminal",
        "field_role": "AXTextArea",
        "field_text": "can you bring the milk tomorrow\n\nalex@host ~ % ",
        "field_box": {"x": 0, "y": 0, "w": 800, "h": 600},
    }))
    assert output.kind == "above"
    assert "milk" in output.subject
    assert "alex@host" not in output.subject
    mail = choose(scene_from({
        "app": "Microsoft Outlook",
        "field_role": "AXTextArea",
        "field_text": (
            "thanks, I will check\n\n"
            "From: Dana\nSubject: accounts\n\nCan you create the account?\n\n"
            "From: Old\nSubject: meet\n\navailable at https://example.com/join\n"
        ),
        "field_box": {"x": 0, "y": 0, "w": 700, "h": 900},
    }))
    assert mail.kind == "above"
    assert "create the account" in mail.subject
    assert "example.com/join" in mail.subject
    assert "thanks" in mail.subject
    picked = choose(scene_from({
        "app": "zoom.us",
        "field_role": "AXTextArea",
        "field_text": "",
        "selection": "Ctrl+x:shortcuts",
        "field_box": {"x": 400, "y": 200, "w": 500, "h": 80},
        "blocks": [
            {"text": "Ctrl+x:shortcuts", "role": "AXButton", "box": {"x": 400, "y": 40, "w": 120, "h": 20}},
            {"text": "Can you create the account tomorrow", "role": "AXStaticText", "box": {"x": 400, "y": 80, "w": 400, "h": 40}},
        ],
    }))
    assert picked.kind == "above"
    assert "create the account" in picked.subject
    assert "shortcut" not in picked.subject.lower()
    from hailports.clip.scene import reply_ok
    assert not reply_ok("Ctrl+x:shortcuts")


def test_secure_field_is_skipped_and_memory_is_a_source(tmp_path: Path):
    locked = prepare(_scene(field_role="AXSecureTextField", selection="secret-value"))
    assert locked["ok"] is False
    assert locked["reason"] == "secret"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "pantry.md").write_text("bring oat milk on Tuesdays", encoding="utf-8")
    hits = folder_hits(vault, "bring milk")
    assert hits and "oat" in hits[0]["body"]
    prepared = prepare(_scene(), search=lambda _q: [{"title": "note", "body": "oat milk"}])
    assert prepared["sent"] is False
    assert any("oat milk" in src for src in prepared["sources"])


def test_runner_uses_the_local_model_and_does_not_send():
    router = _Router()
    result = run_round([
        {"message_id": "p1", "subject": "", "from": "", "body": "", "scene": {
            "app": "Notes",
            "title": "list",
            "origin": "top",
            "field_text": "buy milk\n\n",
            "caret": len("buy milk"),
            "field_box": {"x": 0, "y": 0, "w": 100, "h": 300},
        }},
    ], router=router)
    assert result["drafted"] == 1
    assert result["drafts"][0]["sent"] is False
    assert result["drafts"][0]["via"] == "paiste"
    assert result["drafts"][0]["text"] == "bring the milk"
    assert "On screen" in json.dumps(router.messages)
    voiced = _Router()
    run_round([
        {"message_id": "p2", "subject": "", "from": "", "body": "", "context": "hey Name -- then the point. no sign-off.", "scene": {
            "app": "Notes",
            "title": "list",
            "origin": "top",
            "field_text": "buy milk\n\n",
            "caret": len("buy milk"),
            "field_box": {"x": 0, "y": 0, "w": 100, "h": 300},
        }},
    ], router=voiced)
    assert "no sign-off" in json.dumps(voiced.messages)
    plain = prepare(scene_from({"app": "Notes", "field_text": "buy milk\n\n", "caret": len("buy milk")}))
    assert "no sign-off" not in json.dumps(plain.get("messages"))


def test_inbox_copies_only_through_the_hook(tmp_path: Path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    (inbox / "one.json").write_text(json.dumps({
        "app": "Mail",
        "origin": "top",
        "selection": "are you around",
    }), encoding="utf-8")
    copied = []
    rows = run_inbox(inbox, tmp_path / "out", lambda _m: "yes", copy=True, copy_run=copied.append)
    assert rows[0]["sent"] is False
    assert rows[0]["text"] == "yes"
    assert rows[0]["copied"] is True
    assert copied == ["yes"]
    assert copy_text("", run=copied.append) is False
    saved = json.loads(next((tmp_path / "out").glob("*.json")).read_text(encoding="utf-8"))
    assert saved["sent"] is False
