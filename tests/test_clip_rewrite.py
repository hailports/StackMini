"""Select-and-rewrite tests. No network: the model is a fake `complete`."""

from __future__ import annotations

import types

import pytest

from hailports.clip import rewrite


def test_styles_keys():
    assert set(rewrite.STYLES) == {"rewrite", "shorter", "grammar", "formal"}


def test_messages_puts_text_and_style():
    msgs = rewrite.messages("hello there", "shorter")
    assert msgs[0]["role"] == "system"
    assert rewrite.STYLES["shorter"] in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hello there"}


def test_clean_strips_fences():
    assert rewrite.clean("```\nhi\n```") == "hi"
    assert rewrite.clean("```text\nhi\n```") == "hi"


def test_clean_strips_quotes():
    assert rewrite.clean('"hi"') == "hi"
    assert rewrite.clean("\u201chi\u201d") == "hi"


def test_clean_strips_preamble():
    assert rewrite.clean("Here is the rewritten text:\nhi") == "hi"
    assert rewrite.clean("Rewritten:\nhi") == "hi"


def test_clean_keeps_normal_first_line():
    assert rewrite.clean("hi\nthere") == "hi\nthere"
    assert rewrite.clean("Note: hi") == "Note: hi"


def test_clean_keeps_real_colon_first_line():
    assert rewrite.clean("Dear team:\nThanks for the notes.") == "Dear team:\nThanks for the notes."


def test_clean_strips_sure_preamble():
    assert rewrite.clean("Sure, here's a shorter version:\nhi") == "hi"


def test_rewrite_rejects_empty():
    with pytest.raises(ValueError):
        rewrite.rewrite("   ", "rewrite", lambda m: "x")


def test_rewrite_rejects_unknown_style():
    with pytest.raises(ValueError):
        rewrite.rewrite("hi", "nope", lambda m: "x")


def test_rewrite_rejects_too_long():
    with pytest.raises(ValueError):
        rewrite.rewrite("a" * (rewrite.MAX_CHARS + 1), "rewrite", lambda m: "x")


def test_rewrite_returns_cleaned():
    out = rewrite.rewrite("hi", "rewrite", lambda m: "```\nbye\n```")
    assert out == "bye"


def test_cmd_prints_only_text_and_deletes_file(tmp_path, capsys):
    path = tmp_path / "sel.txt"
    path.write_text("hi there", encoding="utf-8")
    args = types.SimpleNamespace(text_file=str(path), style="rewrite")
    code = rewrite.cmd(args, {}, complete=lambda m: "rewritten")
    assert code == 0
    assert capsys.readouterr().out == "rewritten\n"
    assert not path.exists()


def test_cmd_empty_file_returns_2(tmp_path, capsys):
    path = tmp_path / "sel.txt"
    path.write_text("   ", encoding="utf-8")
    args = types.SimpleNamespace(text_file=str(path), style="rewrite")
    code = rewrite.cmd(args, {}, complete=lambda m: "rewritten")
    assert code == 2
    assert capsys.readouterr().err.strip() == "no text"
