import json
import time

from hailports import wiki_core as W
from hailports import wiki_first as F


def _wiki(tmp_path, summary="Billing portal is in UAT."):
    p = W.Page(slug="project-billing-portal", type="project", title="Billing portal",
               aliases=["billing portal", "invoices site"], facts=["2026-01-02 ticket 100 opened"],
               summary=summary, fact_hash="a", summary_hash="a")
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "project-billing-portal.md").write_text(W.render(p))
    (tmp_path / "state.json").write_text(json.dumps({"compiled_at": time.time(), "pages": {
        p.slug: {"type": "project", "title": p.title, "aliases": p.aliases}}}))
    return tmp_path


def test_hit_returns_compiled_layer(tmp_path):
    out = F.wiki_prefix("where is the billing portal at", _wiki(tmp_path))
    assert "WIKI PAGE [[project-billing-portal]]" in out and "in UAT" in out and "ticket 100" in out


def test_summary_only_mode_drops_facts(tmp_path):
    out = F.wiki_prefix("billing portal status", _wiki(tmp_path), facts=False)
    assert "in UAT" in out and "ticket 100" not in out


def test_summary_only_without_compiled_layer_is_miss(tmp_path):
    assert F.wiki_prefix("billing portal status", _wiki(tmp_path, summary=""), facts=False) == ""


def test_fail_open_paths(tmp_path, monkeypatch):
    w = _wiki(tmp_path)
    assert F.wiki_prefix("tell me a joke", w) == ""
    assert F.wiki_prefix("billing portal", w, blocked=lambda q: True) == ""
    assert F.wiki_prefix("billing portal", tmp_path / "missing") == ""
    flag = tmp_path / "off"
    flag.write_text("")
    assert F.wiki_prefix("billing portal", w, off_flag=flag) == ""
    monkeypatch.setenv("WIKI_FIRST", "0")
    assert F.wiki_prefix("billing portal", w) == ""


def test_stale_index_is_miss(tmp_path):
    w = _wiki(tmp_path)
    st = json.loads((w / "state.json").read_text())
    st["compiled_at"] = time.time() - 30 * 86400
    (w / "state.json").write_text(json.dumps(st))
    assert F.wiki_prefix("billing portal", w) == ""


def test_mask_applied(tmp_path):
    out = F.wiki_prefix("billing portal", _wiki(tmp_path), mask=lambda t: t.replace("UAT", "***"))
    assert "***" in out and "UAT" not in out


def test_core_helpers():
    assert W.match_pages("the billing portal", [{"slug": "a", "title": "Billing portal", "aliases": ["billing"]}])[0][0] == "a"
    assert W.diff_state({"s": "New"}, {"s": "Done"}) == ["CHANGED: s: New -> Done"]
    assert W.compile_gate(paused=True, healthy=True, throttle_level="normal", interactive=False)[0] is False
    assert W.page_budget(60, 0.5) == 30
    s, n, c = W.parse_model_page("SUMMARY: x\nNEXT: y\nCONTRADICTIONS: none")
    assert (s, n, c) == ("x", "y", [])
