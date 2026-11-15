"""Greeting, case, labels, and ungrounded claims in a draft."""

from hailports.clip.scene import (
    addressee_first,
    bounced_choice,
    draft_rules,
    overreach,
    relevant_notes,
    rewrite_ask,
    screen_has_choice,
    shape_voice,
)


PRIYA = "From: Priya Nair\nSubject: Thursday garden shift\n\nhey, can you cover the community garden shift this Thursday from 4 to 6?"
SAM = "From: Sam Ortega\nSubject: bookshop invoice\n\nDid the invoice PDF for the 12 chairs come through?"
LILA = (
    "From: Lila Chen\nSubject: potluck\n\n"
    "1. the side gate latch is stuck. can you bring a screwdriver?\n"
    "2. we still need folding chairs, about 8. the ones in the shed are wet.\n"
    "3. can you grab two big bottles of sparkling water?\n"
)
DANA = "Dana Ruiz 8:05 PM\nare you singing the bridge or am I?"
MINA = "Mina Patel 2:21 PM\nQuinn, can you take the second spot and reply today? they lock the list at 4."
NOAH = "Noah Grant commented:\nCan you take a look this week and say if the filter is the cause?"


def test_addressee_is_the_last_other_person():
    assert addressee_first(PRIYA, ["Quinn"]) == "Priya"
    assert addressee_first(SAM, ["Quinn"]) == "Sam"
    assert addressee_first("From: Lila Chen\n\nFrom: Quinn\n\nFrom: Lila Chen\nany update?", ["Quinn"]) == "Lila"
    assert addressee_first("Jordan Lee 9:14 AM\nswap the poster", ["Quinn"]) == "Jordan"
    assert addressee_first("Mina Patel 2:10 PM\nspots\n\nChris Adel 2:18 PM\nout\n\n" + MINA, ["Quinn"]) == "Mina"
    assert addressee_first("Riley Brooks 11:02 AM\nhey are you in for tacos Friday?", ["Quinn"]) == "Riley"
    assert addressee_first(NOAH, ["Quinn"]) == "Noah"
    assert addressee_first(DANA, ["Quinn"]) == "Dana"


def test_rules_name_the_person_and_ban_invention():
    rules = draft_rules("Priya", choice=False)
    assert 'hey Priya --' in rules
    assert "hey there" in rules
    assert "Do not invent a time" in rules
    assert "No labels" in rules
    assert "tool JSON" in rules
    assert "Pick one side" not in rules
    choice = draft_rules("Dana", choice=True)
    assert "Pick one side" in choice
    ask = rewrite_ask("Priya", choice=False)
    assert ask.startswith("Rewrite that reply")
    assert "hey Priya --" in ask
    assert "meeting" in ask
    assert "Pick one side" in rewrite_ask("Dana", choice=True)


def test_shape_fixes_greeting_case_and_labels():
    out = shape_voice("Hey there, Please send the PDF again.", SAM, "Sam")
    assert out.startswith("hey Sam --")
    assert "hey there" not in out.lower()
    assert "please" in out
    assert "PDF" in out
    labeled = shape_voice(
        "gate: I will bring a screwdriver\nchairs: they are not lined up\ndrinks: I will grab two bottles",
        LILA,
        "Lila",
    )
    assert "gate:" not in labeled and "chairs:" not in labeled and "drinks:" not in labeled
    assert labeled.startswith("hey Lila --")
    assert "screwdriver" in labeled and "bottles" in labeled
    poster = "Jordan Lee 9:14 AM\nOak Street Fair, blue background, June 6."
    kept = shape_voice("hey Jordan -- headline is Oak Street Fair, blue stays, June 6.", poster, "Jordan")
    assert kept.startswith("hey Jordan --")
    assert "Oak" in kept and "June" in kept and "blue" in kept


def test_ungrounded_meeting_and_status_are_overreach():
    assert overreach("my last meeting wraps at 4.", PRIYA)
    assert overreach("I'm free after my call.", PRIYA)
    assert not overreach("hey Priya -- yes, I can cover Thursday from 4 to 6.", PRIYA)
    assert overreach("the chairs are not lined up.", LILA)
    assert not overreach("they lock the list at 4. I can take the spot.", MINA)


def test_a_hedge_is_not_an_answer_and_a_dump_lists_every_item():
    from hailports.clip.board import open_points

    dump = """Dana Ruiz 8:05 PM
the hall key is with the front desk, not in the lockbox. I checked.
we are short one music stand. can you bring yours?
start time moved from 7 to 6:30. tell me if 6:30 is bad.
do not print the old lyric sheet, verse 2 changed.
also my mic cable is crackly. if you have a spare, please bring it.
are you singing the bridge or am I?"""
    points = " ".join(open_points(dump)).lower()
    assert "front desk" in points and "6:30" in points and "verse" in points and "cable" in points
    assert overreach("I don't have a confirmed update on the gate.", LILA)


def test_a_choice_that_is_handed_back_is_a_bounce():
    assert screen_has_choice(DANA)
    assert not screen_has_choice(PRIYA)
    assert bounced_choice("who do you want on the bridge?", DANA)
    assert not bounced_choice("I will sing the bridge.", DANA)


def test_unrelated_notes_are_dropped():
    screen = PRIYA
    assert relevant_notes("quarterly planning wraps at 4 in the board room", screen, "Priya") == ""
    kept = relevant_notes("Priya left the garden shift notes in the shed", screen, "Priya")
    assert "garden" in kept
    assert "garden" in relevant_notes("community garden shift needs a closer", screen, "Priya")


def test_draft_pins_the_rules_and_shapes_the_reply(tmp_path, monkeypatch, capsys):
    from hailports.clip import run as clip_run

    monkeypatch.setenv("PAISTE_DRAFTS", str(tmp_path / "drafts.jsonl"))
    monkeypatch.setattr(clip_run.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("hailports.clip.gate.allowed", lambda cfg=None, urlopen=None: True)
    monkeypatch.setattr(clip_run, "_instructions", lambda cfg: "You are viewing a communication on the screen. Write the reply.")
    monkeypatch.setattr(clip_run, "_lookup_notes", lambda cfg, text: "quarterly planning wraps at 4 in the board room")
    seen = []

    def fake_complete(endpoints, messages, http=None, ssh=None):
        seen.append(messages)
        return "Hey there, my last meeting wraps at 4.", "claude"

    monkeypatch.setattr(clip_run, "complete", fake_complete)
    monkeypatch.setattr(clip_run, "_complete", lambda cfg: (lambda messages: "hey Priya -- yes, I can cover Thursday from 4 to 6."))
    doc = {"app": "Mail", "title": "shift", "field_text": PRIYA}
    code = clip_run.draft_scene(doc, "http://127.0.0.1:9/v1", "x", False, {"paiste": {"me": "Quinn", "order": ["only"], "models": {
        "only": {"via": "http", "base_url": "http://127.0.0.1:9/v1", "model": "x"},
    }}})
    out = capsys.readouterr().out
    assert code == 0
    assert seen and seen[0][0]["content"].startswith("These rules win")
    assert "hey Priya --" in seen[0][0]["content"]
    assert "board room" not in seen[0][0]["content"]
    assert "hey Priya --" in out
    assert "meeting" not in out.lower()
    assert "4 to 6" in out or "Thursday" in out
