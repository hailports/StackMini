"""Text scrolled out of view reaches the draft without pushing out what is on screen.

The app reads a web view's whole page through Accessibility text markers (no scrolling, nothing on screen
changes) and sends what is not already visible as one last block with role AXOffscreenText.
"""

from hailports.clip import compose
from hailports.clip.scene import OFFSCREEN, _page_text, scene_from

HIDDEN = "On Monday, Dana wrote: the invoice number is INV-20931 and it is due December 4."


def _doc(field="hey Dana,", visible="Dana Lee 2:10 PM\ncan you confirm the invoice?"):
    return {"app": "Safari", "field_text": field, "origin": "cocoa",
            "blocks": [{"text": visible, "role": "AXStaticText"}, {"text": HIDDEN, "role": OFFSCREEN}]}


def test_hidden_text_reaches_the_quick_path_after_the_visible_text():
    page = _page_text(scene_from(_doc()))
    assert "INV-20931" in page
    assert page.index("can you confirm") < page.index("INV-20931")


def test_hidden_text_never_pushes_the_field_out_of_the_quick_path():
    doc = _doc()
    doc["blocks"][1]["text"] = "old thread line\n" * 2000
    page = _page_text(scene_from(doc))
    assert "can you confirm the invoice?" in page and "hey Dana," in page


def test_full_path_keeps_the_visible_text_when_the_hidden_text_is_long():
    doc = _doc()
    doc["blocks"][1]["text"] = "old thread line\n" * 2000
    screen = compose.screen_text(doc)
    assert "can you confirm the invoice?" in screen[-9000:]
    assert "old thread line" in screen


def test_the_newest_message_is_read_from_the_visible_text_only():
    assert HIDDEN not in compose.screen_text(_doc(), hidden=False)
    assert HIDDEN in compose.screen_text(_doc())
