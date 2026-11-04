"""One deep note at a time, with the next item's clips gathered during the call."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from hailports.idle import (
    DEEP_CAP,
    deep_fill_one,
    items_from_brain,
    linked_clips,
    note_ended,
    run_job,
    take_one,
)
from hailports.notes import add, connect as brain_connect
from hailports.providers.base import Reply
from hailports.router import Policy, Router
from tests.test_modes import Fake


class CapFake(Fake):
    def __init__(self, name: str, text: str) -> None:
        super().__init__(name, text)
        self.max_tokens = None

    def chat(self, messages, max_tokens=512, temperature=0.2):
        self.max_tokens = max_tokens
        return super().chat(messages, max_tokens, temperature)


def _page(ref, source, body, **extra):
    page = {
        "ref": ref,
        "source": source,
        "title": extra.get("title") or ref,
        "body": body,
        "url": extra.get("url") or "",
        "ties": extra.get("ties") or [],
        "state": extra.get("state") or "",
    }
    return page


LONG = "The last note says the gate stays closed until the check passes and the owner agrees. "


def test_end_line_is_the_whole_line():
    assert note_ended("facts\nENDNOTE\n")
    assert not note_ended("ENDNOTE still has more of the note\n")
    assert not note_ended("facts ENDNOTE")
    assert take_one([{"label": "a"}, {"label": "b"}, {"label": "c"}]) == [{"label": "a"}]


def test_linked_clips_are_short_and_real():
    item = {
        "label": "card-100",
        "ref": "card-100",
        "source": "tracker",
        "text": "Parent: 900\nSee rec-200 for the record.\n" + LONG,
    }
    pages = [
        _page("card-100", "tracker", item["text"], title="Primary card that should stay out of the clips"),
        _page("comments-100", "comments", LONG + "Comment history is here.", ties=["card-100"], title="Comments"),
        _page("rec-200", "record", LONG + "Record row for the same work.", title="Record"),
        _page("card-900", "tracker", LONG + "Parent card.", title="Parent card with enough text to keep", state="open"),
        _page("pull-1", "pull", LONG, title="Open pull", url="https://github.com/example/app/pull/4", ties=["card-100"], state="open"),
        _page("pull-2", "pull", LONG, title="Other pull", url="https://github.com/other/app/pull/9", ties=["card-100"], state="open"),
        _page("legal-1", "page", "pressure schedule " + LONG, title="secret", url="/legal/file"),
    ]
    clips = linked_clips(item, pages, allowed_repos=["example/app"])
    assert clips
    assert len(clips) <= 4
    blob = "\n".join(clips)
    assert "LINK comments" in blob
    assert "LINK record" in blob
    assert "LINK parent" in blob
    assert "example/app#4" in blob
    assert "other/app" not in blob
    assert "Primary card that should stay out" not in blob
    assert "/legal/" not in blob
    for clip in clips:
        body = clip.split("\n", 1)[1]
        assert len(body) <= 340


def test_doc_uses_related_pages():
    item = {
        "label": "doc-1",
        "ref": "doc-1",
        "source": "doc",
        "text": "The pump schedule needs a longer note about pressure and flow in the west yard.",
    }
    pages = [
        _page("doc-1", "doc", item["text"], title="Doc"),
        _page("page-9", "page", "A page about the pump schedule and a longer pressure reading in the yard.", title="Yard page"),
        _page("legal-2", "page", "pump schedule longer pressure", title="nope", url="finance/secret"),
    ]
    clips = linked_clips(item, pages)
    assert len(clips) == 1
    assert clips[0].startswith("LINK page")
    assert "finance" not in clips[0]


def test_prefetch_runs_during_the_call(monkeypatch):
    started = threading.Event()
    finished = threading.Event()
    real = linked_clips

    def wrapped(item, pages, allowed_repos=None):
        if item.get("label") == "card-2":
            started.set()
            time.sleep(0.2)
            finished.set()
        return real(item, pages, allowed_repos=allowed_repos)

    monkeypatch.setattr("hailports.idle.linked_clips", wrapped)

    class Gate:
        def complete(self, messages, task=None, max_tokens=512):
            assert started.wait(2)
            assert not finished.is_set()
            self.max_tokens = max_tokens
            self.messages = messages
            return Reply("Facts from the one item.\nENDNOTE\n", "local", "m", 3, 4, {})

    gate = Gate()
    item = {"label": "card-1", "ref": "card-1", "source": "tracker", "text": LONG}
    nxt = {"label": "card-2", "ref": "card-2", "source": "tracker", "text": LONG + "other"}
    result = deep_fill_one(gate, item, [], nxt)
    assert result["ended"] is True
    assert result["stop"] == "end"
    assert result["text"] == "Facts from the one item."
    assert result["changed_user_files"] is False
    assert gate.max_tokens == DEEP_CAP
    prompt = gate.messages[1]["content"]
    assert prompt.count("ITEM ") == 1
    assert "card-1" in prompt
    assert "card-2" not in prompt
    assert "stop" not in gate.messages[0]["content"].lower() or "not the end" in gate.messages[0]["content"]


def test_three_modes_use_the_router():
    text = "One finished note.\nENDNOTE\n"
    item = {"label": "card-1", "ref": "card-1", "source": "doc", "text": LONG}
    pages = [_page("card-1", "doc", LONG, title="Card")]
    for mode in ("local", "hybrid", "api"):
        local = CapFake("local", text)
        cloud = CapFake("cloud", text)
        if mode == "api":
            router = Router(mode, None, cloud, Policy())
            used = cloud
        else:
            router = Router(mode, local, cloud, Policy())
            used = local
        result = deep_fill_one(router, item, pages)
        assert result["ended"] is True
        assert result["stop"] == "end"
        assert used.max_tokens == DEEP_CAP
        if mode == "hybrid":
            assert cloud.calls == []
        if mode == "api":
            assert local.calls == []


def test_deep_fill_stores_one_note_and_does_not_delete(tmp_path: Path):
    marker = tmp_path / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    con = brain_connect(tmp_path / "brain.sqlite")
    add(
        con,
        "Gate",
        "source: tracker\nref: card-100\n\nParent: 90\nThe pump schedule needs a longer note about pressure.\n",
    )
    add(
        con,
        "Later",
        "source: learn\nref: learn-2\n\nA learning note about the pump schedule and pressure.\n",
    )
    items, _pages = items_from_brain(con)
    assert [item["label"] for item in items] == ["card-100", "learn-2"]
    router, _local = _router_pair("Stored facts.\nENDNOTE\n")
    result = run_job("deep-fill", router=router, brain=con, thread_con=None)
    assert result["stored"] is True
    assert result["changed_user_files"] is False
    assert marker.read_text(encoding="utf-8") == "keep"
    titles = [row[0] for row in con.execute("select title from docs").fetchall()]
    assert "deep card-100" in titles
    assert "deep learn-2" not in titles
    left, _pages = items_from_brain(con)
    assert [item["label"] for item in left] == ["learn-2"]


def _router_pair(text: str):
    local = CapFake("local", text)
    return Router("local", local, None, Policy()), local
