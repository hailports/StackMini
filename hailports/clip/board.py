"""Self-guided Paiste setup. It prompts, then stores what the person connected.

Mail and chat stay copy-only. Nothing is sent. A blank model line stops the list.
"""

from __future__ import annotations

import re
import shutil
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from hailports.chat_badge import secret_hold
from hailports.clip.hops import _HOST, _url_ok

_SURFACES = (
    ("notes", "notes_dir", "Notes your model already has?", "paisty does not connect to notes and does not keep them. Leave this off when your model already has them. A folder is read on the click, then forgotten."),
    ("mail", "email_dir", "Mail your model already has?", "paisty does not sign in to mail and does not keep the message. Nothing is sent. Type front to read the window once, or a folder you already exported."),
    ("chat", "chat_dir", "Chat your model already has?", "paisty does not join the chat and does not keep it. Nothing is sent. Type front to read the window once, or a folder you already exported."),
    ("calendar", "calendar_dir", "Calendar your model already has?", "paisty does not accept or send invites and does not keep events. A folder of .ics files is read on the click, then forgotten."),
)

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
_KEEP = ("context_file", "lookup_cmd", "me", "license")


class BoardError(ValueError):
    """The onboarding answers cannot be saved."""


def _path_ok(raw: str) -> bool:
    text = (raw or "").strip()
    if not text or text in {"front", "skip", "back"}:
        return False
    if len(text) > 240 or any(ch in text for ch in "\n\r\t"):
        return False
    if "://" in text or "@" in text or text.startswith("-") or text.split()[0] in {"ssh", "http", "https"}:
        return False
    if secret_hold(text):
        return False
    return True


def parse_hop(line: str, index: int) -> dict | None:
    parts = (line or "").split()
    if not parts:
        return None
    if parts[0].startswith(("http://", "https://")):
        if not _url_ok(parts[0]):
            return None
        name = "local" if index == 0 else "local" + str(index + 1)
        return {"name": name, "spec": {
            "via": "http", "base_url": parts[0], "model": "local", "ssh_host": "", "remote_url": "",
        }}
    if parts[0] == "ssh" and len(parts) >= 3 and _HOST.match(parts[1]) and _url_ok(parts[2]):
        name = "ssh" if index == 0 else "ssh" + str(index + 1)
        return {"name": name, "spec": {
            "via": "ssh", "base_url": "", "model": "local", "ssh_host": parts[1], "remote_url": parts[2],
        }}
    return None


def _model_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=0.4) as resp:
            return resp.status == 200
    except (OSError, TimeoutError):
        return False


def discover(home: Path | None = None) -> dict:
    """Look for an Obsidian vault, a local model, and a notes program. Nothing is sent."""
    root = Path(home) if home is not None else Path.home()
    vault = ""
    for base in (root, root / "Documents", root / "notes"):
        if not base.is_dir():
            continue
        found = []
        if (base / ".obsidian").is_dir():
            found.append(base)
        try:
            children = [child for child in base.iterdir() if child.is_dir() and not child.name.startswith(".")]
        except OSError:
            children = []
        for child in children:
            if (child / ".obsidian").is_dir():
                found.append(child)
                break
        if found:
            vault = str(found[0])
            break
    model = ""
    for url in ("http://127.0.0.1:8080/v1", "http://127.0.0.1:18080/v1"):
        if _model_up(url):
            model = url
            break
    voice = root / ".config" / "paiste" / "voice.md"
    program = root / ".config" / "paiste" / "work-brain-lookup"
    cursor = shutil.which("agent") or shutil.which("cursor-agent") or ""
    return {
        "obsidian": vault,
        "model": model,
        "context_file": str(voice) if voice.is_file() else "",
        "lookup_cmd": str(program) if program.is_file() else "",
        "cursor": cursor if cursor.startswith("/") else "",
    }


def _surface(io, out: dict, key: str, path_key: str, title: str, explain: str, suggested: str = "") -> None:
    if not io.confirm("paiste_" + key, title, explain, False):
        out[key] = "off"
        out[path_key] = ""
        return
    raw = io.ask(
        "paiste_" + key + "_path",
        "Folder, or front",
        explain,
        suggested or ("front" if key in {"mail", "chat"} else ""),
    ).strip()
    if raw in {"", "skip", "back"}:
        out[key] = "off"
        out[path_key] = ""
        return
    if raw == "front" and key in {"mail", "chat"}:
        out[key] = "front"
        out[path_key] = ""
        return
    if not _path_ok(raw):
        io.say("That path was not kept.")
        out[key] = "off"
        out[path_key] = ""
        return
    out[key] = "folder"
    out[path_key] = raw
    if key == "notes" and Path(raw).joinpath(".obsidian").is_dir():
        out["obsidian_vault"] = raw


