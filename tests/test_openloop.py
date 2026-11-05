"""Open-card lap: skip unchanged items, keep a next step, mail once per revision."""

from hailports.openloop import (
    after_unchanged_lap,
    build_artifact,
    hard_blocked,
    item_fingerprint,
    mail_choice,
    related_hits,
    remember_fp,
    save_packet,
    select_open,
    step_cursor,
    suggest_due,
)


TOKENS = ("owner",)


def _row(wid, state, assigned, body, title="Gate check"):
    return {
        "id": wid,
        "title": title,
        "state": state,
        "assigned": assigned,
        "body": "State: {0}\nAssigned to: {1}\n{2}".format(state, assigned, body),
    }


def test_selects_assigned_mention_and_active_not_created_by():
    rows = [
        _row("1", "New", "Owner Person", "Need the gate opened."),
        _row("2", "New", "Other Person", "Created by: Owner Person\nQuiet."),
        _row("3", "Closed", "Owner Person", "Done."),
        _row("4", "New", "Other Person", "See /legal/file and Owner Person."),
        _row("5", "New", "Other Person", "Owner Person please look at the gate."),
        _row("6", "Active", "Other Person", "Last changed by: Owner Person\nWaiting on a check."),
        _row("7", "New", "Other Person", "Last changed by: Owner Person\nNo mention."),
    ]
    ids = [item["id"] for item in select_open(rows, TOKENS)]
    assert ids == ["1", "5", "6"]
    assert hard_blocked("seed phrase is here")
    assert hard_blocked("notes", "/finance/q3")


def test_unchanged_skips_then_laps_and_starts_again():
    order = ["1", "5"]
    fps = {"1": "aaa", "5": "bbb"}
    state = {"cursor": 0, "fp": {}, "n": 2, "served": 0}

    def fp_of(wid):
        return fps[wid]

    first = step_cursor(state, order, fp_of)
    assert first["action"] == "work" and first["id"] == "1" and first["skips"] == 0
    state = remember_fp(first["state"], "1", "aaa")
    assert state["served"] == 1
    second = step_cursor(state, order, fp_of)
    assert second["id"] == "5"
    state = remember_fp(second["state"], "5", "bbb")
    lap = step_cursor(state, order, fp_of)
    assert lap["action"] == "lap" and lap["skips"] == 2
    assert lap["state"]["cursor"] == 0
    assert after_unchanged_lap(False) == "other"
    assert after_unchanged_lap(True) == "suggest"
    again = step_cursor(lap["state"], order, fp_of)
    assert again["action"] == "lap"
    fps["1"] = "ccc"
    changed = step_cursor(again["state"], order, fp_of)
    assert changed["action"] == "work" and changed["id"] == "1"


def test_next_step_uses_real_hits_and_waits_for_go(tmp_path):
    item = {
        "id": "1",
        "title": "Gate check",
        "state": "New",
        "body": "State: New\nParent: 900\nWaiting on the gate.\nSee rec-200.",
        "changed": "t1",
    }
    pages = [
        {"source": "pull", "ref": "pull-4", "title": "Gate patch", "url": "https://github.com/example/app/pull/4", "body": "card-1", "ties": ["card-1"]},
        {"source": "pull", "ref": "pull-9", "title": "Other", "url": "https://github.com/other/app/pull/9", "body": "card-1", "ties": []},
        {"source": "record", "ref": "rec-200", "title": "Record row", "url": "", "body": "rec-200 for card-1", "ties": []},
        {"source": "mail", "ref": "m-1", "title": "Thread", "url": "", "body": "card-1 please check", "ties": []},
        {"source": "index", "ref": "doc-1", "title": "Run note", "url": "/legal/secret", "body": "card-1", "ties": []},
    ]
    hits = related_hits(item, pages, allowed_repos=["example/app"])
    sources = {h["source"] for h in hits}
    assert "pull" in sources
    assert "record" in sources
    assert "mail" in sources
    assert all("legal" not in (h.get("note") or "") for h in hits)
    assert all(h["ref"] != "pull-9" for h in hits)
    fp = item_fingerprint(item, hits, "cmt")
    art = build_artifact("1", item["body"], hits, "rev-1", fp, "next")
    assert "Waiting on the gate" in art["blocking"]
    assert art["prepared"]["needs_go"] is True
    assert art["prepared"]["kind"] == "pr_plan"
    assert art["found"]
    choice, stored = save_packet(tmp_path, None, art, 0)
    assert choice == "baseline"
    later = dict(art)
    later["found"] = art["found"] + [{"source": "chat", "ref": "c-9", "note": "new"}]
    later["revision"] = "rev-1"
    assert mail_choice(stored, later, 0) == "email"
    assert mail_choice(stored, art, 0) == "update"
    emailed = dict(stored)
    emailed["emailed_rev"] = "rev-1"
    assert mail_choice(emailed, later, 0) == "update"
    assert mail_choice(emailed, {**later, "revision": "rev-2"}, 10) == "cap"


def test_suggest_slot_every_three_served():
    assert suggest_due(2, True) is False
    assert suggest_due(3, True) is True
    assert suggest_due(3, False) is False
