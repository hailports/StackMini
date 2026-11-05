"""Chat bridge stubs. They do nothing until an environment token is set."""

from __future__ import annotations

import os

from hailports.config import is_placeholder

TOKEN_ENV = {
    "telegram": "TELEGRAM_TOKEN",
    "signal": "SIGNAL_ACCOUNT",
    "matrix": "MATRIX_TOKEN",
}


class NotConfigured(RuntimeError):
    pass


class Bridge:
    name = "bridge"

    def __init__(self, settings: dict) -> None:
        self.settings = settings or {}

    def configured(self) -> bool:
        env_name = TOKEN_ENV.get(self.name, "")
        token = os.environ.get(env_name, "").strip() if env_name else ""
        return bool(token) and not is_placeholder(token)

    def send(self, text: str) -> str:
        if not self.configured():
            raise NotConfigured(f"{self.name} is not configured")
        return f"would send via {self.name}: {text[:80]}"


class TelegramBridge(Bridge):
    name = "telegram"


class SignalBridge(Bridge):
    name = "signal"


class MatrixBridge(Bridge):
    name = "matrix"


def load_all(cfg: dict) -> list[Bridge]:
    block = cfg.get("bridges") or {}
    return [
        TelegramBridge(block.get("telegram") or {}),
        SignalBridge(block.get("signal") or {}),
        MatrixBridge(block.get("matrix") or {}),
    ]
