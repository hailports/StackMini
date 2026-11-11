"""Per-surface health for hailports doctor. Tokens are not printed."""

from __future__ import annotations

from pathlib import Path

from hailports.integrations.items import SURFACES
from hailports.integrations.store import TokenBox, read_status

NEEDS_TOKEN = {
    "gmail": "google",
    "gcal": "google",
    "graph": "graph",
    "imap": "imap",
    "caldav": "caldav",
    "carddav": "carddav",
}


def integration_checks(cfg: dict, root: Path | None = None) -> list[tuple[str, bool, str]]:
    block = (cfg or {}).get("integrations") or {}
    kind = str(block.get("kind") or "off")
    if kind in ("", "off"):
        return []
    status = read_status(Path(root) / "integrations") if root else {}
    box = TokenBox(Path(root) / "integrations") if root else None
    checks = []
    for surface in SURFACES:
        item = block.get(surface) if isinstance(block.get(surface), dict) else {}
        if not item or not item.get("enabled"):
            checks.append((f"integration-{surface}", True, "off"))
            continue
        provider = str(item.get("provider") or "")
        via = str(item.get("via") or kind)
        ok = True
        detail = via + (" " + provider if provider else "")
        if provider == "files" or via == "native":
            raw_path = str(item.get("path") or "")
            if not raw_path or not Path(raw_path).exists():
                ok = False
                detail = "missing " + surface
        token_name = NEEDS_TOKEN.get(provider)
        if token_name and box is not None and not box.get(token_name):
            ok = False
            detail = "no token"
        row = status.get(surface) if isinstance(status.get(surface), dict) else {}
        if row.get("proof") == "fail":
            ok = False
            detail = (provider or via) + " proof fail"
        elif row.get("proof") == "pass" and ok:
            detail = (provider or via) + " proof pass"
        checks.append((f"integration-{surface}", ok, detail.strip()))
    return checks