def setup_hint(url: str = "") -> str:
    """One short hint from the setup guide. Fail open. No mail or chat text is sent."""
    import json as _json
    import os
    import urllib.request
    target = url or os.environ.get("PAISTY_SETUP_URL") or ""
    if not target:
        return ""
    try:
        req = urllib.request.Request(
            target,
            data=_json.dumps({"step": "start"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            doc = _json.loads(resp.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError):
        return ""
    return str(doc.get("say") or "").strip()[:500]


def _model_ok(name: str) -> bool:
    return bool(_MODEL.match(name or ""))


def _loopback(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "0.0.0.0"}


def _key_path_ok(path: str) -> bool:
    text = (path or "").strip()
    return text.startswith("/") and ".." not in text and len(text) <= 240 and "\n" not in text


def store_api_key(directory: Path, secret: str) -> str:
    """Write the model key on this Mac. The route file stores the path, not the key."""
    text = (secret or "").strip()
    if not text or len(text) > 400 or any(ch in text for ch in "\r\n\t "):
        raise BoardError("That key was not kept.")
    if secret_hold(text):
        raise BoardError("That key was not kept.")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "api.key"
    path.write_text(text + "\n", encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def show_board(paiste: dict | None = None, found: dict | None = None) -> dict:
    """Values for the onboarding window. No key, no mail text, no notes text."""
    current = paiste or {}
    models = current.get("models") if isinstance(current.get("models"), dict) else {}
    order = current.get("order") if isinstance(current.get("order"), list) else []
    local = ""
    local_model = "local"
    api_url = ""
    api_model = ""
    has_key = False
    for name in order:
        spec = models.get(name)
        if not isinstance(spec, dict):
            continue
        via = str(spec.get("via") or "http")
        if via == "ssh" and not local:
            host = str(spec.get("ssh_host") or "")
            remote = str(spec.get("remote_url") or "")
            if host and remote:
                local = "ssh " + host + " " + remote
                local_model = str(spec.get("model") or "local")
        elif via == "http":
            base = str(spec.get("base_url") or "")
            if _loopback(base) and not local:
                local = base
                local_model = str(spec.get("model") or "local")
            elif base and not _loopback(base) and not api_url:
                api_url = base
                api_model = str(spec.get("model") or "")
                has_key = _key_path_ok(str(spec.get("key_file") or ""))
    if not local and not api_url:
        local = str((found or {}).get("model") or "")
    if local and api_url:
        link = "both"
    elif api_url:
        link = "api"
    else:
        link = "local"

    def mode(key: str) -> str:
        value = str(current.get(key) or "off")
        return value if value in {"off", "front", "folder"} else "off"

    return {
        "link": link,
        "local": local,
        "local_model": local_model,
        "api_url": api_url,
        "api_model": api_model,
        "has_key": has_key,
        "mail": mode("mail"),
        "chat": mode("chat"),
        "notes": mode("notes"),
        "notes_path": str(current.get("notes_dir") or ""),
        "calendar": mode("calendar"),
        "calendar_path": str(current.get("calendar_dir") or ""),
        "obsidian": str((found or {}).get("obsidian") or ""),
    }


def _named(existing: dict, spec: dict, fallback: str) -> str:
    models = existing.get("models") if isinstance(existing.get("models"), dict) else {}
    for name, old in models.items():
        if not isinstance(old, dict):
            continue
        if spec.get("via") == "ssh" and old.get("via") == "ssh" and old.get("ssh_host") == spec.get("ssh_host") and old.get("remote_url") == spec.get("remote_url"):
            return str(name)
        if spec.get("via") == "http" and str(old.get("via") or "http") != "ssh" and old.get("base_url") == spec.get("base_url"):
            return str(name)
    return fallback


def _one_surface(out: dict, key: str, path_key: str, mode: str, path: str) -> None:
    picked = (mode or "off").strip().lower()
    if picked == "front" and key in {"mail", "chat"}:
        out[key] = "front"
        out[path_key] = ""
        return
    if picked == "folder" and _path_ok(path or ""):
        out[key] = "folder"
        out[path_key] = path.strip()
        if key == "notes" and Path(path.strip()).joinpath(".obsidian").is_dir():
            out["obsidian_vault"] = path.strip()
        return
    out[key] = "off"
    out[path_key] = ""


def apply_board(existing: dict | None, answers: dict) -> dict:
    """Save local, API, or both, plus which windows to read once. No account links."""
    current = dict(existing or {})
    link = str(answers.get("link") or "").strip().lower()
    if link not in {"local", "api", "both"}:
        raise BoardError("Choose local, API, or both.")
    out = {key: current[key] for key in _KEEP if key in current}
    out["enabled"] = True
    out["copy"] = True
    _one_surface(out, "mail", "email_dir", str(answers.get("mail") or ""), "")
    _one_surface(out, "chat", "chat_dir", str(answers.get("chat") or ""), "")
    _one_surface(out, "notes", "notes_dir", str(answers.get("notes") or ""), str(answers.get("notes_path") or ""))
    _one_surface(out, "calendar", "calendar_dir", str(answers.get("calendar") or ""), str(answers.get("calendar_path") or ""))
    out.setdefault("obsidian_vault", "")
    order = []
    models = {}
    if link in {"local", "both"}:
        hop = parse_hop(str(answers.get("local") or ""), 0)
        if not hop:
            raise BoardError("Add a local model, or choose API.")
        model = str(answers.get("local_model") or hop["spec"]["model"])
        hop["spec"]["model"] = model if _model_ok(model) else "local"
        name = _named(current, hop["spec"], hop["name"])
        order.append(name)
        models[name] = hop["spec"]
    if link in {"api", "both"}:
        url = str(answers.get("api_url") or "").strip()
        if not _url_ok(url) or _loopback(url):
            raise BoardError("The API address is a service, not a local server.")
        model = str(answers.get("api_model") or "").strip()
        if not _model_ok(model):
            raise BoardError("Name the API model.")
        key_file = str(answers.get("key_file") or "")
        if key_file and not _key_path_ok(key_file):
            raise BoardError("That key was not kept.")
        if not key_file:
            old_models = current.get("models") if isinstance(current.get("models"), dict) else {}
            for old in old_models.values():
                if isinstance(old, dict) and old.get("base_url") == url and _key_path_ok(str(old.get("key_file") or "")):
                    key_file = str(old.get("key_file"))
                    break
        spec = {
            "via": "http",
            "base_url": url,
            "model": model,
            "ssh_host": "",
            "remote_url": "",
            "key_file": key_file if _key_path_ok(key_file) else "",
        }
        name = _named(current, spec, "api")
        order.append(name)
        models[name] = spec
    out["order"] = order
    out["models"] = models
    return out


def guide(io, paiste: dict | None = None, found: dict | None = None) -> dict:
    """Prompt for notes, mail, chat, calendar, then the model order. Returns scalars only."""
    out = dict(paiste or {})
    out["enabled"] = True
    out["copy"] = True
    io.say("If your AI is integrated with your email, chat, or notes, paisty works for you.")
    io.say("paisty does not connect to your mail, calendar, notes, or chat, and it does not retain any data. That is your model's job.")
    io.say("Paiste copies a reply. It does not type into the app and it does not send.")
    hint = setup_hint()
    if hint:
        io.say(hint)
    io.say("You do not need a special server. Use a model on this computer, ssh to a computer you own, or the http address of a model you already run. Local, API, or both.")
    io.say("Any app in front is read once, then forgotten. skip leaves one off. Mail and chat can use the text already on screen, or a folder you export.")
    io.say("Use that one thread plus a few matching notes. Calendar shows a conflict or the matching event. You still accept, book, and send. Leave money, legal, HR, and conflict to yourself.")
    found = found or {}
    if found.get("obsidian"):
        io.say("Found an Obsidian vault at " + found["obsidian"] + ". Use it for notes, or skip.")
    io.say("A work brain is optional. It is a program that prints notes. Skip it if you do not have one.")
    if found.get("model"):
        io.say("Found a model at " + found["model"] + ".")
    for key, path_key, title, explain in _SURFACES:
        suggested = found.get("obsidian", "") if key == "notes" else ""
        _surface(io, out, key, path_key, title, explain, suggested)
    io.say("Anything else is a file or a program you already have. A file can be how you write, a ChatGPT instruction, a Claude project, or a Cursor rule. A program prints extra notes. Paiste does not sign into those products.")
    context = io.ask("paiste_context", "Context file", "A file of instructions or notes. Blank skips.", found.get("context_file") or "").strip()
    out["context_file"] = context if _path_ok(context) else ""
    if context and not out["context_file"]:
        io.say("That file was not kept.")
    command = io.ask("paiste_lookup", "Notes program", "A program on this computer. It receives the window text and prints notes. Blank skips.", found.get("lookup_cmd") or "").strip()
    out["lookup_cmd"] = command if _path_ok(command) else ""
    if command and not out["lookup_cmd"]:
        io.say("That program was not kept.")
    io.say("Add models in the order Paiste should try them. A local server is http://127.0.0.1:18080/v1. A model on another machine is: ssh hostname http://127.0.0.1:8080/v1. A blank line stops.")
    order = []
    models = {}
    for index in range(4):
        line = io.ask(
            "paiste_hop_" + str(index),
            "Next model",
            "http URL, or ssh host http URL. Blank stops.",
            found.get("model", "") if index == 0 else "",
        )
        if line.strip() in {"", "skip", "back"}:
            break
        hop = parse_hop(line, index)
        if not hop:
            io.say("Skipped that model line.")
            continue
        order.append(hop["name"])
        models[hop["name"]] = hop["spec"]
    if order:
        out["order"] = order
        out["models"] = models
    return out
