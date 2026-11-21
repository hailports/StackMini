from hailports.brain import lane_health
from hailports.brain.retrieve import exact_ids, flag_exact, rrf


def _row(i, title, kind="note"):
    return {"id": i, "title": title, "body": title, "kind": kind}


def test_exact_id_ranks_first_and_whole_token():
    assert exact_ids("ADO #551207 and 7000123") == {"551207", "7000123"}
    assert exact_ids("v1.2.38225 x") == set()
    lex = [_row(1, "summary of sprint"), _row(2, "ticket 5512071"), _row(3, "User Story 551207")]
    flag_exact("ADO 551207", lex)
    assert [r.get("exact", False) for r in lex] == [False, False, True]
    out = rrf([lex, [_row(1, "summary of sprint")]], limit=3)
    assert out[0]["id"] == 3


def test_caps_limit_summary_kind_but_not_exact():
    rows = [_row(i, f"summary {i}", kind="summary") for i in range(1, 7)] + [_row(9, "design guide")]
    out = rrf([rows], limit=5, caps={"summary": 2})
    kinds = [r["kind"] for r in out]
    assert kinds.count("summary") == 2 and "note" in kinds
    rows[3]["exact"] = True
    out = rrf([rows], limit=5, caps={"summary": 2})
    assert out[0]["id"] == 4 and [r["kind"] for r in out].count("summary") == 3


def test_summary_kinds_sit_below_content_matches_and_one_in_top3():
    rows = [_row(1, "master summary one", kind="summary"), _row(2, "master summary two", kind="summary"),
            _row(3, "unrelated monthly page"), _row(4, "omaha onsite trip notes"), _row(5, "omaha trip receipts")]
    q = "omaha onsite trip"
    out = rrf([rows], limit=5, caps={"summary": 2}, query=q, summary_kinds={"summary"})
    assert [r["id"] for r in out[:2]] == [4, 5]
    assert sum(1 for r in out[:3] if r["kind"] == "summary") <= 1
    assert [r["kind"] for r in out].count("summary") == 2
    rows[1]["exact"] = True
    out = rrf([rows], limit=5, caps={"summary": 2}, query=q, summary_kinds={"summary"})
    assert out[0]["id"] == 2
    plain = rrf([rows], limit=5, caps={"summary": 2})
    assert plain[0]["id"] == 2 and plain[1]["id"] == 1


def test_every_source_has_a_limit_and_missing_lane_is_red():
    now = 1_000_000.0
    src = {"email": (10, now - 2 * 3600), "docs": (3, now - 400 * 3600), "chat": (0, 0)}
    st = lane_health.lane_status(src, {"email": 18}, required={"notebooks": "Notebook jobs"}, now=now)
    assert "docs" in st["stale"] and "email" not in st["stale"]
    assert st["missing"] == {"notebooks": "Notebook jobs"} and st["verdict"] == "RED"
    ok = lane_health.lane_status({"email": (1, now)}, {"email": 18}, now=now)
    assert ok["verdict"] == "GREEN"


def test_probe_needs_top3():
    hits = [{"title": "a"}, {"title": "b"}, {"title": "c"}, {"title": "ticket 551207"}]
    assert not lane_health.probe_hit(hits, ["551207"])
    assert lane_health.probe_hit(hits, ["551207"], top=4)
