"""Let this process read cloud-only (dataless) synced-drive files on macOS.

Jobs started by launchd or over ssh can inherit an I/O policy that forbids
materializing dataless files. A plain read of a cloud-only placeholder then
fails with OSError EDEADLK ("Resource deadlock avoided") instead of
downloading the file. Call allow_materialize() once at the start of the job.

It is a no-op off macOS and never raises.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import sys

IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES = 3
IOPOL_SCOPE_PROCESS = 0
IOPOL_MATERIALIZE_DATALESS_FILES_ON = 2


def _libc():
    path = ctypes.util.find_library("System") or ctypes.util.find_library("c")
    return ctypes.CDLL(path, use_errno=True) if path else None


def allow_materialize(platform: str | None = None, lib=None) -> bool:
    """Turn on dataless-file materialization for this process. True on success."""
    if (platform or sys.platform) != "darwin":
        return False
    try:
        lib = lib or _libc()
        if lib is None:
            return False
        rc = lib.setiopolicy_np(
            IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
            IOPOL_SCOPE_PROCESS,
            IOPOL_MATERIALIZE_DATALESS_FILES_ON,
        )
        return rc == 0
    except Exception:
        return False


def is_placeholder_error(exc: BaseException) -> bool:
    """True when an OSError is the cloud-only placeholder refusal."""
    return isinstance(exc, OSError) and exc.errno == errno.EDEADLK


if __name__ == "__main__":
    print("materialize:", "on" if allow_materialize() else "unchanged")
