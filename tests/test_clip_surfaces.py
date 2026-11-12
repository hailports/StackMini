"""What Paiste reads on each surface. No model, nothing copied, nothing sent."""

from hailports.clip.scene import prepare

SURFACES = [
    {
        "name": "mail",
        "scene": {
            "app": "Microsoft Outlook",
            "title": "deck",
            "field_role": "AXTextArea",
            "field_text": (
                "Best,\nAlex\n\n"
                "From: Dana\nSubject: deck\n\nCan you send the deck Thursday?\n\n"
                "Dana | Example Co.\nPhone: 555-0100\n"
            ),
            "field_box": {"x": 400, "y": 200, "w": 700, "h": 800},
        },
        "ok": True,
        "need": "send the deck Thursday",
        "forbid": ["555-0100", "Best"],
    },
    {
        "name": "browser",
        "scene": {
            "app": "Google Chrome",
            "title": "login fails",
            "field_role": "AXTextArea",
            "field_text": "Add a comment",
            "field_box": {"x": 420, "y": 80, "w": 640, "h": 90},
            "blocks": [
                {"text": "Follow", "role": "AXButton", "box": {"x": 420, "y": 640, "w": 60, "h": 22}},
                {"text": "Login fails after a password reset.", "role": "AXStaticText", "box": {"x": 420, "y": 520, "w": 640, "h": 40}},
                {"text": "Can you list the repro steps?", "role": "AXStaticText", "box": {"x": 420, "y": 400, "w": 640, "h": 36}},
            ],
        },
        "ok": True,
        "need": "repro steps",
        "also": "password reset",
        "forbid": ["Add a comment", "Follow"],
    },
    {
        "name": "chat",
        "scene": {
            "app": "zoom.us",
            "title": "Chat",
            "field_role": "AXTextArea",
            "field_text": "Write a message or type / for more",
            "field_box": {"x": 380, "y": 40, "w": 520, "h": 70},
            "blocks": [
                {"text": "Ctrl+x:shortcuts", "role": "AXButton", "box": {"x": 380, "y": 520, "w": 140, "h": 20}},
                {"text": "Does 2pm still work for the review?", "role": "AXStaticText", "box": {"x": 380, "y": 300, "w": 480, "h": 36}},
            ],
        },
        "ok": True,
        "need": "2pm still work",
        "forbid": ["shortcut", "Write a message"],
    },
    {
        "name": "notes",
        "scene": {
            "app": "Notes",
            "title": "list",
            "field_role": "AXTextArea",
            "field_text": "buy milk\n\ncursor is here",
            "caret": len("buy milk"),
            "field_box": {"x": 0, "y": 0, "w": 400, "h": 500},
        },
        "ok": True,
        "need": "buy milk",
        "forbid": [],
    },
    {
        "name": "selection",
        "scene": {
            "app": "Google Chrome",
            "title": "note",
            "field_role": "AXTextArea",
            "field_text": "a long page of other words that should not win",
            "selection": "The portal needs a hard refresh.",
            "field_box": {"x": 0, "y": 0, "w": 600, "h": 400},
        },
        "ok": True,
        "need": "hard refresh",
        "also": "should not win",
        "forbid": [],
    },
    {
        "name": "chrome",
        "scene": {
            "app": "zoom.us",
            "field_role": "AXButton",
            "field_text": "Ctrl+x:shortcuts",
            "field_box": {"x": 0, "y": 0, "w": 80, "h": 20},
            "blocks": [
                {"text": "Search", "role": "AXButton", "box": {"x": 0, "y": 40, "w": 80, "h": 20}},
            ],
        },
        "ok": False,
        "need": "",
        "forbid": ["shortcut"],
    },
]


def _blob(prepared: dict) -> str:
    return "\n".join(prepared.get("sources") or [])


def test_each_surface_picks_the_reply_and_drops_chrome():
    for row in SURFACES:
        prepared = prepare(row["scene"])
        blob = _blob(prepared)
        assert bool(prepared.get("ok")) is row["ok"], row["name"]
        assert prepared.get("sent") is False
        if row["need"]:
            assert row["need"].lower() in blob.lower(), row["name"]
        if row.get("also"):
            assert row["also"].lower() in blob.lower(), row["name"]
        for banned in row["forbid"]:
            assert banned.lower() not in blob.lower(), row["name"] + " " + banned
