"""Build focus blocks and an iCalendar file. This does not talk to a calendar server."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Block:
    day: str
    start: str
    minutes: int
    title: str


def carve(tasks: list[str], day: str, start_hour: int = 9, minutes: int = 50) -> list[Block]:
    blocks = []
    hour = start_hour
    minute = 0
    for title in tasks:
        stamp = f"{hour:02d}{minute:02d}00"
        blocks.append(Block(day=day, start=stamp, minutes=minutes, title=title))
        minute += minutes
        hour += minute // 60
        minute = minute % 60
    return blocks


def to_ics(blocks: list[Block]) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HailPorts//appliance//EN"]
    for i, block in enumerate(blocks, 1):
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:block-{i}",
            f"DTSTART:{block.day}T{block.start}",
            f"SUMMARY:{block.title}",
            f"DURATION:PT{block.minutes}M",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\n".join(lines) + "\n"
