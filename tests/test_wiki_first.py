import json
import time

from hailports import wiki_core as W
from hailports import wiki_first as F


def _wiki(tmp_path, summary="Billing portal is in UAT."):
    p = W.Page(slug="project-billing-portal", type="project", title="Billing portal",
               aliases=["billing portal", "invoices site"],
               facts=["2026-01-02 [tracker] ticket 100 opened [src tracker:t_100 2026-01-02]",
                      "UNVERIFIED (not in source db; notes): owner is probably Sam",
                      "loose line with no source"],
               summary=summary, fact_hash="a", summary_hash="a")
    t = W.Page(slug="ticket-100", type="ticket", title="100 Billing export", aliases=["100", "BP-7"],
               facts=["state Active | owner Sam [src tracker:t_100 2026-01-02]"])
    for page, sub in ((p, "projects"), (t, "tickets")):
        (tmp_path / sub).mkdir(exist_ok=True)
        (tmp_path / sub / f"{page.slug}.md").write_text(W.render(page))
    (tmp_path / "state.json").write_text(json.dumps({"compiled_at": time.time(), "pages": {
        p.slug: {"type": "project", "title": p.title, "aliases": p.aliases},
        t.slug: {"type": "ticket", "title": t.title, "aliases": t.aliases}}}))
    return tmp_path


def test_hit_serves_only_sourced_facts(tmp_path):
    out = F.wiki_prefix("where is the billing portal at", _wiki(tmp_path))
    assert out.startswith("WIKI EVIDENCE") and "[W1]" in out and "src tracker:t_100" in out
    assert "in UAT" not in out          # model summary never served
    assert "probably Sam" not in out    # UNVERIFIED never served
    assert "loose line" not in out      # unsourced never served


def test_ticket_alias_routes(tmp_path):
    out = F.wiki_prefix("status of BP-7 for standup", _wiki(tmp_path))
    assert "state Active" in out


def test_drafting_and_code_not_routed(tmp_path):
    w = _wiki(tmp_path)
    assert F.wiki_prefix("write my reply to Sam about the billing portal", w) == ""
    assert F.wiki_prefix("billing portal: return ONLY JSON with keys a, b", w) == ""
    assert F.wiki_prefix("fix this ```def f(): pass``` for the billing portal", w) == ""


def test_focus_uses_question_line(tmp_path):
    w = _wiki(tmp_path)
    long = "Context:\n[1] billing portal notes\n\nQuestion: what is the capital of France?"
    assert F.wiki_prefix(long, w) == ""


def test_budget_caps_output(tmp_path):
    assert len(F.wiki_prefix("billing portal", _wiki(tmp_path), budget=400)) < 900


def test_fail_open_paths(tmp_path, monkeypatch):
    w = _wiki(tmp_path)
    assert F.wiki_prefix("tell me a joke", w) == ""
    assert F.wiki_prefix("billing portal", w, blocked=lambda q: True) == ""
    assert F.wiki_prefix("billing portal", tmp_path / "missing") == ""
    flag = tmp_path / "off"
    flag.write_text("")
    assert F.wiki_prefix("billing portal", w, off_flag=flag) == ""
    monkeypatch.setenv("WIKI_FIRST", "force")
    assert F.wiki_prefix("billing portal", w, off_flag=flag) != ""
    monkeypatch.setenv("WIKI_FIRST", "0")
    assert F.wiki_prefix("billing portal", w) == ""


def test_stale_index_is_miss(tmp_path):
    w = _wiki(tmp_path)
    st = json.loads((w / "state.json").read_text())
    st["compiled_at"] = time.time() - 30 * 86400
    (w / "state.json").write_text(json.dumps(st))
    assert F.wiki_prefix("billing portal", w) == ""


def test_mask_applied(tmp_path):
    out = F.wiki_prefix("billing portal", _wiki(tmp_path), mask=lambda t: t.replace("ticket 100", "***"))
    assert "***" in out and "ticket 100" not in out


def test_core_helpers():
    assert W.match_pages("the billing portal", [{"slug": "a", "title": "Billing portal", "aliases": ["billing"]}])[0][0] == "a"
    assert W.diff_state({"s": "New"}, {"s": "Done"}) == ["CHANGED: s: New -> Done"]
    assert W.compile_gate(paused=True, healthy=True, throttle_level="normal", interactive=False)[0] is False
    assert W.page_budget(60, 0.5) == 30
    s, n, c = W.parse_model_page("SUMMARY: x\nNEXT: y\nCONTRADICTIONS: none")
    assert (s, n, c) == ("x", "y", [])
