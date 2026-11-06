"""Stage files on an unsent Zoom draft.

This is the attachment entry for the draft stager. It calls the raw CDP client
and returns. It does not type, and it does not fall through to a keyboard path.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile


def stage_attachment(session_id, text, paths, *, supersede=False, node_bin="node"):
    """Put files on the open draft composer. Never sends the draft."""
    if not session_id or not str(text or "").strip():
        raise RuntimeError("attachment staging needs a session and text")
    files = [str(path) for path in (paths or []) if str(path or "").strip()]
    if not files:
        raise RuntimeError("attachment staging needs a file")
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False)
    try:
        handle.write(text)
        handle.close()
        args = ["save", "--session-id", str(session_id), "--text-file", handle.name]
        if supersede:
            args.append("--supersede")
        for extra in files:
            args.extend(["--attach", extra])
        env = os.environ.copy()
        env.setdefault("ZOOM_WEB_CDP", "http://127.0.0.1:18812")
        proc = subprocess.run(
            [node_bin, "tools/zoom_write.js", *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    lines = (proc.stdout or "").strip().splitlines()
    payload = {}
    if lines:
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError:
            payload = {}
    attachment = payload.get("attachment") if isinstance(payload, dict) else None
    proven = (
        isinstance(payload, dict)
        and payload.get("ok") is True
        and payload.get("sent") is False
        and isinstance(attachment, dict)
        and attachment.get("chip") is True
        and attachment.get("historyChanged") is not True
        and str(attachment.get("sendTime")) in ("0", "0.0")
    )
    if proc.returncode != 0 or not proven:
        raise RuntimeError("attachment was not left unsent in the composer")
    return payload
