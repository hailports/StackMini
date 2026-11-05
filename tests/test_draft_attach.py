"""An unsent draft may attach a presentation. Other types stay refused."""

import pytest

from hailports.plugins.mail import DRAFT_ATTACH_TYPES, draft_attach_plan


def test_presentation_is_on_the_allowlist():
    staged = draft_attach_plan([{"name": "slides.PPTX", "size": 1200}])
    assert staged == [{
        "name": "slides.PPTX",
        "size": 1200,
        "type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }]
    assert ".pptx" in DRAFT_ATTACH_TYPES
    assert ".ppt" not in DRAFT_ATTACH_TYPES


def test_a_basename_is_kept_when_a_path_is_given():
    staged = draft_attach_plan([{"name": "/tmp/deck.pptx", "size": 40}])
    assert staged[0]["name"] == "deck.pptx"
    assert staged[0]["type"].endswith("presentationml.presentation")


def test_unknown_and_old_presentation_extensions_are_refused():
    with pytest.raises(ValueError, match="refuses extension .exe"):
        draft_attach_plan([{"name": "notes.exe", "size": 10}])
    with pytest.raises(ValueError, match="refuses extension .ppt"):
        draft_attach_plan([{"name": "old.ppt", "size": 10}])
    with pytest.raises(ValueError, match=r"refuses extension \(none\)"):
        draft_attach_plan([{"name": "README", "size": 10}])


def test_count_and_size_caps():
    with pytest.raises(ValueError, match="at most 3"):
        draft_attach_plan([{"name": f"n{i}.txt", "size": 1} for i in range(4)])
    with pytest.raises(ValueError, match="empty"):
        draft_attach_plan([{"name": "notes.txt", "size": 0}])
    with pytest.raises(ValueError, match="size cap"):
        draft_attach_plan([{"name": "notes.pdf", "size": 5_000_001}])
    with pytest.raises(ValueError, match="total over"):
        draft_attach_plan([
            {"name": "a.pdf", "size": 4_000_000},
            {"name": "b.docx", "size": 4_000_000},
            {"name": "c.pptx", "size": 3_000_000},
        ])
