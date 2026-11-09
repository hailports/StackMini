#!/usr/bin/env python3
"""Keep an iPhone remote canvas clear of its on-screen keyboard.

Narrower than a display controller:

* fresh, identity-gated session state only
* acts only for mobile_connected=1 and mobile_profile=iphone
* the focused display must be 768x1024 points
* changes only yabai's temporary global bottom_padding
* snapshots that exact prior value and restores it on the next positively
  identified non-phone state
* clamps floating windows it changed to y<=761, and restores only those frames

Missing, stale, malformed, or future-dated state is a no-op. A missing
yabai is a no-op.

Geometry, measured on the 768x1024 profile: the window bottom is y=761.
The keyboard and accessory row begins near y=852, which leaves 91 points
of breathing room. Usable bottom was 963, so the padding is 963-761=202.

Per-space padding is not used. yabai remembers a per-space override and
has no command to clear that flag. The global value is restored exactly.

Session file format (one KEY=VALUE per line):

    updated=<unix seconds>
    mobile_connected=1
    mobile_profile=iphone

`mobile_connected=0` is a positive non-phone state. `mobile_profile=ipad`
with `mobile_connected=1` is also non-phone. Any other profile while
connected is unknown, which does not change padding.

The Mac path is ~/Library/Application Support/Remote4by3/mobile-session-state.env.
A hailports install uses ~/Library/Application Support/hailports/mobile-session-state.env.
Set REMOTE_SESSION_STATE_FILE to point at either file, and
REMOTE_SESSION_STATE_EXTRA (path separator) for more copies. The freshest
file inside the 45 second window wins.
Set REMOTE_LINK_STATE_FILE to a JSON object with metrics.ts and viewer when
the session file is not being written yet. That JSON is used only when no
session file is fresh.
"""

from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # Windows has no fcntl; the reflow is a macOS feature.
    fcntl = None
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional


STATE_VERSION = 2
STATE_TTL_SECONDS = 45.0
PHONE_WIDTH = 768
PHONE_HEIGHT = 1024
PHONE_SAFE_BOTTOM = 761
PHONE_PADDING = 202
USABLE_BOTTOM = 963
KEYBOARD_ROW_Y = 852
BREATHING_ROOM = KEYBOARD_ROW_Y - PHONE_SAFE_BOTTOM
FRAME_TOLERANCE = 2

IPHONE_RX_TEXT = ("iphone", "ipod", "phone")


def geometry() -> dict[str, int]:
    return {
        "phone_width": PHONE_WIDTH,
        "phone_height": PHONE_HEIGHT,
        "safe_bottom": PHONE_SAFE_BOTTOM,
        "padding": PHONE_PADDING,
        "usable_bottom": USABLE_BOTTOM,
        "keyboard_row_y": KEYBOARD_ROW_Y,
        "breathing_room": BREATHING_ROOM,
    }


def _check_geometry() -> None:
    if USABLE_BOTTOM - PHONE_SAFE_BOTTOM != PHONE_PADDING:
        raise RuntimeError("phone padding does not match the measured usable bottom")
    if BREATHING_ROOM != 91:
        raise RuntimeError("phone breathing room drifted")


_check_geometry()


def _truth(value: Any) -> bool:
    return value is True or value == 1 or str(value).lower() == "true"


