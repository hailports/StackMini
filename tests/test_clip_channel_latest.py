"""In a busy channel the draft greets the author of the newest message from someone else and
answers that message, not an older ask or the person they @tagged."""
import json
from pathlib import Path

import hailports.clip.run as clip_run
from hailports.clip.board import thread_points
from hailports.clip.scene import addressee_first, chat_first_name, latest_chat_message, shape_voice

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "zoom_channel_latest_message.txt"
ME = ["Sam van Writer", "Sam"]


def _screen() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_last_first_names():
    assert chat_first_name("Marlowe, Jenna") == "Jenna"
    assert chat_first_name("Brennan, Theo kumar") == "Theo"
    assert chat_first_name("Okafor, Tunde Kumar") == "Tunde"
    assert chat_first_name("Dana Lee") == "Dana"
    assert chat_first_name("van Writer, Sam (You)") == "Sam"


def test_greets_latest_author_not_the_mention():
    assert addressee_first(_screen(), ME) == "Jenna"
    assert shape_voice("hey Tunde -- I will look at it.", _screen(), "Jenna").startswith("hey Jenna --")


def test_latest_message_is_the_only_focus():
    who, body = latest_chat_message(_screen(), ME)
    assert who == "Jenna"
    assert "40021187 is missing" in body and "Dale Hughes" in body
    assert "postman" not in body and "reply" not in body.lower()
    assert not any("postman" in point for point in thread_points(body))


def test_writer_message_is_skipped():
    screen = _screen() + "\nvan Writer, Sam (You) 7:40 AM\non it\n"
    assert addressee_first(screen, ME) == "Jenna"
    screen = _screen() + "\nYou 7:40 AM\non it\n"
    assert addressee_first(screen, ME) == "Jenna"


def test_draft_prompt_answers_only_the_latest(monkeypatch, capsys):
    seen = []

    def fake_complete(endpoints, messages, http=None, ssh=None):
        seen.append(messages)
        return "hey Jenna -- I will get 40021187 loaded in for Dale Hughes.", "local"

    monkeypatch.setattr(clip_run, "complete", fake_complete)
    doc = {"app": "Zoom", "origin": "screen", "title": "Zoom", "field_role": "AXTextArea", "field_text": _screen()}
    cfg = {"llm": {"base_url": "http://127.0.0.1:8080/v1", "model": "local"}, "paiste": {"me": "Sam van Writer, Sam"}}
    code = clip_run.draft_scene(doc, "http://127.0.0.1:8080/v1", "local", False, cfg)
    out = capsys.readouterr().out
    assert code == 0 and out.strip().startswith("hey Jenna --")
    prompt = json.dumps(seen[0])
    assert "Reply only to the newest message from Jenna" in prompt
    assert "hey Tunde" not in prompt
