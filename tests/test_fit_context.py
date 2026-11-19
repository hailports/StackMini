"""Fit-to-context trimmer: keeps the latest turn whole, never orphans tool results, refuses what cannot fit."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fit_context as F  # noqa: E402

CTX = 65536
IMG = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}}


def cnt(o):
    return F.estimate(o)


def _big():
    sysm = {"role": "system", "content": "You are a helpful assistant. " * 50}
    hist = []
    for i in range(40):
        hist += [{"role": "user", "content": f"question {i} " + "log line here\n" * 300},
                 {"role": "assistant", "content": None,
                  "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "grep_logs", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": f"c{i}", "content": "ERROR x\n" * 2000 + "QmFzZTY0" * 100}]
    tools = [{"type": "function", "function": {"name": n, "description": "d" * 800, "parameters": {"type": "object"}}}
             for n in ["grep_logs"] + [f"t{i}" for i in range(20)]]
    last = {"role": "user", "content": [{"type": "text", "text": "What is in the screenshot?"}, IMG]}
    return {"messages": [sysm, sysm] + hist + [last], "tools": tools, "max_tokens": 1000}, last


def test_oversized_history_fits_and_keeps_latest():
    big, last = _big()
    assert F.estimate(big) > CTX
    o, st = F.fit(big, CTX, cnt)
    assert st and not st.get("refused")
    assert F.estimate(o) + o.get("max_tokens", 0) <= CTX
    assert o["messages"][-1] == last
    assert [t["function"]["name"] for t in o["tools"]] == ["grep_logs"]
    for i, m in enumerate(o["messages"]):
        if m["role"] == "tool":
            assert o["messages"][i - 1]["role"] in ("assistant", "tool")


def test_small_request_untouched():
    small = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
    o, st = F.fit(small, CTX, cnt)
    assert st is None and o == small


def test_huge_latest_is_cut_to_fit():
    o, st = F.fit({"messages": [{"role": "user", "content": "x " * 300000}], "max_tokens": 500}, CTX, cnt)
    assert st and not st.get("refused") and F.estimate(o) <= CTX


def test_too_many_images_refused():
    _, st = F.fit({"messages": [{"role": "user", "content": [IMG] * 60}], "max_tokens": 100}, CTX, cnt)
    assert st and st.get("refused")


def test_raw_prompt_cut():
    o, st = F.fit_prompt({"prompt": "y " * 400000, "max_tokens": 100}, CTX, lambda s: len(s) // 3)
    assert st and not st.get("refused") and len(o["prompt"]) // 3 <= CTX
