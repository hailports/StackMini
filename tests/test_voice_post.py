from hailports.voice_post import VOICE_HARD_RULES, apply, post_filter


def test_post_filter_lowercases_and_keeps_email():
    got = post_filter(
        "Hey Pat,\n\nThe link is https://example.com/X and mail noreply@hailports.com. Best regards,\nAl" + "ex"
    )
    assert got.startswith("hey pat,")
    assert "https://example.com/X" in got
    assert "noreply@hailports.com" in got
    assert "best regards" not in got.lower()


def test_apply_rewrites_greeting_name():
    got = apply("hey Message,\n\nthe change is done.", sender_name="Sam")
    assert got.startswith("hey sam,")


def test_voice_hard_rules_present():
    assert "agreed" in VOICE_HARD_RULES
    assert "need to check" in VOICE_HARD_RULES
