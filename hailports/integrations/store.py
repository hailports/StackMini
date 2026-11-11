"""Integration tokens live in the OS keychain or a 0600 file. They are not logged."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


class IntegrationError(RuntimeError):
    pass


def _tighten(path: Path) -> None:
    try:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    except OSError:
        pass


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _tighten(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    _tighten(tmp)
    os.replace(tmp, path)
    _tighten(path)


def _account(name: str) -> str:
    cleaned = str(name or "").strip().lower()
    if not cleaned or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in cleaned):
        raise IntegrationError("unknown token name")
    return "int-" + cleaned


def _keychain_put(account: str, raw: str) -> bool:
    if os.environ.get("HAILPORTS_NO_KEYCHAIN") == "1":
        return False
    from hailports.secrets_store import keychain_set
    try:
        keychain_set("hailports", account, raw)
        return True
    except Exception as exc:
        if raw and raw in str(exc):
            raise IntegrationError("keychain rejected the token") from None
        return False


def _keychain_get(account: str) -> str | None:
    if os.environ.get("HAILPORTS_NO_KEYCHAIN") == "1":
        return None
    from hailports.secrets_store import keychain_get
    try:
        return keychain_get("hailports", account)
    except Exception:
        return None


def _keychain_delete(account: str) -> None:
    if os.environ.get("HAILPORTS_NO_KEYCHAIN") == "1":
        return
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["security", "delete-generic-password", "-s", "hailports", "-a", account],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return
        if sys.platform != "win32" and shutil.which("secret-tool"):
            subprocess.run(
                ["secret-tool", "clear", "service", "hailports", "account", account],
                capture_output=True, text=True, timeout=5, check=False,
            )
    except (OSError, subprocess.SubprocessError):
        return


class TokenBox:
    """One name per provider. Values never appear in repr or raised messages."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.file = self.root / "tokens.json"

    def __repr__(self) -> str:
        return "TokenBox()"

    def _read_file(self) -> dict:
        if not self.file.exists():
            return {}
        try:
            loaded = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _write_file(self, data: dict) -> None:
        _write_private(self.file, json.dumps(data, indent=2) + "\n")

    def put(self, name: str, payload: dict) -> str:
        if not isinstance(payload, dict) or not payload:
            raise IntegrationError("token payload is empty")
        account = _account(name)
        raw = json.dumps(payload, separators=(",", ":"))
        if _keychain_put(account, raw):
            data = self._read_file()
            if name in data:
                data.pop(name, None)
                self._write_file(data)
            return "keychain"
        data = self._read_file()
        data[name] = payload
        self._write_file(data)
        return "file"

    def get(self, name: str) -> dict | None:
        account = _account(name)
        raw = _keychain_get(account)
        if raw:
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                return loaded
        item = self._read_file().get(name)
        return dict(item) if isinstance(item, dict) else None

    def delete(self, name: str) -> None:
        account = _account(name)
        _keychain_delete(account)
        data = self._read_file()
        if name in data:
            data.pop(name, None)
            if data:
                self._write_file(data)
            elif self.file.exists():
                self.file.unlink()

    def names(self) -> list[str]:
        found = set(self._read_file())
        return sorted(found)


def write_status(root: Path, status: dict) -> None:
    path = Path(root) / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")


def load_mask(root: Path) -> list[str]:
    path = Path(root) / "mask.json"
    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded if str(item or "").strip()]


def save_mask(root: Path, terms: list[str]) -> None:
    cleaned = []
    for item in terms:
        text = str(item or "").strip()
        if len(text) >= 3 and text not in cleaned:
            cleaned.append(text)
    _write_private(Path(root) / "mask.json", json.dumps(cleaned, indent=2) + "\n")


def read_status(root: Path | None) -> dict:
    if root is None:
        return {}
    path = Path(root) / "status.json"
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def safe_text(text: str) -> str:
    raw = " ".join(str(text or "").split())
    raw = re.sub(
        r"(?i)\b(authorization|bearer|token|password|secret|client_secret)\b\s*[:=]\s*\S+",
        r"\1 [masked]",
        raw,
    )
    raw = re.sub(r"(?i)\bbearer\s+\S+", "bearer [masked]", raw)
    if len(raw) > 180:
        raw = raw[:180]
    return raw or "request failed"
