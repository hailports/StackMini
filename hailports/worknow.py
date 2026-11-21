"""Current focus cards stored in a local JSON file. Empty until you add some."""

from __future__ import annotations

import json
from pathlib import Path


def load_cards(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def save_cards(path: Path, cards: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cards, indent=2) + "\n", encoding="utf-8")


def _rich(cards: list[dict]) -> bool:
    for card in cards:
        if not isinstance(card, dict):
            continue
        if card.get("state") or card.get("owner") or card.get("changed") or "mine" in card:
            return True
    return False


def brief(router, cards: list[dict], *, user: str = ""):
    """Model brief of the focus list. The list text is notes, not the reply."""
    system = "Turn the focus list into a short brief. Use only these lines."
    if _rich(cards):
        from hailports.focus_rank import phrase_instructions

        system += " " + phrase_instructions()
    return router.complete(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": pack(cards, user=user)},
        ],
        task="summarize",
    )


def pack(cards: list[dict], limit: int = 5, *, user: str = "") -> str:
    if not cards:
        return "Nothing on the focus list."
    if _rich(cards):
        from hailports.focus_rank import pack_focus

        return pack_focus(cards, limit=limit, user=user)
    lines = []
    for card in cards[:limit]:
        title = str(card.get("title") or "untitled")
        detail = str(card.get("detail") or "").strip()
        line = title if not detail else f"{title}: {detail}"
        lines.append("- " + line)
    return "\n".join(lines)
