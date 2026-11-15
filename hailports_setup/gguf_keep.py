"""Paths a free-space GGUF prune must keep.

A prune that only keeps the ``-m`` weight will delete the vision projector.
That file is larger than a 500 MB cutoff and is passed as ``--mmproj``, so it
does not match a ``-m`` search. The server can keep running after the unlink.
The next start is the one that fails.

Read the paths from the server command and from the extra-args file. Do this
even when the server is stopped, so the projector is still on disk at boot.
"""

from __future__ import annotations

import os
import re

from hailports_setup.weight_preflight import argv_weight_paths

_GGUF = re.compile(r"\S+\.gguf")


def keep_paths(argv: list[str], extra_text: str = "") -> list[str]:
    """Absolute paths named by llama-server weight flags or by extra args."""
    found = list(argv_weight_paths(argv))
    found.extend(_GGUF.findall(extra_text))
    out: list[str] = []
    seen: set[str] = set()
    for path in found:
        full = os.path.realpath(path)
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out
