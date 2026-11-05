#!/usr/bin/env python3
"""Configure a llama.cpp build for this machine.

The reference Metal server that served the vision model was commit
f174553e1621c6a22a64e7bfcbb58d3a5c49e6fb. Pass --commit to build that
revision. With no --commit, cmake uses the sources in --source.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hailports_setup.detect import probe  # noqa: E402
from hailports_setup.models import REFERENCE_COMMIT, source_flags  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configure llama.cpp")
    parser.add_argument("--source", required=True, help="Path to a llama.cpp checkout")
    parser.add_argument("--commit", default="", help="Checkout this commit first")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--target-os", choices=["linux", "darwin", "windows"])
    parser.add_argument("--accel", choices=["metal", "cuda", "rocm", "vulkan", "cpu"])
    args = parser.parse_args(argv)
    hw = probe(args.target_os, accel=args.accel)
    flags = source_flags(hw)
    commit = args.commit or REFERENCE_COMMIT
    source = Path(args.source)
    print(f"commit: {commit}")
    print("accel: " + hw.accel)
    cmake = ["cmake", "-B", str(source / "build"), "-S", str(source), *flags]
    build = ["cmake", "--build", str(source / "build"), "--config", "Release", "-j", str(hw.cores)]
    print(" ".join(cmake))
    print(" ".join(build))
    if args.commit and not re.fullmatch(r"[0-9a-fA-F]{7,40}", args.commit):
        print("commit must be a hex sha")
        return 2
    if args.dry_run:
        return 0
    if args.commit:
        subprocess.check_call(["git", "-C", str(source), "checkout", "--detach", args.commit])
    subprocess.check_call(cmake)
    subprocess.check_call(build)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
