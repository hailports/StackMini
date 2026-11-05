"""Optional iMessage poll. macOS only. Other systems get a clear error.

AppleScript stays in this file. The rest of the package does not call it.
"""

from __future__ import annotations

import shutil
import sys


class MacOnly(RuntimeError):
    pass


def supported() -> bool:
    return sys.platform == "darwin"


def _require() -> None:
    if not supported():
        raise MacOnly("iMessage is a macOS extra. Use web chat or a bridge on this system.")


def tool_available() -> bool:
    _require()
    return shutil.which("imsg") is not None


def compose(router, incoming: str):
    """Model reply for a chat turn. Sending it still requires macOS."""
    return router.complete(
        [
            {"role": "system", "content": "Reply briefly. Do not invent private data."},
            {"role": "user", "content": incoming},
        ],
        task="chat",
    )


def poll_hint() -> str:
    _require()
    if tool_available():
        return "imsg is installed. Point the module at the handle in your config before you enable it."
    return "imsg is not installed. Install it yourself, or leave this module off."


def applescript_probe() -> list[str]:
    """Command list for a read-only Messages check. Not run unless you call it on a Mac."""
    _require()
    script = 'tell application "Messages" to get name'
    return ["osascript", "-e", script]
