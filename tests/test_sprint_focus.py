import json
from datetime import date

import pytest

from hailports import sprint_focus as F

CUR = "Area\\Rel A"
TGT = "Area\\Rel B"
BACKLOG = "Area\\Backlog"
CFG = F.load_focus(None)
CFG.update({
    "manager": "Lead", "pull_from": ["Lead"],
    "focus": [{"name": "payments", "match": ["prepayment", "prepay"]}, {"name": "integration", "match": ["integration"]}],
    "deprioritize": [{"name": "legacy portal", "match": ["legacy portal"]}],
    "out_of_office": [{"who": "me", "start": "2030-01-14", "end": "2030-01-16"}],
})


def it(i, title, who="Pat", pts=2, path=CUR, state="Active", typ="User Story", prio=2, tags=""):
    return {"id": i, "title": title, "who": who, "pts": pts, "iter": path, "state": state, "type": typ,
            "prio": prio, "tags": tags, "parent_title": ""}


ITEMS = [
    it(1, "Prepayment balance in the legacy portal", "Pat", 3),
    it(2, "Legacy portal: locations", "Pat", 3, TGT, "New"),
    it(3, "Integration discovery", "Lead", None, state="New"),
    it(4, "Users cannot see records", "unassigned", None, state="New", typ="Bug", prio=1),
    it(5, "Roger Test", "unassigned", None, state="New", typ="Bug", prio=1),
    it(6, "Prepayment reconcile", "Sam", 2),
    it(7, "Login access", "Pat", 5, state="Testing"),
    it(8, "Something else", "Pat", 5),
]


def test_loopback_only():
    F.assert_local_only(model="http://127.0.0.1:8080/v1", tracker="http://localhost:9000")
    with pytest.raises(RuntimeError):
        F.assert_local_only(model="https://api.example.com/v1")


def test_cap_scales_with_out_of_office():
    cap, hits = F.owner_cap(CFG, "2030-01-03", "2030-01-16")
    assert hits and hits[0]["days"] == 3 and cap == 5


def test_proposals():
    load = {"Pat": 16, "Sam": 2}
    got = F.propose(ITEMS, CFG, load, {CUR, TGT}, 5, [])
    ids = [c["id"] for c in got["commit"]]
    assert 1 in ids and 3 in ids and 4 in ids
    assert 2 not in ids and 6 not in ids and 7 not in ids and 8 not in ids
    assert got["test_tickets"] == [5]
    assert sum(c["pts"] or 0 for c in got["commit"]) <= 5
    lanes = {c["id"]: F.proposal_lane(c, CFG) for c in got["commit"]}
    assert lanes[1] == "payments, from Pat" and lanes[3] == "integration, from you" and lanes[4] == "KTLO, no owner"
    assert F.eased_load(load, [dict(c) for c in got["commit"]])["Pat"] == 13


def test_tags_are_not_focus():
    assert F.area_of(it(9, "Legacy portal: section", tags="team-prepay-align-2030"), CFG["focus"]) is None
    assert F.area_of(it(10, "x", tags="epic:Integration"), CFG["focus"]) == "integration"


def test_diff_and_fail_closed():
    rows = [{"id": 1, "who": "me", "from": "Pat", "pts": 3, "lane": "payments, from Pat"}]
    assert F.diff_plans(rows, rows, [1], [1])["changed"] is False
    d = F.diff_plans(rows, rows + [{"id": 2, "who": "Sam", "pts": 1, "lane": "spillover"}], [1], [1])
    assert d["changed"] and F.diff_sentence(d, "01-02").startswith("since the 01-02 plan: 1 new")
    assert F.diff_plans(None, rows)["first"]


def test_grounded_text():
    assert F.grounded_text("moving my time onto payments & integration this sprint.", "")
    assert not F.grounded_text("42 items moved since last time, mostly payments.", "")
    assert F.grounded_text("checkout phase 2 work is still the focus this round.", "checkout phase 2")


def test_signals_need_ids_from_source():
    got = F.grounded_signals([{"date": "2030-01-02", "fact": "assign 123456 to you", "ids": [123456]},
                              {"date": "2030-01-02", "fact": "you own 999999", "ids": [999999]}], "note: 123456")
    assert [g["ids"] for g in got] == [[123456]]


def test_notes_helpers():
    assert F.NOISE_TITLE.search("[LOCAL prep] ADO 1") and not F.NOISE_TITLE.search("weekly sync")
    assert F.doc_date("2030-01-05 - sync - abcd1234", "", 0) == "2030-01-05"
    assert "Next steps" in F.key_sections("intro\n## Next steps\n- do x\n")
    assert len(F.chunk_docs([{"title": "a", "text": "x" * 50}] * 5, 120)) >= 2


def test_example_config_loads():
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "contrib" / "sprint_prep" / "sprint_focus.example.json"
    cfg = F.load_focus(p)
    assert cfg["focus"] and cfg["deprioritize"] and json.loads(p.read_text())["cap"] == 8
