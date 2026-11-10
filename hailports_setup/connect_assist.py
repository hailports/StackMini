"""Setup choice for native apps, or loopback bridges drafted by the user's subscription.

The subscription call gets the operating system, the bridge names, and the ports.
It does not get mail, notes, chats, or the API key. A URL that is not loopback
or an overlay address is not kept.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from hailports.local_bridges.fabric import KINDS, _host_ok, enabled_specs

SURFACES = ("email", "notes", "chats")
DEFAULT_PORTS = {"mail": 9222, "meetings": 9223, "tracker": 9224, "cdp": 9222}
_URL = re.compile(r"https?://[^\s]+")


def default_bridges(enabled, ports: dict | None = None) -> dict:
    wanted = set(enabled or [])
    use = dict(DEFAULT_PORTS)
    if ports:
        use.update(ports)
    out = {}
    for name in KINDS:
        port = int(use.get(name) or DEFAULT_PORTS[name])
        out[name] = {"enabled": name in wanted, "base_url": f"http://127.0.0.1:{port}"}
    return out


def parse_kinds(text: str) -> list[str]:
    wanted = []
    for part in str(text or "").replace(" ", ",").split(","):
        name = part.strip().lower()
        if name in KINDS and name not in wanted:
            wanted.append(name)
    return wanted


def parse_ports(text: str) -> dict[str, int]:
    ports = dict(DEFAULT_PORTS)
    parts = [part.strip() for part in str(text or "").split(",") if part.strip()]
    named = {}
    positional = []
    for part in parts:
        if "=" in part:
            key, raw = part.split("=", 1)
            key = key.strip().lower()
            if key not in KINDS:
                raise ValueError("unknown bridge " + key)
            named[key] = _port(raw)
        else:
            positional.append(_port(part))
    if positional and len(positional) not in (1, len(KINDS)):
        raise ValueError("port list must be one number or four numbers")
    if len(positional) == 1:
        for name in KINDS:
            ports[name] = positional[0]
    elif len(positional) == len(KINDS):
        for name, value in zip(KINDS, positional):
            ports[name] = value
    ports.update(named)
    return ports


def _port(raw: str) -> int:
    value = int(str(raw).strip())
    if value < 1 or value > 65535:
        raise ValueError("port out of range")
    return value


def _office(exe: str, label: str) -> list[tuple[str, str]]:
    roots = []
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(key)
        if value:
            roots.append(value)
    roots.extend([r"C:\Program Files", r"C:\Program Files (x86)"])
    found = []
    seen = set()
    for root in roots:
        for version in ("Office16", "Office15"):
            for path in (
                str(Path(root) / "Microsoft Office" / "root" / version / exe),
                str(Path(root) / "Microsoft Office" / version / exe),
            ):
                if path not in seen:
                    seen.add(path)
                    found.append((label, path))
    return found


def app_candidates(os_name: str) -> dict[str, list[tuple[str, str]]]:
    if os_name == "darwin":
        return {
            "email": [("Mail", "/System/Applications/Mail.app"), ("Mail", "/Applications/Mail.app")],
            "notes": [("Notes", "/System/Applications/Notes.app"), ("Notes", "/Applications/Notes.app")],
            "chats": [("Messages", "/System/Applications/Messages.app"), ("Messages", "/Applications/Messages.app")],
        }
    if os_name == "windows":
        return {
            "email": _office("OUTLOOK.EXE", "Outlook"),
            "notes": _office("ONENOTE.EXE", "OneNote"),
            "chats": [],
        }
    return {"email": [], "notes": [], "chats": []}


def detect_native(os_name: str, exists=None) -> dict:
    check = exists or (lambda path: Path(path).exists())
    found = {}
    for surface, options in app_candidates(os_name).items():
        hit = None
        for app, path in options:
            if check(path):
                hit = {"enabled": True, "app": app, "path": path}
                break
        if hit:
            found[surface] = hit
        else:
            found[surface] = {
                "enabled": False,
                "app": "",
                "path": "",
                "reason": _missing_reason(os_name, surface),
            }
    return found


def _missing_reason(os_name: str, surface: str) -> str:
    if os_name == "linux":
        return "no native " + surface + " app is wired on Linux"
    if os_name == "windows" and surface == "chats":
        return "no native chat app is wired on Windows"
    if os_name == "windows":
        return "Outlook or OneNote was not found on disk"
    return "the app was not found in the usual folder"


def permission_lines(os_name: str, found: dict) -> list[str]:
    enabled = [name for name in SURFACES if (found.get(name) or {}).get("enabled")]
    lines = []
    if os_name == "darwin" and enabled:
        names = ", ".join(str(found[name].get("app") or name) for name in enabled)
        lines.append(
            "To finish native access for "
            + names
            + ", open System Settings, Privacy and Security, Automation, and allow this terminal to control those apps."
        )
    if os_name == "windows" and enabled:
        lines.append("The program file is on disk. Setup does not start it.")
    if os_name == "windows" and not (found.get("chats") or {}).get("enabled"):
        lines.append("No native chat app is wired on Windows. Bridges can cover chat.")
    if not enabled:
        lines.append("No native mail, notes, or chat app was found. Choose bridges to connect those on this computer.")
    lines.append("Setup does not open the apps and does not read mail, notes, or chats.")
    return lines


def _url_ok(url: str) -> bool:
    if not url or "@" in url or any(ch.isspace() for ch in url):
        return False
    return _host_ok(url)


def _load_json(text: str):
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


def _bad_step(text: str) -> bool:
    low = text.lower()
    if any(word in low for word in ("token", "password", "api key", "secret")):
        return True
    for match in _URL.finditer(text):
        if not _url_ok(match.group(0).rstrip(".,)")):
            return True
    return False


def clean_steps(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    kept = []
    for item in raw[:6]:
        text = " ".join(str(item).split())
        if not text or len(text) > 180 or _bad_step(text):
            continue
        kept.append(text)
    return kept


def builtin_bridge_steps(enabled) -> list[str]:
    lines = []
    for name in enabled:
        lines.append(f"Keep the {name} bridge on 127.0.0.1. Setup does not open a browser and does not send.")
    lines.append("hailports doctor checks that each enabled bridge stays on this computer.")
    return lines[:6]


def build_prompt(os_name: str, enabled: list[str], ports: dict[str, int]) -> str:
    port_text = ", ".join(f"{name}={int(ports[name])}" for name in KINDS)
    return "\n".join([
        "Reply with one JSON object and no other text.",
        'Shape: {"bridges": {"mail": {"enabled": false, "base_url": "http://127.0.0.1:9222"}}, "steps": ["short line"]}',
        "Include mail, meetings, tracker, and cdp.",
        "Enabled kinds: " + ", ".join(enabled),
        "Ports: " + port_text,
        "Operating system: " + os_name,
        "Every base_url must use 127.0.0.1 or localhost.",
        "steps is at most 4 short lines about those loopback URLs.",
        "Do not include keys, tokens, mailbox text, notes, or chat text.",
    ])


def parse_bridge_reply(text: str, enabled: list[str], ports: dict[str, int]) -> dict | None:
    doc = _load_json(text)
    if not isinstance(doc, dict):
        return None
    block = doc.get("bridges") if isinstance(doc.get("bridges"), dict) else doc
    if not isinstance(block, dict):
        return None
    wanted = set(enabled)
    bridges = {}
    for name in KINDS:
        item = block.get(name) if isinstance(block.get(name), dict) else {}
        url = str(item.get("base_url") or "").strip()
        port = int(ports.get(name) or DEFAULT_PORTS[name])
        if not _url_ok(url):
            url = f"http://127.0.0.1:{port}"
        if not _url_ok(url):
            return None
        bridges[name] = {"enabled": name in wanted, "base_url": url}
    steps = clean_steps(doc.get("steps")) or builtin_bridge_steps(enabled)
    return {"bridges": bridges, "steps": steps, "source": "subscription"}


def fallback_plan(os_name: str, enabled: list[str], ports: dict[str, int], source: str) -> dict:
    del os_name
    return {
        "bridges": default_bridges(enabled, ports),
        "steps": builtin_bridge_steps(enabled),
        "source": source,
    }


def assist_bridges(cloud, *, os_name: str, enabled: list[str], ports: dict[str, int], secret: str = "") -> dict:
    fallback = fallback_plan(os_name, enabled, ports, "defaults")
    prompt = build_prompt(os_name, enabled, ports)
    if cloud is None:
        return fallback
    if secret and len(secret) >= 6 and secret in prompt:
        return fallback
    try:
        reply = cloud.chat([{"role": "user", "content": prompt}], max_tokens=500, temperature=0)
        text = str(getattr(reply, "text", "") or "")
    except Exception:
        return fallback
    if secret and len(secret) >= 6 and secret in text:
        return fallback
    parsed = parse_bridge_reply(text, enabled, ports)
    if parsed is None:
        return fallback
    return parsed


def _json_dict(raw) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def apply_connection(cfg: dict, answers: dict, os_name: str) -> None:
    kind = str((answers or {}).get("connect") or "skip")
    if kind == "native":
        native = _json_dict((answers or {}).get("native_json")) or detect_native(os_name)
        block = {"kind": "native"}
        for surface in SURFACES:
            item = native.get(surface) if isinstance(native.get(surface), dict) else {}
            block[surface] = {
                "enabled": bool(item.get("enabled")),
                "app": str(item.get("app") or ""),
                "path": str(item.get("path") or ""),
            }
        cfg["integrations"] = block
        cfg["local_bridges"] = default_bridges([])
        if os_name == "darwin" and block["chats"]["enabled"]:
            cfg.setdefault("modules", {})
            cfg["modules"]["imessage"] = True
        return
    if kind == "bridges":
        plan = _json_dict((answers or {}).get("bridge_plan_json")) or {}
        bridges = plan.get("bridges") if isinstance(plan.get("bridges"), dict) else None
        if not isinstance(bridges, dict):
            bridges = default_bridges(parse_kinds(str((answers or {}).get("bridge_kinds") or "")))
        cleaned = {}
        for name in KINDS:
            item = bridges.get(name) if isinstance(bridges.get(name), dict) else {}
            url = str(item.get("base_url") or "").strip()
            if not _url_ok(url):
                url = f"http://127.0.0.1:{DEFAULT_PORTS[name]}"
            cleaned[name] = {"enabled": bool(item.get("enabled")) and _url_ok(url), "base_url": url}
        cfg["integrations"] = {"kind": "bridges", "assist": str(plan.get("source") or "defaults")}
        cfg["local_bridges"] = cleaned
        return
    cfg["integrations"] = {"kind": "off"}
    cfg["local_bridges"] = default_bridges([])


def describe(cfg: dict) -> str:
    kind = str((cfg.get("integrations") or {}).get("kind") or "off")
    if kind == "native":
        block = cfg.get("integrations") or {}
        on = [name for name in SURFACES if (block.get(name) or {}).get("enabled")]
        if not on:
            return "Connections: native, no installed app was found."
        return "Connections: native " + ", ".join(on) + "."
    if kind == "bridges":
        names = [spec.name for spec in enabled_specs(cfg)]
        assist = str((cfg.get("integrations") or {}).get("assist") or "defaults")
        listed = ", ".join(names) if names else "none"
        return f"Connections: bridges {listed} ({assist})."
    return "Connections: off."


def connection_status(cfg: dict) -> tuple[bool, str]:
    kind = str((cfg.get("integrations") or {}).get("kind") or "off")
    if kind == "off":
        return True, "not configured"
    if kind == "native":
        block = cfg.get("integrations") or {}
        on = []
        missing = []
        for name in SURFACES:
            item = block.get(name) or {}
            if not item.get("enabled"):
                continue
            on.append(name)
            path = str(item.get("path") or "")
            if not path or not Path(path).exists():
                missing.append(name)
        if not on:
            return True, "native none installed"
        if missing:
            return False, "missing " + ",".join(missing)
        return True, "native " + ",".join(on)
    if kind == "bridges":
        names = [spec.name for spec in enabled_specs(cfg)]
        if not names:
            return False, "bridges selected and none are enabled"
        assist = str((cfg.get("integrations") or {}).get("assist") or "defaults")
        return True, "bridges " + ",".join(names) + " " + assist
    return False, "unknown connection kind"
