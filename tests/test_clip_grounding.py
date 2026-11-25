"""A draft takes its facts from the highlighted text, not from what usually happens.

Field report: an "order delivered" email was highlighted in a full Mail window. The draft said the
package never arrived and offered a trace, a replacement and a discount. The highlighted text sat at
the front of the screen text and the prompt kept only the last 9000 characters of it.
"""

from hailports.clip import compose, hops
from hailports.clip.scene import (
    drop_ungrounded_outcomes,
    draft_rules,
    prepare,
    ungrounded_outcomes,
    wrong_side,
)

DELIVERED = (
    "Hi Sam,\n\nGood news: your order #48213 was delivered on Friday, November 20 and left at the front door.\n"
    "Tracking 9400111899223817460912 shows it signed for.\n\nIf you have questions about the items, reply here.\n\nThe Shop team"
)
# The rest of a mail window: a message list full of other mail, the usual template material.
WINDOW = "\n".join(
    f"Inbox  Promo {i}  Last chance: we extended your discount. Your package is delayed. Open a USPS trace request."
    for i in range(160)
)
TEMPLATE = (
    "Sorry your package never arrived. I opened a USPS trace request and placed a replacement order.\n"
    "I also extended your discount through next week."
)


def _ctx(tmp_path, monkeypatch, doc):
    monkeypatch.setenv("HOME", str(tmp_path))
    return compose.gather(doc, {"paiste": {}}, extra_dirs=[], code_roots=[])


def test_selection_survives_a_long_window_on_the_full_path(tmp_path, monkeypatch):
    doc = {"app": "Mail", "selection": DELIVERED, "window_ocr": WINDOW}
    assert len(compose.screen_text(doc)) > 9000  # the old prompt kept only the tail
    ctx = _ctx(tmp_path, monkeypatch, doc)
    user = compose.messages(ctx, "rules")[1]["content"]
    assert user.startswith("SELECTED TEXT")
    assert DELIVERED in user
    assert "REST OF THE WINDOW (background only" in user
    assert user.index(DELIVERED) < user.index("Promo 159")
    assert "Write the reply now, to the SELECTED TEXT." in user


def test_full_path_without_a_selection_is_unchanged(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, {"app": "Mail", "window_ocr": "Dana: can you send the deck?"})
    user = compose.messages(ctx, "rules")[1]["content"]
    assert "SELECTED TEXT" not in user
    assert "Write the reply now. Only the reply text." in user


def test_quick_path_puts_the_selection_first_even_deep_in_a_long_page():
    blocks = [{"text": "Inbox list " + "x" * 7000}, {"text": DELIVERED}]
    out = prepare({"app": "Mail", "selection": DELIVERED, "blocks": blocks})
    user = out["messages"][1]["content"]
    assert out["kind"] == "selection"
    assert user.startswith("Write the reply to the selected text. Take every fact from it.")
    assert "Selected text (the message you are replying to; take every fact from it):\n" + DELIVERED in user
    assert "Rest of the window (background only" in user


def test_a_chrome_selection_is_not_the_message():
    out = prepare({"app": "Mail", "selection": "Send", "field_text": "Dana: can you send the deck?"})
    assert out["kind"] == "above"
    assert "Selected text" not in out["messages"][1]["content"]


def test_mail_headers_greet_the_newest_sender_and_chat_the_last():
    from hailports.clip.scene import addressee_first
    mail = "From: Dana Lee\nSent: Tuesday\nCan you ship it today?\n\nFrom: Old Timer\nSent: Monday\nhello"
    assert addressee_first(mail, []) == "Dana"
    chat = "Mina Patel 2:10 PM\nspots\n\nChris Adel 2:18 PM\nwho has the van?"
    assert addressee_first(chat, ["Quinn"]) == "Chris"


