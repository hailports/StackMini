"""Guided prompts. Questionary when a terminal asks for it, plain text otherwise."""

from __future__ import annotations

import getpass
import os
import sys


def use_questionary() -> bool:
    flag = os.environ.get("HAILPORTS_PROMPT", "").strip().lower()
    if flag == "plain":
        return False
    if flag == "questionary":
        return _questionary_importable()
    if not sys.stdin.isatty():
        return False
    return _questionary_importable()


def _questionary_importable() -> bool:
    # A test can plant a fake module with no __spec__. find_spec raises on that.
    planted = sys.modules.get("questionary", False)
    if planted is not False:
        return planted is not None
    import importlib.util
    return importlib.util.find_spec("questionary") is not None


def interpret(raw: str, choices: list[tuple[str, str]], default: str):
    text = (raw or "").strip()
    if text == "":
        return default
    low = text.lower()
    if low in ("back", "b"):
        return "back"
    if low in ("skip", "s"):
        return "skip"
    if low.isdigit():
        index = int(low)
        if 1 <= index <= len(choices):
            return choices[index - 1][0]
    for value, _blurb in choices:
        if low == value.lower():
            return value
    return None


class PlainIO:
    """Numbered questions. Type back to return, skip to leave an optional step."""

    def __init__(self, replies: list[str] | None = None, presets: dict | None = None) -> None:
        self.replies = list(replies or [])
        self.presets = dict(presets or {})

    def say(self, text: str) -> None:
        print(text)

    def fail(self, what: str, how: str) -> None:
        print("That step failed.")
        print("What went wrong: " + what)
        print("How to fix: " + how)

    def progress(self, label: str, frac: float) -> None:
        pct = max(0, min(100, int(float(frac) * 100)))
        print(f"progress {label} {pct}%")

    def _read(self, key: str) -> str:
        if self.replies:
            value = str(self.replies.pop(0))
            print("> " + value)
            return value
        if key in self.presets:
            value = str(self.presets[key])
            print("> " + value)
            return value
        try:
            return input("> ").strip()
        except EOFError:
            return ""

    def choose(self, key: str, title: str, explain: str, choices: list[tuple[str, str]], default: str) -> str:
        self._banner(title, explain)
        print(f"step {key}")
        for index, (value, blurb) in enumerate(choices, 1):
            print(f"{index} {value}  {blurb}")
        print(f"default {default}")
        if use_questionary() and not self.replies and key not in self.presets:
            picked = _questionary_select(title, choices, default)
            print("> " + picked)
            return picked
        while True:
            raw = self._read(key)
            parsed = interpret(raw, choices, default)
            if parsed is None:
                print("That is not one of the choices. Type a number, back, or skip.")
                if self.replies or key in self.presets:
                    return default
                continue
            return parsed

    def ask(self, key: str, title: str, explain: str, default: str = "") -> str:
        self._banner(title, explain)
        print(f"step {key}")
        print(f"{title} [{default}]")
        raw = self._read(key)
        if raw.strip().lower() in ("back", "b"):
            return "back"
        if raw.strip().lower() in ("skip", "s"):
            return "skip"
        if raw.strip() == "":
            return default
        return raw.strip()

    def confirm(self, key: str, title: str, explain: str, default: bool = True) -> bool:
        self._banner(title, explain)
        print(f"step {key}")
        hint = "Y/n" if default else "y/N"
        print(f"{title} [{hint}]")
        if key in self.presets and not self.replies:
            raw = str(self.presets[key])
            print("> " + raw)
        else:
            raw = self._read(key)
        if raw.strip() == "":
            return default
        return raw.strip().lower() in ("y", "yes", "true", "1")

    def secret(self, title: str) -> str:
        print(title)
        if self.replies:
            return str(self.replies.pop(0))
        try:
            return getpass.getpass("> ")
        except (EOFError, getpass.GetPassWarning):
            return ""

    def _banner(self, title: str, explain: str) -> None:
        plain = os.environ.get("HAILPORTS_PROMPT", "").strip().lower() == "plain"
        body = ((explain + "\n\n") if explain else "") + title
        if not plain:
            try:
                from rich.console import Console
                from rich.panel import Panel
                Console().print(Panel(body, title="HailPorts setup"))
                return
            except Exception:
                pass
        if explain:
            print(explain)
        print(title)


class YesIO(PlainIO):
    """Non-interactive. Every answer is the preset or the default. Nothing is read from the keyboard."""

    def __init__(self, presets: dict | None = None) -> None:
        super().__init__(replies=None, presets=presets or {})

    def _read(self, key: str) -> str:
        value = "" if key not in self.presets else str(self.presets[key])
        if value:
            print("> " + value)
        return value

    def secret(self, title: str) -> str:
        print(title)
        return ""


def _questionary_select(title: str, choices: list[tuple[str, str]], default: str) -> str:
    import questionary
    labels = [value for value, _blurb in choices]
    picked = questionary.select(title, choices=labels, default=default if default in labels else None).ask()
    if not picked:
        return default
    return str(picked)
