"""Voice scorer and the self-check shaper. Synthetic text only."""

from hailports.contextpack import apply_check
from hailports.voice_score import score_reply, shape_reply


def test_rules_catch_a_stiff_promise():
    draft = "Hi Pat -- I'll follow up tomorrow. I hope this helps."
    graded = score_reply(draft, "can you see the record?", "the record is open.", "outlook")
    assert graded["checks"]["no_dash"] is False
    assert graded["checks"]["no_commit"] is False
    assert graded["checks"]["no_ai"] is False
    assert graded["checks"]["case"] is False


def test_shaper_keeps_a_short_answer():
    draft = "Hey Pat -- the record is open. I'll follow up tomorrow. I hope this helps."
    shaped = shape_reply(draft, "Pat asked if the record is open.", "outlook")
    assert "I'll" not in shaped
    assert "--" not in shaped
    assert "hope this helps" not in shaped.lower()
    assert "record is open" in shaped
    graded = score_reply(shaped, "Pat asked if the record is open.", "the record is open.", "outlook")
    assert graded["checks"]["no_dash"] is True
    assert graded["checks"]["no_commit"] is True
    assert graded["checks"]["no_ai"] is True
    assert graded["score"] > 0.5


def test_shaper_drops_a_signoff_and_a_fifth_sentence():
    draft = (
        "the record is open. the second line is here. the third line is here. "
        "the fourth line is here. the fifth line should go. best,"
    )
    shaped = shape_reply(draft, "is the record open?", "outlook")
    assert "fifth" not in shaped
    assert "best" not in shaped.lower()
    assert "record is open" in shaped


def test_gate_shapes_a_passing_draft():
    def complete(messages, max_tokens=80, json_mode=False):
        return {"ok": True, "text": '{"pass": true, "reason": "answers"}', "reason": ""}

    out = apply_check(
        "is the record open?",
        "the record is open",
        "the record is open. I'll follow up tomorrow.",
        complete=complete,
    )
    assert out["ok"] is True
    assert out["status"] == "pass"
    assert "I'll" not in out["text"]
    assert "record is open" in out["text"]
