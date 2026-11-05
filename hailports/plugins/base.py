"""Shared plug shape. A plug stays off until its token is in the environment."""

from __future__ import annotations

import os

from hailports.config import is_placeholder

# Names match .env.example. The config file keeps a placeholder, not the token.
TOKEN_ENV = {
    "crm": "CRM_TOKEN",
    "tracker": "TRACKER_TOKEN",
    "mail": "MAIL_TOKEN",
    "meetings": "MEETINGS_TOKEN",
    "files": "FILES_TOKEN",
}


class Plugin:
    name = "plugin"

    def __init__(self, settings: dict) -> None:
        self.settings = settings or {}

    def token(self) -> str:
        env_name = TOKEN_ENV.get(self.name, "")
        if not env_name:
            return ""
        return os.environ.get(env_name, "").strip()

    def configured(self) -> bool:
        base = str(self.settings.get("base_url") or "")
        token = self.token()
        if is_placeholder(base) or is_placeholder(token):
            return False
        return bool(token and base)

    def describe(self) -> str:
        if not self.configured():
            return f"{self.name}: off (placeholder config)"
        return f"{self.name}: configured"
