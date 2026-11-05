#!/usr/bin/env python3
"""Guided installer. Packaging uses pyproject.toml.

    python setup.py
    python setup.py --dry-run --non-interactive --target-os linux
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGING = {"egg_info", "bdist_wheel", "sdist", "install", "develop", "editable_wheel", "dist_info"}
if any(arg in sys.argv for arg in _PACKAGING):
    from setuptools import setup
    setup()
else:
    ROOT = Path(__file__).resolve().parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from hailports.wizard import main
    if __name__ == "__main__":
        raise SystemExit(main())