def _frame(value: Any) -> Optional[dict[str, int]]:
    if not isinstance(value, dict):
        return None
    try:
        return {key: int(round(float(value[key]))) for key in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None


def _same_frame(left: dict[str, int], right: dict[str, int]) -> bool:
    return all(abs(left[key] - right[key]) <= FRAME_TOLERANCE for key in ("x", "y", "w", "h"))


def _token_hit(name: str, tokens: tuple[str, ...]) -> bool:
    text = (name or "").lower()
    for token in tokens:
        start = 0
        while True:
            index = text.find(token, start)
            if index < 0:
                break
            before = text[index - 1] if index else ""
            after_at = index + len(token)
            after = text[after_at] if after_at < len(text) else ""
            if (not before or not before.isalnum()) and (not after or not after.isalnum()):
                return True
            start = index + 1
    return False


def parse_env(text: str | None, now: float, ttl: float = STATE_TTL_SECONDS) -> str:
    """Return iphone, normal, or unknown. `now` is the current unix time."""
    if text is None:
        return "unknown"
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    try:
        updated = float(fields["updated"])
    except (KeyError, ValueError):
        return "unknown"
    if not math.isfinite(updated):
        return "unknown"
    age = now - updated
    if age < -5.0 or age > ttl:
        return "unknown"
    if fields.get("mobile_connected") == "1":
        profile = fields.get("mobile_profile")
        if profile == "iphone":
            return "iphone"
        if profile == "ipad":
            return "normal"
        return "unknown"
    if fields.get("mobile_connected") == "0":
        return "normal"
    return "unknown"


def gate_from_adapter(data: Any, now: float, ttl: float = STATE_TTL_SECONDS) -> str:
    """Read a link-adapter status object. Stale or malformed input is unknown."""
    if not isinstance(data, dict):
        return "unknown"
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    try:
        updated = float(metrics.get("ts"))
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(updated):
        return "unknown"
    age = now - updated
    if age < -5.0 or age > ttl:
        return "unknown"
    viewer = str(data.get("active_viewer") or data.get("viewer") or "")
    if _token_hit(viewer, IPHONE_RX_TEXT):
        return "iphone"
    return "normal"


def session_candidates() -> list[Path]:
    home = Path.home()
    found: list[Path] = []
    primary = os.environ.get("REMOTE_SESSION_STATE_FILE", "").strip()
    if primary:
        found.append(Path(primary))
    extra = os.environ.get("REMOTE_SESSION_STATE_EXTRA", "")
    for part in extra.split(os.pathsep):
        part = part.strip()
        if part:
            found.append(Path(part))
    found.append(home / "Library" / "Application Support" / "hailports" / "mobile-session-state.env")
    found.append(home / "Library" / "Application Support" / "Remote4by3" / "mobile-session-state.env")
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def choose_gate(files: list[tuple[Path, str | None]], now: float, adapter: Any = None) -> tuple[str, Path | None]:
    """Pick the freshest valid session file. Fall back to adapter JSON."""
    best: tuple[float, Path] | None = None
    best_gate = "unknown"
    for path, text in files:
        gate = parse_env(text, now)
        if gate == "unknown" or text is None:
            continue
        updated = _updated_of(text)
        if updated is None:
            continue
        if best is None or updated > best[0]:
            best = (updated, path)
            best_gate = gate
    if best is not None:
        return best_gate, best[1]
    if adapter is not None:
        return gate_from_adapter(adapter, now), None
    return "unknown", files[0][0] if files else None


def _updated_of(text: str) -> float | None:
    for line in text.splitlines():
        if line.startswith("updated="):
            try:
                return float(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


def safe_frame(current: dict[str, int], origin_y: int = 0) -> dict[str, int]:
    top = origin_y
    safe_bottom = origin_y + PHONE_SAFE_BOTTOM
    height = min(current["h"], safe_bottom - top)
    if height < 1:
        height = 1
    y = min(max(current["y"], top), safe_bottom - height)
    return {"x": current["x"], "y": y, "w": current["w"], "h": height}


def render_session(updated: float, connected: str, profile: str) -> str:
    return f"updated={updated:.3f}\nmobile_connected={connected}\nmobile_profile={profile}\n"


def publish_session(path: Path, updated: float, connected: str, profile: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(render_session(updated, connected, profile), encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        return False


class Yabai:
    """Talks to yabai. A missing binary, a timeout, or a bad reply is None."""

    def __init__(self, binary: str | None = None, runner: Callable[[list[str]], str | None] | None = None) -> None:
        self.binary = binary
        self.runner = runner

    def available(self) -> bool:
        return bool(self.binary) or self.runner is not None

    def run(self, *args: str) -> str | None:
        if self.runner is not None:
            try:
                return self.runner(list(args))
            except Exception:
                return None
        if not self.binary:
            return None
        try:
            completed = subprocess.run(
                [self.binary, "-m", *args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=4,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    def query(self, *args: str) -> Any:
        raw = self.run("query", *args)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def bottom_padding(self) -> int | None:
        raw = self.run("config", "bottom_padding")
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    def set_bottom_padding(self, value: int) -> bool:
        if value < 0 or self.run("config", "bottom_padding", str(value)) is None:
            return False
        return self.bottom_padding() == value


def yabai_path() -> str | None:
    override = os.environ.get("REMOTE4BY3_YABAI") or os.environ.get("HAILPORTS_YABAI")
    candidates = [override, "/opt/homebrew/bin/yabai", "/usr/local/bin/yabai", shutil.which("yabai")]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class Controller:
    def __init__(
        self,
        yabai: Yabai | None = None,
        *,
        support_dir: Path | None = None,
        files: list[tuple[Path, str | None]] | None = None,
        adapter: Any = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.yabai = yabai if yabai is not None else Yabai(yabai_path())
        self.files = files
        self.adapter = adapter
        self.now = now or time.time
        self.support_dir = support_dir

    def _files(self) -> list[tuple[Path, str | None]]:
        if self.files is not None:
            return self.files
        loaded = []
        for path in session_candidates():
            try:
                loaded.append((path, path.read_text(encoding="utf-8")))
            except OSError:
                loaded.append((path, None))
        return loaded

    def _adapter(self) -> Any:
        if self.adapter is not None:
            return self.adapter
        raw = os.environ.get("REMOTE_LINK_STATE_FILE", "").strip()
        if not raw:
            return None
        try:
            return json.loads(Path(raw).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def gate(self) -> tuple[str, Path | None]:
        return choose_gate(self._files(), self.now(), self._adapter())

    def _support(self, session_path: Path | None) -> Path:
        if self.support_dir is not None:
            return self.support_dir
        if session_path is not None:
            return session_path.parent
        primary = os.environ.get("REMOTE_SESSION_STATE_FILE", "").strip()
        if primary:
            return Path(primary).parent
        return Path.home() / "Library" / "Application Support" / "hailports"

    def _paths(self, support: Path) -> tuple[Path, Path]:
        return support / "iphone-keyboard-reflow-state.json", support / "iphone-keyboard-reflow.lock"

    def portrait(self) -> dict[str, Any] | None:
        display = self.yabai.query("--displays", "--display")
        if isinstance(display, list):
            display = display[0] if len(display) == 1 else None
        if not isinstance(display, dict):
            return None
        frame = _frame(display.get("frame"))
        if not frame or frame["w"] != PHONE_WIDTH or frame["h"] != PHONE_HEIGHT:
            return None
        try:
            display_index = int(display["index"])
        except (KeyError, TypeError, ValueError):
            return None
        space = self.yabai.query("--spaces", "--space")
        if isinstance(space, list):
            space = space[0] if len(space) == 1 else None
        if not isinstance(space, dict):
            return None
        try:
            space_index = int(space["index"])
            if int(space["display"]) != display_index:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return {"display": display_index, "space": space_index, "frame": frame}

    def would(self) -> str:
        gate, _path = self.gate()
        if not self.yabai.available():
            return "noop"
        if gate == "unknown":
            return "noop"
        if gate == "iphone":
            return "arm" if self.portrait() else "noop"
        return "disarm"

    def status(self) -> dict[str, Any]:
        gate, path = self.gate()
        binary = self.yabai.binary
        padding = self.yabai.bottom_padding() if self.yabai.available() else None
        support = self._support(path)
        state_path, _lock = self._paths(support)
        saved = _load_saved(state_path)
        out: dict[str, Any] = {
            "ok": True,
            "gate": gate,
            "state_file": str(path) if path else "",
            "yabai": binary,
            "yabai_reachable": padding is not None,
            "saved_armed": saved.get("armed") is True,
            "geometry": geometry(),
            "would": self.would() if self.yabai.available() else "noop",
        }
        if gate == "iphone" and self.yabai.available():
            context = self.portrait()
            out["portrait_context"] = bool(context)
            if context:
                out["display"] = context["display"]
                out["space"] = context["space"]
                out["bottom_padding"] = padding
        return out

    def once(self, *, dry_run: bool = False) -> dict[str, Any]:
        gate, path = self.gate()
        base = {"ok": True, "gate": gate, "geometry": geometry(), "yabai": self.yabai.binary}
        if dry_run:
            base["dry_run"] = True
            base["would"] = self.would()
            if self.yabai.available():
                base["bottom_padding"] = self.yabai.bottom_padding()
            return base
        if not self.yabai.available():
            base["noop"] = "yabai-missing"
            return base
        if gate == "unknown":
            base["noop"] = "unknown-state"
            return base
        support = self._support(path)
        state_path, lock_path = self._paths(support)
        try:
            support.mkdir(parents=True, exist_ok=True)
        except OSError:
            base["ok"] = False
            base["reason"] = "cannot create state directory"
            return base
        try:
            if fcntl is None:
                base["noop"] = "unsupported-os"
                return base
            with lock_path.open("a+") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    base["noop"] = "locked"
                    return base
                if gate == "iphone":
                    context = self.portrait()
                    if not context:
                        base["noop"] = "waiting-for-portrait"
                        return base
                    result = _arm(self.yabai, context, state_path, support)
                else:
                    result = _disarm(self.yabai, state_path)
                result["gate"] = gate
                result["geometry"] = geometry()
                return result
        except Exception as error:
            return {"ok": False, "reason": str(error), "gate": gate, "geometry": geometry()}


def _load_saved(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("version") == STATE_VERSION else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_saved(path: Path, support: Path, value: dict[str, Any]) -> bool:
    temporary_name: str | None = None
    try:
        support.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=support, text=True)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(temporary_name, path)
        return True
    except OSError:
        return False
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _new_state(context: dict[str, Any], baseline: int, applied: int) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "padding_scope": "global",
        "armed": True,
        "baseline_bottom_padding": baseline,
        "applied_bottom_padding": applied,
        "display": context["display"],
        "space": context["space"],
        "windows": {},
    }


def _window_is_placeable(window: Any, context: dict[str, Any]) -> bool:
    if not isinstance(window, dict):
        return False
    try:
        if int(window.get("display")) != context["display"] or int(window.get("space")) != context["space"]:
            return False
        int(window["id"])
    except (TypeError, KeyError, ValueError):
        return False
    if not _truth(window.get("is-visible")) or _truth(window.get("is-minimized")) or _truth(window.get("is-native-fullscreen")):
        return False
    subrole = str(window.get("subrole") or "")
    return not subrole or subrole == "AXStandardWindow"


def _windows_on_context(yabai: Yabai, context: dict[str, Any]) -> list[dict[str, Any]]:
    value = yabai.query("--windows")
    if not isinstance(value, list):
        return []
    return [window for window in value if _window_is_placeable(window, context)]


def _window_by_id(yabai: Yabai, window_id: int) -> dict[str, Any] | None:
    value = yabai.query("--windows", "--window", str(window_id))
    return value if isinstance(value, dict) else None


def _run_frame_command(yabai: Yabai, window_id: int, command: str, frame: dict[str, int]) -> bool:
    if command == "resize":
        value = f"abs:{frame['w']}:{frame['h']}"
    else:
        value = f"abs:{frame['x']}:{frame['y']}"
    return yabai.run("window", str(window_id), f"--{command}", value) is not None


def _apply_frame(yabai: Yabai, window_id: int, target: dict[str, int]) -> dict[str, int] | None:
    if not _run_frame_command(yabai, window_id, "resize", target):
        return None
    if not _run_frame_command(yabai, window_id, "move", target):
        return None
    result = _window_by_id(yabai, window_id)
    return _frame(result.get("frame")) if result else None


def _reflow_floating(yabai: Yabai, context: dict[str, Any], saved: dict[str, Any], state_path: Path, support: Path) -> int:
    windows = saved.setdefault("windows", {})
    changed = 0
    safe_bottom = context["frame"]["y"] + PHONE_SAFE_BOTTOM
    for window in _windows_on_context(yabai, context):
        if not _truth(window.get("is-floating")):
            continue
        window_id = int(window["id"])
        current = _frame(window.get("frame"))
        if not current or current["y"] + current["h"] <= safe_bottom + FRAME_TOLERANCE:
            continue
        target = safe_frame(current, context["frame"]["y"])
        if _same_frame(current, target):
            continue
        key = str(window_id)
        previous = windows.get(key)
        has_original = isinstance(previous, dict) and _frame(previous.get("original")) is not None
        if has_original:
            record = dict(previous)
        else:
            record = {"original": current, "display": context["display"], "space": context["space"]}
        record["applied"] = target
        windows[key] = record
        if not _save_saved(state_path, support, saved):
            if has_original:
                windows[key] = previous
            else:
                windows.pop(key, None)
            continue
        actual = _apply_frame(yabai, window_id, target)
        if not actual or actual["y"] + actual["h"] > safe_bottom + FRAME_TOLERANCE:
            _apply_frame(yabai, window_id, current)
            continue
        record["applied"] = actual
        if not _save_saved(state_path, support, saved):
            record["applied"] = target
            _apply_frame(yabai, window_id, current)
            continue
        changed += 1
    return changed


def _tiled_overflow(yabai: Yabai, context: dict[str, Any]) -> int:
    safe_bottom = context["frame"]["y"] + PHONE_SAFE_BOTTOM
    overflow = 0
    for window in _windows_on_context(yabai, context):
        if _truth(window.get("is-floating")):
            continue
        frame = _frame(window.get("frame"))
        if frame:
            overflow = max(overflow, frame["y"] + frame["h"] - safe_bottom)
    return max(0, overflow)


def _arm(yabai: Yabai, context: dict[str, Any], state_path: Path, support: Path) -> dict[str, Any]:
    saved = _load_saved(state_path)
    current_padding = yabai.bottom_padding()
    if current_padding is None:
        return {"ok": False, "reason": "bottom_padding unavailable"}
    if saved.get("armed") is not True:
        desired = max(PHONE_PADDING, current_padding)
        saved = _new_state(context, current_padding, desired)
        if not _save_saved(state_path, support, saved):
            return {"ok": False, "reason": "cannot save baseline"}
    else:
        desired = int(saved.get("applied_bottom_padding", PHONE_PADDING))
        if current_padding != desired:
            saved["baseline_bottom_padding"] = current_padding
            desired = max(PHONE_PADDING, current_padding)
            saved["applied_bottom_padding"] = desired
            if not _save_saved(state_path, support, saved):
                return {"ok": False, "reason": "cannot save edited baseline"}
    if current_padding != desired and not yabai.set_bottom_padding(desired):
        return {"ok": False, "reason": "cannot set bottom_padding"}
    overflow = _tiled_overflow(yabai, context)
    if overflow > FRAME_TOLERANCE:
        desired += int(math.ceil(overflow)) + 8
        saved["applied_bottom_padding"] = desired
        if not _save_saved(state_path, support, saved):
            return {"ok": False, "reason": "cannot save raised padding"}
        if not yabai.set_bottom_padding(desired):
            return {"ok": False, "reason": "cannot raise bottom_padding"}
    saved["applied_bottom_padding"] = desired
    saved["display"] = context["display"]
    saved["space"] = context["space"]
    moved = _reflow_floating(yabai, context, saved, state_path, support)
    if not _save_saved(state_path, support, saved):
        return {"ok": False, "reason": "cannot save applied frames"}
    return {"ok": True, "armed": True, "padding": desired, "moved": moved}


def _restore_frame(yabai: Yabai, window_id: int, original: dict[str, int]) -> bool:
    actual = _apply_frame(yabai, window_id, original)
    return actual is not None and _same_frame(actual, original)


def _disarm(yabai: Yabai, state_path: Path) -> dict[str, Any]:
    saved = _load_saved(state_path)
    if saved.get("armed") is not True:
        return {"ok": True, "armed": False}
    try:
        baseline = int(saved["baseline_bottom_padding"])
        applied = int(saved["applied_bottom_padding"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "reason": "invalid saved baseline"}
    current_padding = yabai.bottom_padding()
    if current_padding is None:
        return {"ok": False, "reason": "bottom_padding unavailable"}
    if current_padding == applied:
        if not yabai.set_bottom_padding(baseline):
            return {"ok": False, "reason": "cannot restore bottom_padding"}
    remaining: dict[str, Any] = {}
    restored = 0
    support = state_path.parent
    for key, record in (saved.get("windows") or {}).items():
        try:
            window_id = int(key)
            expected_display = int(record["display"])
            expected_space = int(record["space"])
            original = _frame(record["original"])
            applied_frame = _frame(record["applied"])
        except (TypeError, KeyError, ValueError):
            continue
        if not original or not applied_frame:
            continue
        window = _window_by_id(yabai, window_id)
        current = _frame(window.get("frame")) if window else None
        if not window or not _truth(window.get("is-floating")):
            continue
        try:
            same_place = int(window.get("display")) == expected_display and int(window.get("space")) == expected_space
        except (TypeError, ValueError):
            same_place = False
        if not same_place or not current or not _same_frame(current, applied_frame):
            continue
        if _restore_frame(yabai, window_id, original):
            restored += 1
        else:
            remaining[key] = record
    if remaining:
        saved["windows"] = remaining
        _save_saved(state_path, support, saved)
        return {"ok": False, "reason": "some frame restores failed", "restored": restored}
    saved.update({"armed": False, "windows": {}})
    if not _save_saved(state_path, support, saved):
        return {"ok": False, "reason": "cannot save restored state", "restored": restored}
    return {"ok": True, "armed": False, "restored": restored}


def render_agent(
    home: Path,
    script: Path,
    session_dir: Path,
    *,
    label: str = "com.hailports.iphone-keyboard-reflow",
    link_state: Path | None = None,
) -> str:
    """Launch agent text. Paths are absolute. The label is a hailports label."""
    env = [f"        <key>REMOTE_SESSION_STATE_FILE</key>\n        <string>{session_dir / 'mobile-session-state.env'}</string>"]
    if link_state is not None:
        env.append(f"        <key>REMOTE_LINK_STATE_FILE</key>\n        <string>{link_state}</string>")
    environment = "\n".join(env)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>-s</string>
    <string>-E</string>
    <string>{script}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
{environment}
  </dict>
  <key>ProcessType</key>
  <string>Background</string>
  <key>RunAtLoad</key>
  <true/>
  <key>WatchPaths</key>
  <array>
    <string>{session_dir}</string>
  </array>
  <key>StartInterval</key>
  <integer>4</integer>
  <key>ThrottleInterval</key>
  <integer>2</integer>
  <key>StandardOutPath</key>
  <string>{home / "Library" / "Logs" / "hailports" / "iphone-keyboard-reflow.out.log"}</string>
  <key>StandardErrorPath</key>
  <string>{home / "Library" / "Logs" / "hailports" / "iphone-keyboard-reflow.err.log"}</string>
</dict>
</plist>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="iPhone keyboard window reflow")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--math", action="store_true", help="Print the calibrated padding and exit.")
    args = parser.parse_args(argv)
    if args.math:
        print(json.dumps(geometry(), sort_keys=True))
        return 0
    controller = Controller()
    if args.status or args.dry_run:
        payload = controller.status() if args.status and not args.dry_run else controller.once(dry_run=True)
        if args.status and args.dry_run:
            payload = controller.once(dry_run=True)
        print(json.dumps(payload, sort_keys=True))
        return 0
    result = controller.once(dry_run=False)
    if not result.get("ok") or result.get("moved") or result.get("restored"):
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
