"""Make sure llama-server weight files exist before the process starts.

A KeepAlive service that execs llama-server directly will crash-loop after a
reboot if a required file (often the vision projector) was deleted while the
previous process was still running. That process did not need the file on
disk once it had loaded. The next start does.

This wrapper checks `-m`, `--mmproj`, and draft (`-md`) paths, copies a
missing file from a backup directory when a same-named spare is there, and
can fetch the published MiniCPM-V 4.7 4x projector by basename. If a file
is still missing it waits and logs instead of starting llama-server.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


WEIGHT_FLAGS = ("-m", "--mmproj", "-md", "--model-draft", "--draft-model")


def argv_weight_paths(argv: list[str]) -> list[str]:
    """Return weight file paths named by llama-server flags."""
    paths: list[str] = []
    i = 0
    while i < len(argv):
        item = argv[i]
        if item in WEIGHT_FLAGS:
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                paths.append(argv[i + 1])
                i += 2
                continue
        for prefix in ("--mmproj=", "-md=", "--model-draft=", "--draft-model=", "-m="):
            if item.startswith(prefix) and item.split("=", 1)[1]:
                paths.append(item.split("=", 1)[1])
                break
        i += 1
    return paths


def present(path: str | Path) -> bool:
    target = Path(path)
    try:
        return target.is_file() and target.stat().st_size > 0
    except OSError:
        return False


def _log(message: str, log_file: Path | None = None) -> None:
    line = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + " " + message
    print(line, file=sys.stderr, flush=True)
    if log_file is None:
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def restore(path: str, backup_dir: str, log_file: Path | None = None) -> bool:
    if present(path):
        return True
    if not backup_dir:
        return False
    spare = Path(backup_dir) / Path(path).name
    if not present(spare):
        return False
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(spare, dest)
    ok = present(dest)
    _log(("restored " if ok else "restore-failed ") + dest.name + " from backup", log_file)
    return ok


def parse_fetch(spec: str) -> tuple[str, str, int | None]:
    """Parse `basename=url` or `basename=url:bytes` (bytes are trailing digits)."""
    if "=" not in spec:
        raise ValueError("fetch spec must be name=url or name=url:bytes")
    name, rest = spec.split("=", 1)
    if not name or not rest:
        raise ValueError("fetch spec must be name=url or name=url:bytes")
    url, size = rest, None
    if "://" in rest and ":" in rest.rsplit("://", 1)[-1]:
        left, maybe = rest.rsplit(":", 1)
        if maybe.isdigit() and "://" in left:
            url, size = left, int(maybe)
    return name, url, size


def fetch(path: str, url: str, expected: int | None = None, log_file: Path | None = None) -> bool:
    if present(path):
        return True
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".partial")
    _log("fetch " + dest.name, log_file)
    try:
        urllib.request.urlretrieve(url, part)
        size = part.stat().st_size
        if expected is not None and size != expected:
            part.unlink(missing_ok=True)
            _log(f"fetch-failed {dest.name} size={size} expected={expected}", log_file)
            return False
        part.replace(dest)
    except OSError as exc:
        part.unlink(missing_ok=True)
        _log(f"fetch-failed {dest.name} {exc}", log_file)
        return False
    ok = present(dest)
    _log(("fetched " if ok else "fetch-failed ") + dest.name, log_file)
    return ok


def ensure(paths: list[str], backup_dir: str = "", fetches: dict[str, tuple[str, int | None]] | None = None, log_file: Path | None = None) -> list[str]:
    """Try restore and fetch once. Return paths that are still missing."""
    fetches = fetches or {}
    missing: list[str] = []
    for path in paths:
        if present(path):
            continue
        if restore(path, backup_dir, log_file):
            continue
        name = Path(path).name
        if name in fetches:
            url, size = fetches[name]
            if fetch(path, url, size, log_file):
                continue
        missing.append(path)
    return missing


def wait_until_ready(
    paths: list[str],
    backup_dir: str = "",
    fetches: dict[str, tuple[str, int | None]] | None = None,
    poll_s: float = 15,
    log_file: Path | None = None,
    max_attempts: int | None = None,
) -> list[str]:
    attempts = 0
    while True:
        missing = ensure(paths, backup_dir, fetches, log_file)
        if not missing:
            if attempts:
                _log("required weights present after wait", log_file)
            return []
        attempts += 1
        _log("waiting for missing weights: " + " ".join(missing), log_file)
        if max_attempts is not None and attempts >= max_attempts:
            return missing
        time.sleep(max(0.1, float(poll_s)))


def wrap_model_command(python: str, model_cmd: list[str], backup_dir: str = "weight-backup") -> list[str]:
    """Prefix a llama-server command with this wrapper."""
    from hailports_setup.models import VISION_MMPROJ, VISION_MMPROJ_BYTES

    extra: list[str] = [python, "-m", "hailports_setup.weight_preflight", "--backup-dir", backup_dir]
    if "--mmproj" in model_cmd or any(part.startswith("--mmproj=") for part in model_cmd):
        spec = f"{VISION_MMPROJ}:{VISION_MMPROJ_BYTES}"
        extra += ["--fetch", f"mmproj.gguf={spec}", "--fetch", f"mmproj-model-f16-4x.gguf={spec}"]
    return extra + ["--", *model_cmd]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait for llama-server weight files, then exec the server.")
    parser.add_argument("--backup-dir", default="weight-backup")
    parser.add_argument("--poll-s", type=float, default=15)
    parser.add_argument("--fetch", action="append", default=[], help="basename=url or basename=url:bytes")
    parser.add_argument("--log", default="")
    parser.add_argument("--check", action="store_true", help="try once and exit; do not exec")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("a llama-server command is required after --", file=sys.stderr)
        return 2
    fetches: dict[str, tuple[str, int | None]] = {}
    for spec in args.fetch:
        name, url, size = parse_fetch(spec)
        fetches[name] = (url, size)
    log_file = Path(args.log) if args.log else None
    paths = argv_weight_paths(command)
    if args.check:
        missing = ensure(paths, args.backup_dir, fetches, log_file)
        return 0 if not missing else 1
    missing = wait_until_ready(paths, args.backup_dir, fetches, args.poll_s, log_file)
    if missing:
        return 1
    os.execvp(command[0], command)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