def test_rules_tell_the_model_the_selection_wins():
    assert "If text is selected, it is the message you reply to." in draft_rules("Sam")
    assert "When there is a SELECTED TEXT block, that is the message you answer." in compose.QUALITY_RULES
    assert "the text wins" in compose.QUALITY_RULES


def test_template_outcomes_on_a_delivered_email_are_caught():
    assert ungrounded_outcomes(TEMPLATE, DELIVERED) == ["that it never arrived", "a replacement", "a trace", "a discount"]
    assert ungrounded_outcomes("Glad it arrived. Thanks for the update.", DELIVERED) == []


def test_outcomes_the_message_states_are_allowed():
    lost = "Hi, my order #77 never arrived. Can you send a replacement or a refund?"
    assert ungrounded_outcomes("Sorry it never arrived. I will send a replacement today.", lost) == []
    assert ungrounded_outcomes("I can do a refund instead.", lost) == []
    promo = "Your order ships Monday. Use code FALL for 20% off your next purchase."
    assert ungrounded_outcomes("Thanks, I will use the discount next time.", promo) == []


def test_outside_orders_a_replacement_value_is_not_order_talk():
    chat = "Can you swap the replacement value in the config before the demo?"
    assert ungrounded_outcomes("Yes, I will update the replacement value today.", chat) == []


def test_an_invented_offer_is_caught_in_any_thread():
    ask = "Can you send the deck by Friday?"
    assert ungrounded_outcomes("Yes, Friday works. I can also give you a discount on the next one.", ask) == ["a discount"]
    assert ungrounded_outcomes("Yes, Friday works.", ask) == []
    sale = "We are running the holiday sale this week, can you draft the email?"
    assert ungrounded_outcomes("Here is the draft for the holiday discount.", sale) == []


def test_drop_keeps_the_grounded_sentences():
    reply = ("Thanks for the update.\n\n1. Glad it arrived on Friday.\n2. I opened a USPS trace request.\n"
             "Sorry it never arrived. I will check the items you asked about.")
    assert drop_ungrounded_outcomes(reply, DELIVERED) == (
        "Thanks for the update.\n\n1. Glad it arrived on Friday.\nI will check the items you asked about.")
    assert drop_ungrounded_outcomes("Glad it arrived.", DELIVERED) == "Glad it arrived."


def test_full_path_retries_then_drops_a_template_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    doc = {"app": "Mail", "selection": DELIVERED, "window_ocr": WINDOW}
    asked = []

    def model(messages):
        last = str(messages[-1]["content"])
        asked.append(last)
        if "Return only JSON" in last:
            return "{}"
        if "never says so" in last:
            return "Glad it arrived on Friday. Thanks for letting me know."
        return TEMPLATE

    text, _ctx2, attempts = compose.draft(doc, {"paiste": {}}, "rules", model, extra_dirs=[], code_roots=[])
    assert "never says so" in " ".join(attempts[0]["issues"])
    assert text.lower().startswith("glad it arrived")
    assert not ungrounded_outcomes(text, DELIVERED)

    stubborn, _c, _a = compose.draft(doc, {"paiste": {}}, "rules", lambda m: TEMPLATE + "\nThanks for the note.",
                                     extra_dirs=[], code_roots=[])
    assert "never arrived" not in stubborn and "trace" not in stubborn and "discount" not in stubborn


# Retest 3: the selection was grounded, but the draft answered as the shop that sent it.

SHOP_VOICE = ("hey WolfMilk, no, it shows as delivered because our records have the tracking number. "
              "Check your porch and your neighbors.")


def test_a_reply_in_the_senders_voice_is_caught():
    assert wrong_side(SHOP_VOICE, DELIVERED) == ["check your porch", "our records"]
    assert wrong_side("hey Shop, got it on Friday, thanks.", DELIVERED) == []


def test_a_shop_answering_its_customer_may_speak_as_the_shop():
    customer = "Hi, I ordered a blender last week and my package never arrived. Where is my order?"
    assert wrong_side("hey Dana, our records show it was delivered Friday. Check your porch.", customer) == []


