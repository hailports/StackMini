"""Move windows onto a smaller desktop.

After the display drops to the client size, a window that used to fit the
old desktop can sit past the new edge. This helper shrinks and shifts those
frames. It does not grow a window, and it does not touch frames that already
fit. The phone keyboard inset is a separate step.
"""

from __future__ import annotations


def _box(frame: dict) -> dict[str, int]:
    return {key: int(frame[key]) for key in ("x", "y", "w", "h")}


def fit_frame(frame: dict, canvas: dict) -> dict[str, int]:
    current = _box(frame)
    left = int(canvas.get("x", 0))
    top = int(canvas.get("y", 0))
    width = int(canvas["w"])
    height = int(canvas["h"])
    if width < 1 or height < 1:
        return current
    fitted = dict(current)
    fitted["w"] = min(current["w"], width)
    fitted["h"] = min(current["h"], height)
    fitted["x"] = min(max(current["x"], left), left + width - fitted["w"])
    fitted["y"] = min(max(current["y"], top), top + height - fitted["h"])
    return fitted


def reflow(frames: list[dict], canvas: dict) -> list[dict]:
    """Return the frames that had to move, with `before` and `after`."""
    moved = []
    for frame in frames:
        before = _box(frame)
        after = fit_frame(frame, canvas)
        if after != before:
            moved.append({"before": before, "after": after, "id": frame.get("id")})
    return moved
