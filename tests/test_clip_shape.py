"""Reply shape: length cap, greeting, sign-off, surface."""

from hailports.clip import compose, shape

ME = ["Jordan Reyes", "Jordan"]


def test_surface_email_and_chat():
    assert shape.surface({"app": "Microsoft Outlook"}) == "email"
    assert shape.surface({"app": "Microsoft Teams"}) == "chat"
    assert shape.surface({"app": "Zoom"}) == "chat"
    assert shape.surface({"app": "Safari", "title": "Mail - Outlook"}) == "email"


def test_word_cap_scales():
    assert shape.word_cap("email", 10) == (80, 180)
    assert shape.word_cap("email", 1000)[0] == 180
    target, cap = shape.word_cap("chat", 8)
    assert target < 80 and cap <= 70


def test_finish_greeting_and_signoff():
    out = shape.finish("hey Morgan -- yes, it is built in.\n\nlmk.\n\nthanks,\nJordan", "email", ME)
    assert out == "hey Morgan,\n\nyes, it is built in.\n\nlmk.\n\nbest,\nJordan"
    chat = shape.finish("Hi Priya — on it.\nJordan", "chat", ME)
    assert chat == "hey Priya, on it."
    assert shape.finish(out, "email", ME) == out  # idempotent


def test_long_reply_gets_retry_shorter():
    long = "hey Morgan -- " + " ".join(["word"] * 300)
    issues = shape.issues(long, "email", 180)
    assert issues and "at most 180 words" in issues[0]
    assert shape.issues("hey Morgan, short.", "email", 180) == []


def test_rules_name_the_shape():
    text = shape.rules("email", 120, 180)
    for needle in ("hey <their first name>,", "numbered list", "one or two key numbers", "never more than 180"):
        assert needle in text
    assert "No sign-off" in shape.rules("chat", 30, 70)


def test_draft_hard_cap_pass():
    long = "hey Sam -- " + " ".join(["detail"] * 260)
    short = "hey Sam -- yes, that works. fix is:\n1. drop the codes\n2. delete the accounts\nwant me to do it?"
    replies = iter([long, long, long, short])

    def fake(messages):
        return next(replies)

    doc = {"app": "Microsoft Outlook", "field_text": "From: Sam Lee <s@example.com>\nSent: Monday\nTo: me\n\ncan you fix it?"}
    text, ctx, attempts = compose.draft(doc, {"paiste": {"me": ", ".join(ME)}}, "rules", fake, code_roots=[])
    assert ctx.kind == "email"
    assert text.startswith("hey Sam,") and text.endswith("best,\nJordan")
    assert shape.words(text) <= ctx.cap
