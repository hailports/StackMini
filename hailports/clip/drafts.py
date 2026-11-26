"""Recent Paiste drafts. The menu lists them on right-click. Nothing is sent.

The window text behind a draft is kept on this Mac for 7 days so Redo can rewrite it.
A scene that looks like it holds a secret is not kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEEP = 30
MENU = 12
SCENE_CAP = 16 * 1024
SCENE_DAYS = 7
LISTED = ("id", "at", "preview", "app", "hop", "text", "read", "check")


def drafts_path() -> Path:
    raw = os.environ.get("PAISTE_DRAFTS") or ""
    return Path(raw) if raw else Path.home() / ".config" / "paiste" / "drafts.jsonl"


def _preview(text: str, width: int = 80) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= width else line[: width - 1].rstrip() + "…"


def _shrink(value, width: int):
    if isinstance(value, str):
        return value if len(value) <= width else value[-width:]
    if isinstance(value, list):
        return [_shrink(item, width) for item in value[-60:]]
    if isinstance(value, dict):
        return {key: _shrink(item, width) for key, item in value.items()}
    return value


def cap_scene(scene, cap: int = SCENE_CAP) -> dict | None:
    """Keep the scene for a real redo. Long text keeps its tail, where the ask usually is."""
    if not isinstance(scene, dict) or not scene:
        return None
    try:
        if len(json.dumps(scene, ensure_ascii=False)) <= cap:
            return scene
        for width in (6000, 3000, 1500, 800, 400, 200):
            small = _shrink(scene, width)
            if len(json.dumps(small, ensure_ascii=False)) <= cap:
                return small
    except (TypeError, ValueError):
        return None
    return None


# Numbers a reader should check before sending: order and tracking numbers, amounts, times, dates.
_CHECK = re.compile(
    r"(?i)(?:\$\s?\d[\d,]*(?:\.\d{2})?"
    r"|#\s?[A-Z0-9][A-Z0-9-]{2,}"
    r"|\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b"
    r"|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\b\d[\d,.-]{3,}\d\b"
    r"|\b\d+(?:\.\d+)?\s?%)")


def check_items(text: str, limit: int = 6) -> list[str]:
    """The numbers, dates and times in a draft, in order, for a 'check these' hint. Nothing else."""
    out: list[str] = []
    for m in _CHECK.finditer(str(text or "")):
        item = " ".join(m.group(0).split()).rstrip(".,")
        if len(item) > 28:
            item = item[:27] + "…"
        if item and item.lower() not in [o.lower() for o in out]:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _window_chars(scene: dict) -> int:
    total = len(str(scene.get("field_text") or "")) + len(str(scene.get("window_ocr") or ""))
    for block in scene.get("blocks") or []:
        if isinstance(block, dict):
            total += len(str(block.get("text") or ""))
    return total


def receipt(scene, secret: bool = False, lookups: int = 0, files: list[str] | None = None) -> list[str]:
    """What paisty read for one draft, in plain lines for the menu. Kept in drafts.jsonl on this Mac only."""
    if not isinstance(scene, dict) or not scene:
        return []
    lines = []
    app = str(scene.get("app") or "").strip()
    title = " ".join(str(scene.get("title") or "").split())[:60]
    if app or title:
        same = title.lower() == app.lower()
        lines.append("App: " + (app or "?") + (f" ({title})" if title and not secret and not same else ""))
    picked = " ".join(str(scene.get("selection") or "").split())
    if picked and secret:
        lines.append(f"Selected text: {len(picked):,} characters")
    elif picked:
        shown = picked if len(picked) <= 120 else picked[:119].rstrip() + "…"
        lines.append(f"Selected text ({len(picked):,} characters): {shown}")
    else:
        lines.append("Selected text: none")
    lines.append(f"Window text: {_window_chars(scene):,} characters")
    from hailports.clip.scene import OFFSCREEN
    hidden = sum(len(str(b.get("text") or "")) for b in scene.get("blocks") or []
                 if isinstance(b, dict) and b.get("role") == OFFSCREEN)
    if hidden:
        lines.append(f"Text scrolled out of view: {hidden:,} characters (read without scrolling)")
    images = scene.get("images") or []
    if isinstance(images, list) and images:
        lines.append(f"Screenshots: {len(images)}")
    names = [str(n) for n in (files or []) if str(n).strip()]
    if names and secret:
        lines.append(f"Files read: {len(names)}")
    elif names:
        shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
        lines.append("Files read: " + shown)
    if lookups:
        lines.append(f"Lookups: {lookups} fact{'s' if lookups != 1 else ''}")
    lines.append("This stays on this Mac.")
    return lines


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)


def scene_secret(scene) -> bool:
    """True when any scene text trips the existing secret checks."""
    from hailports.chat_badge import secret_hold
    from hailports.clip.board import _SECRET_NOTE
    for text in _strings(scene):
        if text and (_SECRET_NOTE.search(text) or secret_hold(text)):
            return True
    return False


def _expired(row: dict, now: datetime) -> bool:
    try:
        at = datetime.fromisoformat(str(row.get("at") or ""))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return now - at > timedelta(days=SCENE_DAYS)


def load(path: Path | None = None) -> list[dict]:
    path = path or drafts_path()
    rows: list[dict] = []
    now = datetime.now(timezone.utc)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("id") and isinstance(row.get("text"), str):
            if "scene" in row and _expired(row, now):
                row.pop("scene", None)
            rows.append(row)
    return rows


def append(text: str, hop: str = "", app: str = "", path: Path | None = None, keep: int = KEEP,
           scene: dict | None = None, lookups: int = 0, files: list[str] | None = None) -> dict | None:
    text = str(text or "").strip()
    if not text:
        return None
    path = path or drafts_path()
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    digest = hashlib.sha1((at + "\n" + text).encode("utf-8")).hexdigest()[:10]
    row = {"id": at.replace(":", "").replace("+0000", "Z") + "-" + digest, "at": at, "text": text,
           "hop": str(hop or ""), "app": str(app or ""), "preview": _preview(text)}
    secret = scene_secret(scene)
    read = receipt(scene, secret=secret, lookups=lookups, files=files)
    if read:
        row["read"] = read
    check = check_items(text)
    if check:
        row["check"] = check
    kept = None if secret else cap_scene(scene)
    if kept is not None:
        row["scene"] = kept
    rows = (load(path) + [row])[-keep:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for item in rows:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return row


def recent(limit: int = MENU, path: Path | None = None) -> list[dict]:
    return list(reversed(load(path)))[:limit]


def find(draft_id: str, path: Path | None = None) -> dict | None:
    for row in reversed(load(path)):
        if row.get("id") == draft_id:
            return row
    return None


def listed(rows: list[dict]) -> list[dict]:
    """The menu list. The scene stays in the file."""
    return [{key: row.get(key, "") for key in LISTED} for row in rows]


def cmd(action: str, args, cfg: dict | None = None) -> int:
    """`drafts` prints newest-first JSON. `redo --id X` writes a new draft from that row's scene."""
    if action == "drafts":
        print(json.dumps(listed(recent()), ensure_ascii=False))
        return 0
    draft_id = str(getattr(args, "id", "") or "").strip()
    row = find(draft_id) if draft_id else (recent(1) or [None])[0]
    if not row:
        print("no draft")
        return 2
    scene = row.get("scene")
    if not isinstance(scene, dict) or not scene:
        print("no scene")
        return 2
    from hailports.clip import run as clip_run
    llm = (cfg or {}).get("llm") or {}
    return clip_run.draft_scene(
        dict(scene),
        str(llm.get("base_url") or ""),
        str(llm.get("model") or "local"),
        bool(getattr(args, "copy", False)),
        cfg,
    )
