from hailports.clip.scene import plain_reply, strip_signoff


def test_template_name_line_is_dropped():
    draft = "Thursday at 2pm works for me. Looking forward to it.\n\nBest, \n[Your Name]"
    assert plain_reply(draft) == "Thursday at 2pm works for me. Looking forward to it."


def test_bare_name_placeholder_is_dropped():
    assert strip_signoff("Sounds good, see you then.\n[Name]") == "Sounds good, see you then."


def test_brackets_inside_a_sentence_stay():
    assert strip_signoff("Use [brackets] in code.") == "Use [brackets] in code."
