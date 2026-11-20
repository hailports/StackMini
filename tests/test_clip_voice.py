"""Greeting, case, labels, and ungrounded claims in a draft."""

from hailports.clip.scene import (
    addressee_first,
    asked_objects,
    bounced_choice,
    draft_rules,
    ground_reply,
    drop_ungrounded,
    ensure_commits,
    missing_commits,
    overreach,
    relevant_notes,
    rewrite_ask,
    screen_has_choice,
    shape_voice,
    trim_overreach,
)


PRIYA = "From: Priya Nair\nSubject: Thursday garden shift\n\nhey, can you cover the community garden shift this Thursday from 4 to 6?"
SAM = "From: Sam Ortega\nSubject: bookshop invoice\n\nDid the invoice PDF for the 12 chairs come through?"
LILA = (
    "From: Lila Chen\nSubject: potluck\n\n"
    "1. the side gate latch is stuck. can you bring a screwdriver?\n"
    "2. we still need folding chairs, about 8. the ones in the shed are wet.\n"
    "3. can you grab two big bottles of sparkling water?\n"
)
LILA_LONG = (
    "From: Lila Chen\nSubject: potluck and the gate\n\n"
    "sorry this is a mess. three things before Saturday.\n\n"
    "1. the side gate latch is stuck. can you bring a screwdriver and look at it before people arrive?\n"
    "2. we still need folding chairs, about 8. the ones in the shed are wet.\n"
    "3. Priya is bringing salad, I have pasta, nobody has drinks. can you grab two big bottles of sparkling water?\n\n"
    "also ignore the old note about a bounce house, that was canceled last week. "
    "and please do not reply-all to the vendor thread, that quote was for a different park.\n\n"
    "From: Quinn\nSubject: potluck and the gate\n\n"
    "got the first note. still looking at the gate.\n\n"
    "From: Lila Chen\nSubject: potluck and the gate\n\n"
    "any update on the three things? people start showing up at 5.\n"
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


def test_a_held_back_role_and_an_unasked_remedy_are_caught():
    from hailports.clip.scene import conditional_claim, held_back, intro_narration, unasked_remedy

    hold = "do not confirm anything yet. I will write again on Friday."
    assert held_back("happy to be the backup speaker if Jordan drops.", hold)
    assert not held_back("got it. I will wait for Friday. I won't confirm.", hold)
    spare = "if you have a spare, please bring it."
    assert conditional_claim("I will bring a spare mic cable.", spare)
    assert not conditional_claim("I will bring a spare mic cable if I have one.", spare)
    assert not conditional_claim("I will bring my music stand.", spare)
    intro = "send a short intro I can forward, using only that."
    assert intro_narration("I will send a short intro you can forward.", intro)
    assert not intro_narration("the group shares one Saturday table at the Oak market.", intro)
    angry = "order 1844 showed up with plates. I want this fixed."
    assert unasked_remedy("I will get the blue mugs to you.", angry)
    assert not unasked_remedy("I will look at order 1844.", angry)
    rules = draft_rules("Robin")
    assert "do not confirm" in rules
    assert "if you have a spare" in rules
    assert "intro they can forward" in rules
    assert "Do not promise to get an item" in rules
    assert "whether a time is bad" in rules
    assert "say you will get it" in rules


def test_ground_reply_closes_the_judge_misses():
    sam = "From: Sam Ortega\n\nThanks for the Saturday reading. Did the invoice PDF for the 12 chairs come through?"
    fixed = ground_reply(
        "hey Sam -- glad the Saturday reading was a good one. if it didn't come through, I'll ask you to send it again.",
        sam,
        ["Quinn"],
    )
    assert "good one" not in fixed
    assert "can't confirm" not in fixed.lower()
    assert "coming" not in fixed.lower()
    assert "invoice" in fixed.lower()
    assert "send it again" in fixed
    assert "if " in fixed
    owen = "Owen Park 4:02 PM\nbooth Saturday?"
    assert ground_reply("hey Owen -- I don't know yet. I'll check and let you know.", owen, ["Quinn"]).endswith("yes.")
    nico = "From: Nico Alvarez\n\nthanks for dropping off the flyers yesterday. they look great."
    assert ground_reply("hey Nico -- lmk if there's anything else on either one!", nico, ["Quinn"]) == "hey Nico -- thanks for the flyers."
    noisy = nico + "\naccount: can you please see if the email in your note is possible?"
    drifted = "hey Nico -- on the email in your note, and see if it's possible. lmk if anything else comes up on either one."
    assert ground_reply(drifted, noisy, ["Quinn"]) == "hey Nico -- thanks for the flyers."
    mina = "Mina Patel\ncan you take the second spot? they lock the list at 4."
    cleaned = ground_reply("hey Mina -- I'll take the second spot, so I'm on the list before they lock it at 4.", mina, ["Quinn"])
    assert "on the list" not in cleaned
    nora = "From: Nora Blake\n\nwe still need a cooler.\n\nFrom: Quinn\n\nstill looking at the gate code.\n\npeople start showing up at 1."
    dropped = ground_reply(
        "hey Nora -- I'm still looking at the gate code. the cooler is still open. I've got 1 as when people start showing up.",
        nora,
        ["Riley Example"],
    )
    assert "still looking" not in dropped
    assert "still open" not in dropped
    assert "people start showing up at 1" in dropped
    intro = "Could you send a short intro I can forward? your group shares one Saturday table."
    forwarded = ground_reply("hey Kai -- your group shares one Saturday table. it can work around you.", intro, ["Quinn"])
    assert "your group" not in forwarded
    assert "we share" in forwarded
    assert "work around" not in forwarded
    wordy = (
        'hey Kai -- here\'s an intro you can forward: "hi. we share one Saturday table. '
        'would a 10-minute call be worth setting up? there\'s no day picked yet, so lmk if it makes sense and we can find one."'
    )
    trimmed = ground_reply(wordy, intro, ["Quinn"])
    assert "lmk" not in trimmed
    assert "find one" not in trimmed
    assert 'forward: "' in trimmed
    angry = "order 1844 showed up with plates. I want this fixed. do not tell me to wait another week."
    looked = ground_reply("hey Pete -- I will look into it now and get this fixed. I won't tell you to wait another week.", angry, ["Quinn"])
    assert "now" not in looked
    assert "get this fixed" not in looked
    assert "1844" in looked
    assert "another week" not in looked


def test_a_borrowed_status_stays_with_its_author_and_a_no_ask_is_only_thanks():
    nora = (
        "From: Nora Blake\n\nwe still need a cooler.\n\n"
        "From: Quinn\n\nstill looking at the gate code.\n\npeople start showing up at 1."
    )
    borrowed = ground_reply(
        "hey Nora -- I'm still looking at the gate code. the cooler is still open.",
        nora,
        ["Pat"],
    )
    assert "still looking" not in borrowed.lower()
    own = ground_reply(
        "hey Nora -- I'm still looking at the gate code.",
        "From: Nora Blake\n\nany update on the gate?\n\nFrom: Quinn\n\nstill looking at the gate code.",
        ["Quinn"],
    )
    assert "still looking" in own.lower()
    posters = "From: Nico Alvarez\n\nthe posters arrived. they look great."
    quiet = ground_reply("hey Nico -- glad the posters look great.", posters, ["Quinn"])
    assert quiet == "hey Nico -- thanks for the posters."
    assert "glad" not in quiet.lower()
    flyers = "From: Nico Alvarez\n\nthanks for dropping off the flyers yesterday. they look great."
    neutral = ground_reply("hey Nico -- glad the flyers look great.", flyers, ["Quinn"])
    assert neutral == "hey Nico -- thanks for the flyers."
    assert "look great" not in neutral.lower()


def test_rules_name_the_person_and_ban_invention():
    rules = draft_rules("Priya", choice=False)
    assert 'hey Priya,' in rules
    assert "hey there" in rules
    assert "Do not invent a time" in rules
    assert "still need" in rules
    assert "proper name" in rules
    assert "No labels" in rules
    assert "tool JSON" in rules
    assert "Pick one side" not in rules
    choice = draft_rules("Dana", choice=True)
    assert "Pick one side" in choice
    ask = rewrite_ask("Priya", choice=False)
    assert ask.startswith("Rewrite that reply")
    assert "hey Priya," in ask
    assert "meeting" in ask
    assert "still need" in ask
    assert "proper names" in ask
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


def test_a_need_is_a_commitment_and_names_keep_their_case():
    bad = (
        "hey Lila -- I'll bring a screwdriver and look at the latch. "
        "the chairs aren't sorted yet. I'll grab two bottles of sparkling water. "
        "priya has the salad. no bounce house, and I won't reply-all."
    )
    shaped = shape_voice(bad, LILA_LONG, "Lila")
    assert "Priya" in shaped
    assert "priya" not in shaped
    assert "Priya's" in shape_voice("priya's salad is covered.", LILA_LONG, "Lila")
    assert overreach(shaped, LILA_LONG)
    assert "chairs" in missing_commits(shaped, LILA_LONG)
    trimmed = trim_overreach(shaped, LILA_LONG)
    assert "sorted" not in trimmed.lower()
    fixed = ensure_commits(trimmed, LILA_LONG)
    assert "I'll get the chairs." in fixed
    assert "sorted" not in fixed.lower()
    assert missing_commits(fixed, LILA_LONG) == []
    good = (
        "hey Lila -- I'll bring a screwdriver. I'll get the folding chairs. "
        "I'll grab two bottles of sparkling water."
    )
    assert not overreach(good, LILA_LONG)
    assert missing_commits(good, LILA_LONG) == []
    assert [token for token, _verb in asked_objects(LILA_LONG)] == ["screwdriver", "chairs", "water"]
    truck = "Riley Brooks\nI need a yes or no by tomorrow so I can tell the truck."
    assert "truck" not in [token for token, _verb in asked_objects(truck)]
    flyer = "Nico Alvarez\nthanks for the flyers yesterday, they look great."
    leaked = (
        "hey Nico -- glad the flyers look great. "
        "I will check why Mara Solano can not access the account. "
        "I'll get the orders too."
    )
    cleaned = drop_ungrounded(leaked, flyer, "Nico")
    assert "flyers" in cleaned
    assert "Mara" not in cleaned
    assert "Solano" not in cleaned
    assert "orders" not in cleaned
    ordered = shape_voice(drop_ungrounded(leaked, flyer, "Nico"), flyer, "Nico")
    assert "flyers" in ordered
    assert "mara" not in ordered.lower()
    assert "solano" not in ordered.lower()
    assert "orders" not in ordered.lower()
    sam = shape_voice("hey Sam -- did the PDF come through? I'll ask again.", SAM, "Sam")
    assert "Did" not in sam
    assert "PDF" in sam


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
    room = "Samira Cole 1:15 PM\nshould we book the small room or the library? I need a pick today."
    assert screen_has_choice(room)
    assert not screen_has_choice(DANA)
    assert not screen_has_choice(PRIYA)
    assert bounced_choice("who do you want on the bridge?", room)
    assert not bounced_choice("I will sing the bridge.", DANA)


def test_ground_reply_stops_the_four_misses():
    sam_src = (
        "From: Sam Ortega\nSubject: bookshop invoice\n\n"
        "Thanks for the Saturday reading. Did the invoice PDF for the 12 chairs come through? "
        "If not I can send it again.\n"
    )
    sam = ground_reply(
        "hey Sam -- thanks for coming to the Saturday reading. "
        "I can't confirm yet that the invoice PDF for the 12 chairs came through, so please send it again.",
        sam_src,
        ["Quinn"],
    )
    assert "coming" not in sam.lower()
    assert "can't confirm" not in sam.lower()
    assert "don't know yet" in sam
    assert "12 chairs" in sam
    assert "send it again" in sam

    june = "From: June Hale\nSubject: coffee\n\nwant to grab coffee sometime Thursday? I do not have a time yet.\n"
    coffee = ground_reply(
        "hey June -- yes, I'd like to grab coffee sometime Thursday. send me a time once you have one and I'll make it work.",
        june,
        ["Quinn"],
    )
    assert "make it work" not in coffee.lower()
    assert "Thursday" in coffee
    assert "time" in coffee.lower()

    dana_src = (
        "Dana Ruiz 8:05 PM\n"
        "the hall key is with the front desk.\n"
        "can you bring your music stand?\n"
        "start time moved from 7 to 6:30. tell me if 6:30 is bad.\n"
        "are you singing the bridge or am I?\n"
    )
    dana = ground_reply(
        "hey Dana -- I'll grab the hall key from the front desk, not the lockbox. "
        "I'll bring my music stand. no, 6:30 is not bad for me. you're singing the bridge.",
        dana_src,
        ["Quinn"],
    )
    assert "not bad" not in dana.lower()
    assert "sing the bridge" not in dana.lower()
    assert "grab the hall key" not in dana.lower()
    assert "front desk" in dana
    assert "don't know yet" in dana
    assert "6:30" in dana
    assert "bridge" in dana

    pete = "From: Pete Lang\n\norder 1844 was supposed to include the blue mugs and it showed up with plates. I want this fixed."
    angry = ground_reply(
        "hey Pete -- I will look at order 1844 to look at order 1844.",
        pete,
        ["Quinn"],
    )
    assert angry.lower().count("look at order 1844") == 1
    assert "get it fixed" not in angry.lower()
    week = ground_reply(
        "hey Pete -- I will look at order 1844, and I'm not going to tell you to wait another week.",
        pete + " do not tell me to wait another week.",
        ["Quinn"],
    )
    assert "another week" not in week.lower()
    assert week.lower().count("look at order 1844") == 1
    owned = ground_reply(
        "hey Pete -- order 1844 showed up with plates instead, and that's on us to sort out. I will look at order 1844.",
        pete,
        ["Quinn"],
    )
    assert "sort out" not in owned.lower()
    assert "on us" not in owned.lower()
    assert owned.endswith(".")
    later = ground_reply(
        "hey Pete -- I hear you. I will look at order 1844 and get back to you. I hear you on not wanting to wait another week.",
        pete,
        ["Quinn"],
    )
    assert "another week" not in later.lower()
    assert "get back" not in later.lower()
    assert "look at order 1844" in later
    mugs = ground_reply(
        "hey Pete -- I will look at order 1844 and the blue mugs that should have been in it.",
        pete,
        ["Quinn"],
    )
    assert mugs.rstrip(".").endswith("look at order 1844")
    assert "plates" in mugs
    assert "with p." not in mugs
    crate = "Casey Dunn\ncan you bring the folding table? Avery has the crate list and Blake has the labels."
    covered = ground_reply(
        "hey Casey -- yes, I'll bring the folding table. with Avery on the crate list and Blake on the labels, that should cover the market crate.",
        crate,
        ["Quinn"],
    )
    assert "should cover" not in covered.lower()
    assert "folding table" in covered
    mina_src = "Mina Patel\ncan you take the second spot and reply today? they lock the list at 4."
    spot = ground_reply(
        "hey Mina -- yes, I'll take the second spot on the new bike rack. this is my reply today before they lock it at 4.",
        mina_src,
        ["Quinn"],
    )
    assert "before" not in spot.lower()
    assert "second spot" in spot
    nora_src = (
        "From: Nora Blake\ncan you bring the new code on a card?\n"
        "From: Quinn\nstill looking at the gate code.\npeople start showing up at 1."
    )
    gate = ground_reply(
        "hey Nora -- I will bring the new park gate code on a card before Sunday, and I will get the blue cooler. "
        "I will have the card, the blue cooler, and the ice there Sunday before people start showing up at 1.",
        nora_src,
        ["Riley Example"],
    )
    assert "bring the new park gate code" not in gate.lower()
    assert "still looking" not in gate.lower()
    assert "before people start showing up" not in gate
    assert "card," not in gate
    owen = "Owen Park 4:02 PM\nbooth Saturday?"
    terse = ground_reply("hey Owen -- yes, I'll be at the booth Saturday.", owen + "\naccount: can you check the other list?", ["Quinn"])
    assert terse.endswith("yes.")
    assert "booth" not in terse.lower()
    assert "all three" not in ground_reply(
        "hey Nora -- I don't have the new park gate code yet. I will have all three for Sunday before people start showing up at 1.",
        nora_src,
        ["Riley Example"],
    ).lower()


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
    assert "hey Priya," in seen[0][0]["content"]
    assert "board room" not in seen[0][0]["content"]
    assert "hey Priya --" in out
    assert "meeting" not in out.lower()
    assert "4 to 6" in out or "Thursday" in out


def test_a_name_outside_the_thread_is_stripped_before_lowercase(tmp_path, monkeypatch, capsys):
    from hailports.clip import run as clip_run

    monkeypatch.setenv("PAISTE_DRAFTS", str(tmp_path / "drafts.jsonl"))
    monkeypatch.setattr(clip_run.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("hailports.clip.gate.allowed", lambda cfg=None, urlopen=None: True)
    monkeypatch.setattr(clip_run, "_instructions", lambda cfg: "You are viewing a communication on the screen. Write the reply.")
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "account.txt").write_text("thanks for the flyers. Mara Solano at Northwind needs an account.\n", encoding="utf-8")
    monkeypatch.setattr(clip_run, "_lookup_notes", lambda cfg, text: "Mara Solano at Northwind needs an account.")
    leak = (
        "hey Nico -- glad the flyers look great. "
        "I will check why Mara Solano can not access the account."
    )
    monkeypatch.setattr(clip_run, "complete", lambda endpoints, messages, http=None, ssh=None: (leak, "claude"))
    monkeypatch.setattr(clip_run, "_complete", lambda cfg: (lambda messages: leak))
    doc = {
        "app": "Slack",
        "title": "flyers",
        "field_text": "Nico Alvarez\nthanks for the flyers yesterday, they look great.",
    }
    code = clip_run.draft_scene(doc, "http://127.0.0.1:9/v1", "x", False, {"paiste": {
        "me": "Quinn",
        "notes_dir": str(notes),
        "order": ["only"],
        "models": {"only": {"via": "http", "base_url": "http://127.0.0.1:9/v1", "model": "x"}},
    }})
    out = capsys.readouterr().out.lower()
    assert code == 0
    assert "flyers" in out
    assert "mara" not in out
    assert "solano" not in out
    assert "northwind" not in out
