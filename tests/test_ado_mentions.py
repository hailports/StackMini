"""Mention markup for a posted note. Names stay in a local file, not here."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SWEEP = Path(__file__).resolve().parents[1] / "contrib" / "stack-sweep" / "mini" / "claude-stack"
sys.path.insert(0, str(SWEEP))
from tools import ado_mentions as am


def _file(tmp_path: Path) -> Path:
    path = tmp_path / "awareness.json"
    path.write_text(json.dumps({
        "people": [
            {
                "label": "Person One",
                "guid": "guid-0001",
                "aliases": ["@Person One", "@One"],
            },
            {
                "label": "Person Two",
                "guid": "guid-0002",
                "aliases": ["@Person Two", "@Two"],
            },
        ]
    }), encoding="utf-8")
    return path


def test_missing_file_refuses(monkeypatch, tmp_path):
    monkeypatch.setenv("ADO_AWARENESS_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(ValueError, match="awareness_unresolved"):
        am.people()


def test_draft_is_first_person_and_post_keeps_the_edit(monkeypatch, tmp_path):
    monkeypatch.setenv("ADO_AWARENESS_FILE", str(_file(tmp_path)))
    draft = am.draft_plain("Checked the login")
    assert draft.startswith("i checked the login")
    assert "@Person One" in draft and "@Person Two" in draft
    posted = am.prepare_plain("leave this sentence as typed")
    assert posted.startswith("leave this sentence as typed")
    assert not posted.lower().startswith("i leave")


def test_removed_tags_come_back_as_html_mentions(monkeypatch, tmp_path):
    monkeypatch.setenv("ADO_AWARENESS_FILE", str(_file(tmp_path)))
    html_body = am.render_html("i set the header\nand then i dropped the tags")
    payload = am.comment_payload(html_body)
    assert payload["format"] == "html"
    text = payload["text"]
    assert text.startswith("<div>") and text.endswith("</div>")
    assert 'href="#"' in text
    assert 'data-vss-mention="version:2.0,guid-0001"' in text
    assert 'data-vss-mention="version:2.0,guid-0002"' in text
    assert ">@Person One</a>" in text and ">@Person Two</a>" in text
    assert "<br>" in text
    assert "&lt;" not in text


def test_empty_and_long_notes_are_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("ADO_AWARENESS_FILE", str(_file(tmp_path)))
    with pytest.raises(ValueError, match="empty_comment"):
        am.prepare_plain("fyi @Person One @Person Two")
    with pytest.raises(ValueError, match="too_long"):
        am.prepare_plain("x" * 1501)
