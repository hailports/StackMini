"""What paisty read, a check-the-numbers hint, and drafting from copied text before any permission.

Tester note: buyers hesitate at "an app that reads my email window" and at one wrong number in a draft.
Each draft keeps a plain receipt of what was read (on this Mac only), the numbers in it are listed for a
quick check, and copied text can be drafted from without Accessibility or Screen Recording.
"""

from pathlib import Path

from hailports.clip import drafts

ROOT = Path(__file__).resolve().parents[1]
DELIVERED = "Hi Sam,\n\nyour order #48213 was delivered on Friday, Nov 20.\n\nThe Shop team"


def test_a_draft_row_keeps_what_was_read_and_the_numbers(tmp_path):
    path = tmp_path / "drafts.jsonl"
    scene = {"app": "Mail", "title": "Delivered", "selection": DELIVERED, "window_ocr": "x" * 9000,
             "images": ["shot.png"]}
    row = drafts.append("hey Shop, got #48213 on Friday, Nov 20 for $24.99.", app="Mail", path=path,
                        scene=scene, lookups=2)
    assert row["read"][0] == "App: Mail (Delivered)"
    assert row["read"][1].startswith("Selected text (") and "your order #48213" in row["read"][1]
    assert "Window text: 9,000 characters" in row["read"]
    assert "Screenshots: 1" in row["read"] and "Lookups: 2 facts" in row["read"]
    assert row["read"][-1] == "This stays on this Mac."
    assert row["check"] == ["#48213", "Friday", "Nov 20", "$24.99"]
    listed = drafts.listed(drafts.recent(path=path))[0]
    assert listed["read"] == row["read"] and listed["check"] == row["check"]


def test_a_long_selection_is_cut_and_a_secret_scene_shows_only_its_size(tmp_path):
    long = "word " * 200
    read = drafts.receipt({"app": "Mail", "selection": long})
    assert len(read[1]) < 170 and read[1].endswith("…")
    secret = drafts.receipt({"app": "Mail", "title": "Reset code", "selection": "your code is 123456"}, secret=True)
    assert secret[0] == "App: Mail" and secret[1] == "Selected text: 19 characters"


def test_no_numbers_means_no_check_and_no_scene_means_no_receipt(tmp_path):
    row = drafts.append("hey Dana, sounds good, I will send it over.", path=tmp_path / "d.jsonl")
    assert "check" not in row and "read" not in row
    assert drafts.check_items("call me at 3:15 pm on 11/20, it was 15% off") == ["3:15 pm", "11/20", "15%"]


def _swift(name: str) -> str:
    return (ROOT / "contrib/clip" / name).read_text(encoding="utf-8")


def test_the_menu_shows_the_receipt_the_check_and_copied_text_drafting():
    src = _swift("PaisteMenu.swift")
    assert 'header("What paisty read")' in src and '"Check: "' in src
    assert '"Draft from copied text"' in src and '"draft", "--scene"' in src
    assert '"Check before you send: "' in src and "nonactivatingPanel" in src


def test_onboarding_offers_copy_and_paste_before_any_permission():
    src = _swift("Onboard.swift")
    assert "Try it first with copy and paste" in src
    assert "To try it first with copy and paste, before turning anything on" in src
