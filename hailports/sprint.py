"""Pack a local backlog into a sprint packet and calendar focus blocks."""

from __future__ import annotations

from hailports.calendar_blocker import carve, to_ics


def plan(items: list[dict], capacity_hours: float, days: int = 10) -> dict:
    left = float(capacity_hours)
    taken = []
    skipped = []
    for item in items:
        hours = float(item.get("hours") or 2)
        title = str(item.get("title") or "untitled")
        if hours <= left:
            taken.append({"title": title, "hours": hours})
            left -= hours
        else:
            skipped.append({"title": title, "hours": hours})
    return {
        "days": days,
        "capacity_hours": capacity_hours,
        "used_hours": round(capacity_hours - left, 2),
        "taken": taken,
        "later": skipped,
    }


def packet(items: list[dict], capacity_hours: float, day: str, days: int = 10, start_hour: int = 9) -> dict:
    """Sprint plan plus focus blocks and an iCalendar body for the taken items."""
    planned = plan(items, capacity_hours, days=days)
    blocks = carve([item["title"] for item in planned["taken"]], day, start_hour=start_hour)
    planned["blocks"] = [
        {"day": block.day, "start": block.start, "minutes": block.minutes, "title": block.title}
        for block in blocks
    ]
    planned["ics"] = to_ics(blocks)
    return planned
