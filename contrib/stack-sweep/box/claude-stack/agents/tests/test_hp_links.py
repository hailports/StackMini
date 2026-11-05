import sys; sys.path.insert(0, "/home/operator/claude-stack")
from core.hp_links import ensure_final_link as e

def test_deepdive_stackmini():
    r = e(["my mac mini runs a local model", "part two"], "deep_dive")
    assert r[-1].endswith("https://hailports.com/stackmini") and len(r) == 3
def test_sidequest_arrange():
    assert "github.com/hailports/arrange" in e(["i rebuilt arrange for my ipad"], "side_quest")[-1]
def test_existing_link_cleaned():
    r = e(["a", "repo https://github.com/hailports/StackMini?utm_source=x"], "analytics")
    assert r[-1] == "repo https://github.com/hailports/StackMini" and len(r) == 2
def test_other_kinds_untouched():
    assert e(["a", "b"], "quote") == ["a", "b"]
    assert e(["a"], None) == ["a"]
def test_no_em_dash():
    for p in e(["mini"], "deep_dive") + e(["x"], "findings"): assert "\u2014" not in p
