"""Data directories. platformdirs when it is installed, same layout otherwise."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _fallback(kind: str) -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        root = Path(base) / "hailports"
        return root / ("data" if kind == "data" else "")
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "hailports"
        return root if kind == "data" else root
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    if kind == "config":
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(xdg) / "hailports"
    return Path(xdg) / "hailports"


def _via_platformdirs(kind: str) -> Path | None:
    try:
        import platformdirs
    except ImportError:
        return None
    if kind == "config":
        return Path(platformdirs.user_config_dir("hailports", "HailPorts"))
    return Path(platformdirs.user_data_dir("hailports", "HailPorts"))


def config_dir() -> Path:
    override = os.environ.get("HAILPORTS_CONFIG_DIR")
    if override:
        return Path(override)
    return _via_platformdirs("config") or _fallback("config")


def data_dir() -> Path:
    override = os.environ.get("HAILPORTS_DATA_DIR")
    if override:
        return Path(override)
    return _via_platformdirs("data") or _fallback("data")


def ensure_dirs() -> tuple[Path, Path]:
    c, d = config_dir(), data_dir()
    c.mkdir(parents=True, exist_ok=True)
    d.mkdir(parents=True, exist_ok=True)
    return c, d
