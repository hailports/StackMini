"""A stored mail decision is not opened again. Run: python3 tests/test_owa_decision_cache.py"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "mlx_stable_prefix" not in sys.modules:
    _mlx = types.ModuleType("mlx_stable_prefix")
    _mlx.with_stable_prefix = lambda text, *a, **k: text
    sys.modules["mlx_stable_prefix"] = _mlx

import ops_safari_feeds as feeds


def test_cached_decision_skips_another_read():
    assert feeds._owa_decision_cached({}) == ""
    assert feeds._owa_decision_cached({"decision": "no_reply_needed"}) == ""
    assert feeds._owa_decision_cached({
        "decision": "no_reply_needed",
        "decision_hash": "abc",
    }) == "no_reply_needed"
    assert feeds._owa_decision_cached({
        "decision": "reply",
        "decision_hash": "abc",
    }) == "reply"
    assert feeds._owa_decision_cached({
        "decision": "not_a_decision",
        "decision_hash": "abc",
    }) == ""


def test_parked_asks_do_not_reopen_a_decision():
    src = Path(feeds.__file__).read_text()
    start = src.index("def _owa_parked_ask_targets")
    block = src[start:start + 1800]
    open_at = block.index("open_actions = {")
    close = block.index("}", open_at)
    body = block[open_at:close]
    assert "decision_no_reply_needed" not in body


if __name__ == "__main__":
    test_cached_decision_skips_another_read()
    test_parked_asks_do_not_reopen_a_decision()
    print("ok")
