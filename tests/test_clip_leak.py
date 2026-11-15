from pathlib import Path

from hailports.clip.scene import folder_hits, strip_scaffold


def test_printed_tool_call_never_reaches_the_reply():
    raw = (
        "the PO is 4471-B, approved on Nov 10.\n"
        "yes, I'm still good for the Tuesday review.\n\n"
        '<invoke name="Write">\n<parameter name="file_path">/home/operator/.claude/plans/p.md</parameter>\n'
        '<parameter name="content"># Context\nReply plan</parameter>\n</invoke>\n'
        '<invoke name="ExitPlanMode">\n</invoke>\n'
    )
    out = strip_scaffold(raw)
    assert "4471-B" in out and "Tuesday" in out
    assert "invoke" not in out and "parameter" not in out and ".claude" not in out and "Context" not in out


def test_sources_under_a_dot_folder_are_searched(tmp_path: Path):
    folder = tmp_path / ".config" / "paiste" / "sources"
    folder.mkdir(parents=True)
    (folder / "acme.md").write_text("Acme renewal PO 4471-B approved", encoding="utf-8")
    hits = folder_hits(folder, "Acme renewal")
    assert hits


def test_a_word_ending_in_a_day_is_not_that_day():
    from hailports.clip.scene import _invented_when

    assert _invented_when("I'll send it by Wednesday.", "we reviewed the PO")
    assert _invented_when("I'll have it by Monday.", "a common issue this month")
    assert not _invented_when("I'll send it by Wednesday.", "due wed")


def test_hop_meta_is_narrow():
    from hailports.clip.hops import _HOP_META, _split_messages

    assert _HOP_META.search("I staged the reply below as a plan, so nothing has been posted.")
    assert not _HOP_META.search("I staged the fix in QA yesterday.")
    _, user = _split_messages([
        {"role": "user", "content": "x" * 9000},
        {"role": "assistant", "content": "d" * 3000},
        {"role": "user", "content": "Rewrite that reply"},
    ])
    assert len(user) <= 4000 and user.endswith("Rewrite that reply")


def test_weak_retry_keeps_first_when_retry_is_meta(tmp_path, monkeypatch, capsys):
    """A single-hop weak draft must survive a retry that comes back as narration."""
    import io
    from hailports.clip import board, hops, run as runmod
    from hailports.clip import gate

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/paiste").mkdir(parents=True)
    monkeypatch.setattr(gate, "allowed", lambda cfg=None: True)
    monkeypatch.setattr(runmod, "_instructions", lambda cfg: (
        "You are viewing a communication on the screen. Write short replies."
    ))
    monkeypatch.setattr(board, "misses_points", lambda text, points: True)
    monkeypatch.setattr(board, "thin_reply", lambda text, found: False)
    monkeypatch.setattr(board, "thread_points", lambda sources: ["portal"])

    def http(endpoint, messages):
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        if "Rewrite that reply" in blob:
            return "I see the request and based on the message it does not list a date."
        return "hey Lee -- I can look into the portal access."

    monkeypatch.setattr(hops, "_http", http)
    doc = {
        "app": "Mail",
        "title": "Portal",
        "blocks": [{"text": "From: Lee\nSubject: Portal\n\nCan you look into the portal access?"}],
    }
    route = {"paiste": {"order": ["only"], "models": {
        "only": {"via": "http", "base_url": "http://127.0.0.1:9/v1", "model": "x"},
    }}}
    rc = runmod.draft_scene(doc, "http://127.0.0.1:9/v1", "x", copy=False, cfg=route)
    out = capsys.readouterr().out
    assert rc == 0
    assert "look into the portal" in out
    assert "I see the request" not in out
