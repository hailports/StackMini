"""Setup choice for approved APIs, native apps, or loopback bridges.

The subscription call gets the operating system, the bridge names, and the ports.
It does not get mail, calendar, notes, chats, or the API key. A URL that is not
loopback or an overlay address is not kept. Tokens are not written here.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from hailports.local_bridges.fabric import KINDS, _host_ok, enabled_specs

SURFACES = ("email", "calendar", "notes", "chats")
PATHS = ("approved", "bridges", "native", "skip")
SURFACE_KIND = {"email": "mail", "calendar": "meetings", "notes": "tracker", "chats": "cdp"}
KIND_SURFACE = {"mail": "email", "meetings": "calendar", "tracker": "notes", "cdp": "chats"}
APPROVED = {
    "email": {"gmail", "graph", "imap", "apple-mail"},
    "calendar": {"gcal", "graph", "caldav", "apple-calendar"},
    "notes": {"files", "graph", "carddav", "apple-notes"},
    "chats": {"apple-messages"},
}
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
            "calendar": [("Calendar", "/System/Applications/Calendar.app"), ("Calendar", "/Applications/Calendar.app")],
            "notes": [("Notes", "/System/Applications/Notes.app"), ("Notes", "/Applications/Notes.app")],
            "chats": [("Messages", "/System/Applications/Messages.app"), ("Messages", "/Applications/Messages.app")],
        }
    if os_name == "windows":
        outlook = _office("OUTLOOK.EXE", "Outlook")
        return {
            "email": outlook,
            "calendar": list(outlook),
            "notes": _office("ONENOTE.EXE", "OneNote"),
            "chats": [],
        }
    return {"email": [], "calendar": [], "notes": [], "chats": []}


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
    if os_name == "windows" and surface == "calendar":
        return "Outlook was not found on disk"
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
        lines.append("No native mail, calendar, notes, or chat app was found. Choose bridges or an approved API to connect those on this computer.")
    lines.append("Setup records the apps it found and does not open them. hailports integrations sync reads a recent window.")
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


def normalize_adapter(raw, surface: str, port: int) -> dict:
    item = raw if isinstance(raw, dict) else {}
    url = str(item.get("base_url") or "").strip()
    if not _url_ok(url):
        url = f"http://127.0.0.1:{int(port)}"
    export_path = str(item.get("export_path") or "/export").strip()
    if not export_path.startswith("/") or ".." in export_path or "://" in export_path:
        export_path = "/export"
    kind = SURFACE_KIND.get(surface) or "mail"
    steps = clean_steps(item.get("steps")) or builtin_bridge_steps([kind])
    return {
        "surface": surface,
        "base_url": url,
        "export_path": export_path,
        "steps": steps,
        "source": str(item.get("source") or "defaults"),
    }


def build_adapter_prompt(os_name: str, surface: str, port: int) -> str:
    return "\n".join([
        "Reply with one JSON object and no other text.",
        '{"adapter": {"base_url": "http://127.0.0.1:%d", "export_path": "/export", "steps": ["short line"]}}' % int(port),
        "Surface: " + surface,
        "Operating system: " + os_name,
        "base_url must use 127.0.0.1 or localhost.",
        "steps is at most 4 short lines about that loopback URL.",
        "Do not include keys, tokens, mailbox text, notes, or chat text.",
    ])


def build_prompt(os_name: str, enabled: list[str], ports: dict[str, int]) -> str:
    port_text = ", ".join(f"{name}={int(ports[name])}" for name in KINDS)
    return "\n".join([
        "Reply with one JSON object and no other text.",
        'Shape: {"bridges": {"mail": {"enabled": false, "base_url": "http://127.0.0.1:9222"}}, "adapters": {"email": {"base_url": "http://127.0.0.1:9222", "export_path": "/export", "steps": ["short line"]}}, "steps": ["short line"]}',
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
    raw_adapters = doc.get("adapters") if isinstance(doc.get("adapters"), dict) else {}
    adapters = {}
    for kind in wanted:
        surface = KIND_SURFACE.get(kind)
        if not surface:
            continue
        port = int(ports.get(kind) or DEFAULT_PORTS[kind])
        adapters[surface] = normalize_adapter(raw_adapters.get(surface), surface, port)
        adapters[surface]["source"] = "subscription"
    return {"bridges": bridges, "steps": steps, "source": "subscription", "adapters": adapters}


def fallback_plan(os_name: str, enabled: list[str], ports: dict[str, int], source: str) -> dict:
    del os_name
    adapters = {}
    for kind in enabled:
        surface = KIND_SURFACE.get(kind)
        if not surface:
            continue
        adapters[surface] = normalize_adapter({}, surface, int(ports.get(kind) or DEFAULT_PORTS[kind]))
    return {
        "bridges": default_bridges(enabled, ports),
        "steps": builtin_bridge_steps(enabled),
        "source": source,
        "adapters": adapters,
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


def parse_connect_spec(text: str) -> dict:
    raw = str(text or "").strip().lower()
    if raw in ("", "off"):
        return {"mode": "all", "via": "skip", "surfaces": {name: "skip" for name in SURFACES}}
    if raw in PATHS:
        return {"mode": "all", "via": raw, "surfaces": {name: raw for name in SURFACES}}
    surfaces: dict[str, str] = {}
    for part in raw.replace(" ", ",").split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError("use approved, bridges, native, skip, or email=approved")
        name, via = part.split("=", 1)
        name = name.strip()
        via = via.strip()
        if name not in SURFACES:
            raise ValueError("unknown surface " + name)
        if via not in PATHS:
            raise ValueError("unknown path " + via)
        surfaces[name] = via
    if not surfaces:
        raise ValueError("name a surface")
    for name in SURFACES:
        surfaces.setdefault(name, "skip")
    unique = set(surfaces.values())
    if len(unique) == 1:
        return {"mode": "all", "via": unique.pop(), "surfaces": surfaces}
    return {"mode": "per", "via": "mixed", "surfaces": surfaces}


def parse_approve(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(text or "").replace(" ", ",").split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError("provider must look like notes=files")
        name, provider = part.split("=", 1)
        name = name.strip().lower()
        provider = provider.strip().lower()
        if name not in SURFACES:
            raise ValueError("unknown surface " + name)
        if provider not in APPROVED[name]:
            raise ValueError("unknown provider " + provider)
        out[name] = provider
    return out


def kind_for(block: dict) -> str:
    vias = []
    for surface in SURFACES:
        item = block.get(surface) if isinstance(block.get(surface), dict) else {}
        vias.append(str(item.get("via") or "skip"))
    unique = set(vias)
    if not unique or unique == {"skip"}:
        return "off"
    if len(unique) == 1:
        return unique.pop()
    return "mixed"


def _window(answers: dict | None) -> int:
    try:
        value = int((answers or {}).get("window_days") or 30)
    except (TypeError, ValueError):
        value = 30
    return value if value > 0 else 30


def _blank(via: str = "skip", provider: str = "", path: str = "", app: str = "", enabled: bool = False) -> dict:
    return {"enabled": bool(enabled), "via": via, "provider": provider, "app": app, "path": path}


def _native_provider(surface: str, os_name: str) -> str:
    if os_name == "darwin":
        return {
            "email": "apple-mail",
            "calendar": "apple-calendar",
            "notes": "apple-notes",
            "chats": "apple-messages",
        }.get(surface, "")
    if os_name == "windows" and surface in ("email", "calendar"):
        return "outlook"
    if os_name == "windows" and surface == "notes":
        return "onenote"
    return ""


def _clean_bridges(bridges: dict) -> dict:
    cleaned = {}
    for name in KINDS:
        item = bridges.get(name) if isinstance(bridges.get(name), dict) else {}
        url = str(item.get("base_url") or "").strip()
        if not _url_ok(url):
            url = f"http://127.0.0.1:{DEFAULT_PORTS[name]}"
        cleaned[name] = {"enabled": bool(item.get("enabled")) and _url_ok(url), "base_url": url}
    return cleaned


def apply_connection(cfg: dict, answers: dict, os_name: str) -> None:
    spec = parse_connect_spec(str((answers or {}).get("connect") or "skip"))
    if spec["mode"] == "all" and spec["via"] == "native":
        _apply_native(cfg, answers, os_name)
        return
    if spec["mode"] == "all" and spec["via"] == "bridges":
        _apply_bridges(cfg, answers)
        return
    if spec["mode"] == "all" and spec["via"] == "skip":
        cfg["integrations"] = {"kind": "off", "window_days": _window(answers), "mask": []}
        cfg["local_bridges"] = default_bridges([])
        return
    _apply_mapped(cfg, answers, os_name, spec)


def _apply_native(cfg: dict, answers: dict, os_name: str) -> None:
    native = _json_dict((answers or {}).get("native_json")) or detect_native(os_name)
    block = {"kind": "native", "window_days": _window(answers), "mask": []}
    for surface in SURFACES:
        item = native.get(surface) if isinstance(native.get(surface), dict) else {}
        enabled = bool(item.get("enabled"))
        block[surface] = _blank(
            "native",
            _native_provider(surface, os_name) if enabled else "",
            str(item.get("path") or ""),
            str(item.get("app") or ""),
            enabled,
        )
    cfg["integrations"] = block
    cfg["local_bridges"] = default_bridges([])
    if os_name == "darwin" and block["chats"]["enabled"]:
        cfg.setdefault("modules", {})
        cfg["modules"]["imessage"] = True


def _apply_bridges(cfg: dict, answers: dict) -> None:
    plan = _json_dict((answers or {}).get("bridge_plan_json")) or {}
    bridges = plan.get("bridges") if isinstance(plan.get("bridges"), dict) else None
    if not isinstance(bridges, dict):
        bridges = default_bridges(parse_kinds(str((answers or {}).get("bridge_kinds") or "")))
    cfg["integrations"] = {
        "kind": "bridges",
        "assist": str(plan.get("source") or "defaults"),
        "window_days": _window(answers),
        "mask": [],
    }
    cfg["local_bridges"] = _clean_bridges(bridges)


def _apply_mapped(cfg: dict, answers: dict, os_name: str, spec: dict) -> None:
    native = _json_dict((answers or {}).get("native_json")) or {}
    if any(via == "native" for via in spec["surfaces"].values()) and not native:
        native = detect_native(os_name)
    try:
        approve = parse_approve(str((answers or {}).get("approve") or ""))
    except ValueError:
        approve = {}
    notes_dir = str((answers or {}).get("notes_dir") or "")
    bridge_surfaces = [name for name, via in spec["surfaces"].items() if via == "bridges"]
    plan = _json_dict((answers or {}).get("bridge_plan_json")) or {}
    bridges = plan.get("bridges") if isinstance(plan.get("bridges"), dict) else None
    if not isinstance(bridges, dict):
        bridges = default_bridges([SURFACE_KIND[name] for name in bridge_surfaces])
    cleaned = _clean_bridges(bridges)
    for name in KINDS:
        surface = KIND_SURFACE[name]
        if surface not in bridge_surfaces:
            cleaned[name]["enabled"] = False
    block: dict = {"window_days": _window(answers), "mask": []}
    needs_token = {"gmail", "gcal", "graph", "imap", "caldav", "carddav"}
    for surface, via in spec["surfaces"].items():
        if via == "native":
            item = native.get(surface) if isinstance(native.get(surface), dict) else {}
            enabled = bool(item.get("enabled"))
            block[surface] = _blank(
                "native",
                _native_provider(surface, os_name) if enabled else "",
                str(item.get("path") or ""),
                str(item.get("app") or ""),
                enabled,
            )
            continue
        if via == "approved":
            provider = approve.get(surface) or ""
            path = notes_dir if provider == "files" and surface == "notes" else ""
            enabled = False
            if provider == "files":
                enabled = bool(path) and Path(path).is_dir()
            elif provider in ("apple-mail", "apple-notes", "apple-calendar", "apple-messages") and os_name == "darwin":
                enabled = True
            elif provider and provider not in needs_token:
                enabled = True
            block[surface] = _blank("approved", provider, path, "", enabled)
            continue
        if via == "bridges":
            kind = SURFACE_KIND[surface]
            block[surface] = _blank("bridges", "bridge", "", "", bool(cleaned[kind]["enabled"]))
            block[surface]["base_url"] = cleaned[kind]["base_url"]
            continue
        block[surface] = _blank("skip")
    block["kind"] = kind_for(block)
    cfg["integrations"] = block
    cfg["local_bridges"] = cleaned
    chats = block.get("chats") or {}
    if os_name == "darwin" and chats.get("enabled") and chats.get("via") == "native":
        cfg.setdefault("modules", {})
        cfg["modules"]["imessage"] = True


def describe(cfg: dict) -> str:
    kind = str((cfg.get("integrations") or {}).get("kind") or "off")
    block = cfg.get("integrations") or {}
    if kind == "native":
        on = [name for name in SURFACES if (block.get(name) or {}).get("enabled")]
        if not on:
            return "Connections: native, no installed app was found."
        return "Connections: native " + ", ".join(on) + "."
    if kind == "bridges":
        names = [spec.name for spec in enabled_specs(cfg)]
        assist = str(block.get("assist") or "defaults")
        listed = ", ".join(names) if names else "none"
        return f"Connections: bridges {listed} ({assist})."
    if kind in ("approved", "mixed"):
        parts = []
        for name in SURFACES:
            item = block.get(name) or {}
            if item.get("enabled"):
                parts.append(name + " " + str(item.get("provider") or item.get("via") or ""))
        if not parts:
            return "Connections: " + kind + ", nothing connected yet."
        return "Connections: " + ", ".join(parts) + "."
    return "Connections: off."


def connection_status(cfg: dict) -> tuple[bool, str]:
    kind = str((cfg.get("integrations") or {}).get("kind") or "off")
    if kind == "off":
        return True, "not configured"
    if kind == "native":
        return _path_status(cfg, "native")
    if kind == "bridges":
        names = [spec.name for spec in enabled_specs(cfg)]
        if not names:
            return False, "bridges selected and none are enabled"
        assist = str((cfg.get("integrations") or {}).get("assist") or "defaults")
        return True, "bridges " + ",".join(names) + " " + assist
    if kind in ("approved", "mixed"):
        return _path_status(cfg, kind)
    return False, "unknown connection kind"


def _path_status(cfg: dict, label: str) -> tuple[bool, str]:
    block = cfg.get("integrations") or {}
    on = []
    missing = []
    for name in SURFACES:
        item = block.get(name) or {}
        if not item.get("enabled"):
            continue
        on.append(name)
        via = str(item.get("via") or label)
        if via == "native" or str(item.get("provider") or "") == "files":
            path = str(item.get("path") or "")
            if not path or not Path(path).exists():
                missing.append(name)
    if not on:
        return True, label + " none connected" if label != "native" else "native none installed"
    if missing:
        return False, "missing " + ",".join(missing)
    return True, label + " " + ",".join(on)
