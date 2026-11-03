"""Turn a short request into a user-story card. It does not file the card anywhere."""

from __future__ import annotations


def story_from_request(text: str, project: str = "PROJECT") -> dict:
    title = " ".join(text.strip().split())
    if len(title) > 80:
        title = title[:77] + "..."
    return {
        "project": project,
        "type": "story",
        "title": title or "Untitled request",
        "as_a": "user of this appliance",
        "i_want": title or "a clear outcome",
        "so_that": "the work is spelled out before anyone starts",
        "acceptance": [
            "The result matches the request title.",
            "No private data is added to the card.",
            "A person reviews the card before it is filed.",
        ],
    }


def story_reply(router, text: str, project: str = "PROJECT"):
    """Model-written story card. The project name is the only extra field."""
    return router.complete(
        [
            {"role": "system", "content": "Write a user story card in plain lines: project, title, who it is for, what they want, why, and acceptance lines. Do not add private data."},
            {"role": "user", "content": f"Project: {project}\nRequest: {text}"},
        ],
        task="draft",
    )


def render(card: dict) -> str:
    lines = [
        f"Project: {card['project']}",
        f"Type: {card['type']}",
        f"Title: {card['title']}",
        "",
        f"As a {card['as_a']}, I want {card['i_want']}, so that {card['so_that']}.",
        "",
        "Acceptance:",
    ]
    for item in card["acceptance"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"
