"""Logical point sizes a Mac desktop can be asked to use.

HiDPI framebuffers are twice the width and height. iPad sizes are the
panel's native pixel grid divided by two. The iPhone row is the portrait
canvas used when a phone viewer is connected: 1024x768 rotated to 768x1024.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Mode:
    width: int
    height: int
    aspect: str
    hidpi: bool = True
    degree: int = 0
    name: str = ""
    kind: str = "mac"

    def as_dict(self) -> dict:
        return asdict(self)


def aspect_name(width: int, height: int) -> str:
    if height <= 0 or width <= 0:
        return "custom"
    ratio = width / height
    targets = (("16:10", 16 / 10), ("16:9", 16 / 9), ("4:3", 4 / 3))
    for name, target in targets:
        if abs(ratio - target) < 0.02 or abs(ratio - (1 / target)) < 0.02:
            return name
    return "custom"


def mode(width: int, height: int, kind: str, name: str = "", degree: int = 0, hidpi: bool = True) -> Mode:
    return Mode(width, height, aspect_name(width, height), hidpi, degree, name or f"{width}x{height}", kind)


# Built-in Mac logical sizes, plus the iPad native grids and the phone canvas.
MAC_MODES: tuple[Mode, ...] = (
    mode(1280, 800, "mac", "16:10-1280x800"),
    mode(1440, 900, "mac", "16:10-1440x900"),
    mode(1680, 1050, "mac", "16:10-1680x1050"),
    mode(1920, 1200, "mac", "16:10-1920x1200"),
    mode(2560, 1600, "mac", "16:10-2560x1600"),
    mode(1344, 840, "mac", "16:10-1344x840"),
    mode(1280, 720, "mac", "16:9-1280x720"),
    mode(1600, 900, "mac", "16:9-1600x900"),
    mode(1920, 1080, "mac", "16:9-1920x1080"),
    mode(2560, 1440, "mac", "16:9-2560x1440"),
    mode(3840, 2160, "mac", "16:9-3840x2160"),
    mode(800, 600, "mac", "4:3-800x600"),
    mode(1024, 768, "mac", "4:3-1024x768"),
    mode(1280, 960, "mac", "4:3-1280x960"),
    mode(1344, 1008, "mac", "4:3-1344x1008"),
    mode(1600, 1200, "mac", "4:3-1600x1200"),
)

# Native pixels / 2. The 13-inch class panel is 2752x2064, so the desktop is 1376x1032.
IPAD_MODES: tuple[Mode, ...] = (
    mode(1376, 1032, "ipad", "ipad-pro-13"),
    mode(1366, 1024, "ipad", "ipad-pro-12.9"),
    mode(1210, 834, "ipad", "ipad-pro-11"),
    mode(1194, 834, "ipad", "ipad-pro-11-previous"),
    mode(1180, 820, "ipad", "ipad-air-11"),
    mode(1133, 744, "ipad", "ipad-mini"),
)

# Unrotated source is 1024x768 HiDPI. Degree 90 presents 768x1024.
IPHONE_MODE = mode(768, 1024, "iphone", "iphone-portrait", degree=90)
MAC_DEFAULT = mode(1680, 1050, "mac", "mac-16-10")
IPAD_NATIVE = mode(1376, 1032, "ipad", "ipad-pro-13")

CATALOG: tuple[Mode, ...] = MAC_MODES + IPAD_MODES + (IPHONE_MODE,)


def find_named(name: str, extra: list[Mode] | None = None) -> Mode | None:
    key = name.strip().lower()
    pool = list(CATALOG) + list(extra or [])
    for item in pool:
        if item.name.lower() == key or f"{item.width}x{item.height}" == key:
            return item
    return None