def test_rules_say_the_selection_was_sent_to_you():
    assert "write back to its sender as yourself, never in the sender's voice" in draft_rules("Sam")
    out = compose.messages(compose.Context(me=["Scot"], selection=DELIVERED, screen=DELIVERED), "rules")
    assert "never in the sender's voice" in out[-1]["content"] or "never in the sender's voice" in out[1]["content"]


def test_full_path_retries_then_drops_the_senders_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    doc = {"app": "Mail", "selection": DELIVERED, "window_ocr": WINDOW}

    def model(messages):
        last = str(messages[-1]["content"])
        if "Return only JSON" in last:
            return "{}"
        if "speaks as the sender" in last:
            return "hey Shop, it came Friday, thanks for the tracking number."
        return SHOP_VOICE

    text, _c, attempts = compose.draft(doc, {"paiste": {}}, "rules", model, extra_dirs=[], code_roots=[])
    assert "speaks as the sender" in " ".join(attempts[0]["issues"])
    assert "it came Friday" in text and not wrong_side(text, DELIVERED)
    stubborn, _c, _a = compose.draft(doc, {"paiste": {}}, "rules",
                                     lambda m: SHOP_VOICE + "\nThanks again.", extra_dirs=[], code_roots=[])
    assert "our records" not in stubborn and "porch" not in stubborn


# A dead cloud engine must not block the model on this Mac.

def _route(tmp_path, key: str | None, llm_url="http://127.0.0.1:8000/v1"):
    spec = {"base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"}
    if key is not None:
        path = tmp_path / "openai.key"
        if key != "missing":
            path.write_text(key)
        spec["key_file"] = str(path)
    route = {"paiste": {"order": ["api"], "models": {"api": spec}}}
    if llm_url:
        route["llm"] = {"base_url": llm_url, "model": "Qwen3.6-35B-A3B-4bit"}
    return route


def test_a_missing_or_empty_key_file_skips_the_paid_engine(tmp_path):
    for key in ("missing", "", "  \n"):
        got = hops.endpoints_from(_route(tmp_path, key))
        assert [(h["name"], h["base_url"], h["model"]) for h in got] == [
            ("default", "http://127.0.0.1:8000/v1", "Qwen3.6-35B-A3B-4bit")], key


def test_a_keyed_engine_falls_back_to_the_local_model(tmp_path):
    got = hops.endpoints_from(_route(tmp_path, "sk-test"))
    assert [h["name"] for h in got] == ["api", "llm"]
    assert got[1]["base_url"] == "http://127.0.0.1:8000/v1" and got[1]["model"] == "Qwen3.6-35B-A3B-4bit"


def test_the_fallback_is_only_a_local_model_and_never_a_duplicate(tmp_path):
    assert [h["name"] for h in hops.endpoints_from(_route(tmp_path, "sk-test", "http://gpu-box.example:8000/v1"))] == ["api"]
    assert [h["name"] for h in hops.endpoints_from(_route(tmp_path, "sk-test", ""))] == ["api"]
    route = {"paiste": {"order": ["local"], "models": {"local": {"base_url": "http://127.0.0.1:8000/v1/", "model": "a"}}},
             "llm": {"base_url": "http://127.0.0.1:8000/v1", "model": "a"}}
    assert [h["name"] for h in hops.endpoints_from(route)] == ["local"]


def test_a_failing_cloud_engine_reaches_the_local_model(tmp_path):
    tried = []

    def http(endpoint, messages):
        tried.append(endpoint["name"])
        if endpoint["name"] == "api":
            raise OSError("401")
        return "Glad it arrived."

    text, name = hops.complete(hops.endpoints_from(_route(tmp_path, "sk-expired")),
                               [{"role": "user", "content": "On screen: it was delivered"}], http=http)
    assert (text, name, tried) == ("Glad it arrived.", "llm", ["api", "llm"])
