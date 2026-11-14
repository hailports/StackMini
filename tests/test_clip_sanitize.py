"""The draft that reaches the clipboard is clean human prose only."""

from __future__ import annotations

from hailports.clip.scene import plain_reply, strip_scaffold


LEAK = '''I'll pull up the full message history to see what's open.
Search: project timeline Q3 deadline
Read: thread_id=8891 messages
<tool_call>
{"name": "search_mail", "arguments": {"query": "timeline"}}
</tool_call>
Let me check the rest of the thread for context.
Hi Dana, the Q3 timeline still lands on the 14th. I moved the review to Tuesday so we have a buffer, and I'll send the deck before then.
Thought: the user also asked about the budget.
On the budget, we're still under by about 8 percent.'''


def test_strips_tool_calls_markers_and_meta():
    out = plain_reply(LEAK)
    low = out.lower()
    for bad in ["i'll pull up", "search:", "read:", "tool_call", '"name"', "arguments", "thought:", "let me check"]:
        assert bad not in low, f"leaked: {bad!r} in {out!r}"
    assert "Q3 timeline still lands on the 14th" in out
    assert "under by about 8 percent" in out


def test_json_only_output_is_dropped_not_pasted():
    out = plain_reply('{"name": "send", "arguments": {"to": "x"}}')
    assert out == ""


def test_plain_prose_passes_through_unchanged():
    msg = "Thanks for the update. The build is green and I merged the fix."
    out = plain_reply(msg)
    assert "build is green" in out and "merged the fix" in out


def test_fenced_tool_block_removed_but_prose_fence_kept():
    text = "Here's the reply:\n```json\n{\"tool\": \"read\"}\n```\nSee you Thursday."
    out = plain_reply(text)
    assert "tool" not in out.lower()
    assert "See you Thursday" in out


def test_legit_ill_check_with_person_survives():
    # Not a retrieval-meta sentence; must not be stripped by the narration rule.
    out = strip_scaffold("I'll check with Bob and confirm the venue by Friday.")
    assert "check with Bob" in out
