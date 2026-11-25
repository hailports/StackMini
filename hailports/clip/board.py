"""Self-guided Paiste setup. It prompts, then stores what the person connected.

Mail and chat stay copy-only. Nothing is sent. A blank model line stops the list.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from hailports.chat_badge import secret_hold
from hailports.clip.hops import _HOST, _url_ok, needs_key

_SURFACES = (
    ("notes", "notes_dir", "Notes your model already has?", "paisty does not connect to notes and does not keep them. Leave this off when your model already has them. A folder is read on the click, then forgotten."),
    ("mail", "email_dir", "Mail your model already has?", "paisty does not sign in to mail and does not keep the message. Nothing is sent. Type front to read the window once, or a folder you already exported."),
    ("chat", "chat_dir", "Chat your model already has?", "paisty does not join the chat and does not keep it. Nothing is sent. Type front to read the window once, or a folder you already exported."),
    ("calendar", "calendar_dir", "Calendar your model already has?", "paisty does not accept or send invites and does not keep events. A folder of .ics files is read on the click, then forgotten."),
)

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
_KEEP = ("context_file", "lookup_cmd", "me", "license", "sources_dir")


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
        with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=1.5) as resp:
            if resp.status != 200:
                return False
            # A long model list cut short is not JSON; read it whole or a live server reads as dead.
            body = resp.read(262144).decode("utf-8", "replace")
    except (OSError, TimeoutError):
        return False
    return bool(model_ids(body))


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
    for url in ("http://127.0.0.1:18080/v1", "http://127.0.0.1:8000/v1", "http://127.0.0.1:8080/v1"):
        if _model_up(url):
            model = url
            break
    voice = root / ".config" / "paiste" / "voice.md"
    program = root / ".config" / "paiste" / "knowledge-base-lookup"
    cursor = shutil.which("agent") or shutil.which("cursor-agent") or ""
    return {
        "obsidian": vault,
        "model": model,
        "context_file": str(voice) if voice.is_file() else "",
        "lookup_cmd": str(program) if program.is_file() else "",
        "cursor": cursor if cursor.startswith("/") else "",
    }


# :8000 is the common default for oMLX, vLLM and llama-cpp-python; :8080 is llama.cpp's llama-server.
_LOCAL_URLS = (
    "http://127.0.0.1:18080/v1",
    "http://127.0.0.1:11434/v1",
    "http://127.0.0.1:1234/v1",
    "http://127.0.0.1:8000/v1",
    "http://127.0.0.1:8080/v1",
)
_REMOTE_ENDPOINTS = (
    ("http://127.0.0.1:18080/v1/models", "http://127.0.0.1:18080/v1"),
    ("http://127.0.0.1:11434/v1/models", "http://127.0.0.1:11434/v1"),
    ("http://127.0.0.1:11434/api/tags", "http://127.0.0.1:11434/v1"),
    ("http://127.0.0.1:1234/v1/models", "http://127.0.0.1:1234/v1"),
    ("http://127.0.0.1:8000/v1/models", "http://127.0.0.1:8000/v1"),
    ("http://127.0.0.1:8080/v1/models", "http://127.0.0.1:8080/v1"),
)


# Listed by a server but not a chat model, or not loadable on its own (oMLX lists MTP heads it
# cannot load). Never picked first; a server that lists only these is not offered as the AI.
_AUX_MODEL = re.compile(r"(?i)(?:^|[-_/.:])(?:embed|embedding|embeddings|rerank|reranker|mtp|draft|whisper|tts|bge)(?:[-_/.:]|$)")


def aux_model(name: str) -> bool:
    return bool(_AUX_MODEL.search(name or ""))


def model_ids(body: str, limit: int = 4) -> list[str]:
    """Names from an OpenAI /models body or an Ollama tags body, chat models first. Nothing else."""
    try:
        doc = json.loads(body or "")
    except json.JSONDecodeError:
        return []
    if not isinstance(doc, dict):
        return []
    names: list[str] = []
    for key, field in (("data", "id"), ("models", "name")):
        rows = doc.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if len(names) == max(16, limit):
                break
            if not isinstance(row, dict):
                continue
            name = str(row.get(field) or "").strip()
            if _model_ok(name) and name not in names:
                names.append(name)
    names.sort(key=aux_model)  # stable: chat models keep the server's order
    return names[:limit]


def ssh_host_names(text: str) -> list[str]:
    """Host names from an ssh config. Wildcards are skipped. No addresses are invented."""
    hosts = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line.lower().startswith("host "):
            continue
        for token in line.split()[1:]:
            if any(ch in token for ch in "*?["):
                continue
            if not _HOST.match(token) or token in hosts:
                continue
            hosts.append(token)
            if len(hosts) == 8:
                return hosts
    return hosts


def _read_quiet(url: str) -> str:
    if not url.startswith("http://127.0.0.1:"):
        return ""
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            if int(resp.status) != 200:
                return ""
            return resp.read(262144).decode("utf-8", "replace")
    except (OSError, TimeoutError, urllib.error.URLError):
        return ""


def _ssh_body(host: str, url: str) -> str:
    allowed = {item[0] for item in _REMOTE_ENDPOINTS}
    if url not in allowed or not _HOST.match(host or ""):
        return ""
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
        host, "curl", "-fsS", "-m", "3", url,
    ]
    try:
        done = subprocess.run(argv, capture_output=True, check=False, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if done.returncode != 0:
        return ""
    return (done.stdout or b"")[:262144].decode("utf-8", "replace")


def _hosts_from_ssh(root: Path) -> list[str]:
    path = root / ".ssh" / "config"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return ssh_host_names(text)


PER_SERVER = 6


def _warm(reader, base: str) -> tuple:
    """Models a server on this Mac has in memory right now (Ollama, LM Studio). A warm model answers
    at once; a cold one loads first. Other servers do not say, so they keep their own order."""
    try:
        if ":11434/" in base:
            doc = json.loads(reader("http://127.0.0.1:11434/api/ps") or "{}")
            return tuple(str(m.get("name") or m.get("model") or "") for m in doc.get("models") or [] if isinstance(m, dict))
        if ":1234/" in base:
            doc = json.loads(reader("http://127.0.0.1:1234/api/v0/models") or "{}")
            return tuple(str(m.get("id") or "") for m in doc.get("data") or []
                         if isinstance(m, dict) and m.get("state") == "loaded")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return ()
    return ()


def scan_models(read_local=None, hosts=None, read_ssh=None) -> list[dict]:
    """Find a running model on this Mac or on an ssh host. The name comes from the server."""
    reader = read_local or _read_quiet
    remote = read_ssh or _ssh_body
    rows = []
    seen = set()

    def add(where: str, host: str, base: str, body: str, warm: tuple = ()) -> None:
        if not body or not body.lstrip().startswith("{"):
            return
        # One row per chat model, so a server with several can be picked from. Embedding,
        # reranker and MTP heads cannot write a reply and are never offered.
        names = [name for name in model_ids(body, limit=PER_SERVER) if not aux_model(name)]
        names.sort(key=lambda name: name not in warm)  # loaded now beats a cold load
        for name in names:
            key = (host, base, name)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"where": where, "host": host, "url": base, "model": name})

    for base in _LOCAL_URLS:
        body = reader(base.rstrip("/") + "/models")
        if body:
            add("This Mac", "", base, body, _warm(reader, base))
        if ":11434/" in base:
            tags = reader("http://127.0.0.1:11434/api/tags")
            if tags:
                add("This Mac", "", "http://127.0.0.1:11434/v1", tags)
    found_remote = False
    for host in list(hosts or [])[:8]:
        if found_remote or not _HOST.match(host):
            continue
        for endpoint, base in _REMOTE_ENDPOINTS:
            body = remote(host, endpoint)
            if not body:
                continue
            add(host, host, base, body)
            found_remote = True
            break
    return rows[:12]


def _vaults(root: Path) -> list[str]:
    found = []
    cfg = root / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    try:
        doc = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        doc = {}
    vaults = doc.get("vaults") if isinstance(doc, dict) else None
    if isinstance(vaults, dict):
        for row in vaults.values():
            if not isinstance(row, dict):
                continue
            path = str(row.get("path") or "").strip()
            if _path_ok(path) and Path(path).is_dir() and path not in found:
                found.append(path)
            if len(found) == 6:
                return found
    one = str(discover(root).get("obsidian") or "")
    if one and one not in found:
        found.insert(0, one)
    return found[:6]


_CONTEXT_APPS = (
    ("Mail", ("/System/Applications/Mail.app", "/Applications/Mail.app")),
    ("Outlook", ("/Applications/Microsoft Outlook.app",)),
    ("Spark", ("/Applications/Spark.app",)),
    ("Zoom", ("/Applications/zoom.us.app",)),
    ("Messages", ("/System/Applications/Messages.app",)),
    ("Slack", ("/Applications/Slack.app",)),
    ("Teams", ("/Applications/Microsoft Teams.app",)),
    ("Littlebird", ("/Applications/Littlebird.app",)),
)


def _installed(pairs=_CONTEXT_APPS) -> list[str]:
    home_apps = Path.home() / "Applications"
    found = []
    for label, paths in pairs:
        checked = list(paths)
        if paths:
            checked.append(str(home_apps / Path(paths[-1]).name))
        if any(Path(path).exists() for path in checked):
            found.append(label)
    return found


def machine_context(home: Path | None = None, present: list[str] | None = None) -> dict:
    """Turn on mail, chat, Littlebird, and an Obsidian vault that are already on this Mac.

    A click reads the open window. Nothing is signed in and no note text is copied here.
    """
    root = Path(home) if home is not None else Path.home()
    installed = list(present) if present is not None else _installed()
    vaults = _vaults(root)
    mail = [name for name in ("Mail", "Outlook", "Spark") if name in installed]
    chat = [name for name in ("Zoom", "Messages", "Slack", "Teams", "Littlebird") if name in installed]
    return {
        "mail_apps": mail,
        "chat_apps": chat,
        "vaults": vaults,
        "mail": "front" if mail else "off",
        "chat": "front" if chat else "off",
        "notes": "folder" if vaults else "off",
        "notes_path": vaults[0] if vaults else "",
        "littlebird": "Littlebird" in installed,
    }


# Premium providers that speak the OpenAI chat route. Key names only; values never leave os.environ.
API_PROVIDERS = (
    ("openai", "OpenAI", ("OPENAI_API_KEY",), "https://api.openai.com/v1", "gpt-4.1-mini"),
    ("anthropic", "Anthropic", ("ANTHROPIC_API_KEY",), "https://api.anthropic.com/v1", "claude-sonnet-5-5"),
    ("xai", "xAI Grok", ("XAI_API_KEY",), "https://api.x.ai/v1", "grok-4"),
    ("gemini", "Google Gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
     "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    ("openrouter", "OpenRouter", ("OPENROUTER_API_KEY",), "https://openrouter.ai/api/v1", "openrouter/auto"),
    ("groq", "Groq", ("GROQ_API_KEY",), "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    ("mistral", "Mistral", ("MISTRAL_API_KEY",), "https://api.mistral.ai/v1", "mistral-large-latest"),
    ("deepseek", "DeepSeek", ("DEEPSEEK_API_KEY",), "https://api.deepseek.com/v1", "deepseek-chat"),
)
_APP_LABELS = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor", "grok": "Grok"}


def env_keys(env=None) -> list[dict]:
    """Which premium keys are set. Name only. The value is never returned."""
    import os
    source = os.environ if env is None else env
    found = []
    for pid, label, names, url, model in API_PROVIDERS:
        for name in names:
            if str(source.get(name) or "").strip():
                found.append({"provider": pid, "label": label, "env": name, "url": url, "model": model})
                break
    return found


def _apple_notes(present=None) -> bool:
    if present is not None:
        return bool(present)
    return any(Path(p).exists() for p in ("/System/Applications/Notes.app", "/Applications/Notes.app"))


def suggest_setup(apps: list[dict], keys: list[dict], models: list[dict], vaults: list[str], apple_notes: bool) -> dict:
    """One AI and one notes pick, each with a plain reason."""
    ai = {"kind": "none", "name": "", "reason": "Nothing is set up yet. Sign in to Claude, Codex, or Cursor, or paste an API key."}
    by_name = {row.get("name"): row for row in apps}
    picked = next((name for name in _APP_NAMES if (by_name.get(name) or {}).get("status") == "signed-in"), "")
    if picked:
        ai = {"kind": "app", "name": picked, "reason": _APP_LABELS[picked] + " is signed in on this Mac."}
    elif keys:
        first = keys[0]
        ai = {"kind": "api", "name": first["provider"], "env": first["env"], "url": first["url"], "model": first["model"],
              "reason": "Your " + first["label"] + " key is already on this Mac."}
    elif models:
        first = models[0]
        ai = {"kind": "local", "name": str(first.get("model") or "local"), "url": str(first.get("url") or ""),
              "host": str(first.get("host") or ""),
              "reason": "A model is running on " + ("this Mac." if not first.get("host") else str(first.get("host")) + ".")}
    if vaults:
        notes = {"kind": "vault", "path": vaults[0], "reason": "Your Obsidian notes are here."}
    elif apple_notes:
        notes = {"kind": "apple-notes", "path": "", "reason": "Apple Notes is on this Mac. A notes folder is made too."}
    else:
        notes = {"kind": "folder", "path": "", "reason": "A small notes folder is made for you."}
    return {"ai": ai, "notes": notes}


def _app_rows(status=None, find_bin=None) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor
    check = status or app_status
    where = find_bin or app_bin
    names = list(_APP_NAMES)
    with ThreadPoolExecutor(max_workers=3) as pool:
        states = list(pool.map(check, names))
    rows = []
    for name, state in zip(names, states):
        rows.append({"name": name, "installed": state != "missing", "status": state, "drives": True})
    grok = where("grok")
    # hops.py cannot drive a grok CLI yet. Reported, never routed.
    rows.append({"name": "grok", "installed": bool(grok), "status": "installed" if grok else "missing", "drives": False})
    return rows


def scan_setup(home: Path | None = None, read_local=None, hosts=None, read_ssh=None,
               status=None, find_bin=None, env=None, apple_notes=None) -> dict:
    """What the setup window can offer. No mail text, no key, no model list dumped raw."""
    root = Path(home) if home is not None else Path.home()
    picked_hosts = hosts if hosts is not None else _hosts_from_ssh(root)
    found = machine_context(root)
    apps = _app_rows(status, find_bin)
    keys = env_keys(env)
    models = scan_models(read_local=read_local, hosts=picked_hosts, read_ssh=read_ssh)
    has_notes = _apple_notes(apple_notes)
    return {
        "mail_apps": found["mail_apps"],
        "chat_apps": found["chat_apps"],
        "vaults": found["vaults"],
        "mail": found["mail"],
        "chat": found["chat"],
        "notes": found["notes"],
        "notes_path": found["notes_path"],
        "littlebird": found["littlebird"],
        "apple_notes": has_notes,
        "apps": apps,
        "keys": [{"provider": row["provider"], "label": row["label"], "env": row["env"]} for row in keys],
        "models": models,
        "suggest": suggest_setup(apps, keys, models, found["vaults"], has_notes),
    }


def _use_hop(current: dict, name: str, spec: dict) -> dict:
    """Add one API or local hop. Other hops stay. Order is claude, codex, paid APIs, local or brain, then cursor."""
    out = dict(current or {})
    old_models = current.get("models") if isinstance(current.get("models"), dict) else {}
    models = {}
    order = []
    for hop in current.get("order") or []:
        old = old_models.get(hop)
        if isinstance(old, dict) and hop != name:
            order.append(hop)
            models[hop] = old
    models[name] = spec
    order.append(name)
    order, models = _premium_after_local(current, order, models)
    out["enabled"] = True
    out["copy"] = True
    out["order"] = order
    out["models"] = models
    return out


def auto_setup(current: dict | None, scan: dict, key_dir: Path, notes_dir: Path,
               setup_text=None, env=None, binary: str = "") -> tuple[dict, dict]:
    """Apply the scan suggestion. Returns the new settings and a short summary. Prints nothing secret."""
    import os
    source = os.environ if env is None else env
    out = dict(current or {})
    suggest = scan.get("suggest") or {}
    ai = suggest.get("ai") or {}
    needs: list[str] = []
    picked = ""
    local_model = ""
    local_host = ""
    kind = ai.get("kind")
    if kind == "app":
        suggested = str(ai.get("name") or "")
        signed = []
        for row in scan.get("apps") or []:
            name = str(row.get("name") or "")
            if row.get("drives") and row.get("status") == "signed-in" and name in _APP_NAMES and name not in signed:
                signed.append(name)
        if suggested in _APP_NAMES and suggested not in signed:
            signed.append(suggested)
        signed.sort(key=lambda name: _APP_NAMES.index(name))
        added = ""
        for name in signed:
            try:
                out = use_app(out, name, binary if name == suggested else "")
                added = name
            except BoardError:
                continue
        if added:
            picked = suggested if suggested in (out.get("models") or {}) else added
        elif suggested:
            out = use_app(out, suggested, binary)
            picked = suggested
    elif kind == "api":
        secret = str(source.get(str(ai.get("env") or "")) or "").strip()
        try:
            key_file = store_api_key(key_dir, secret, str(ai.get("name") or "api")) if secret else ""
        except BoardError:
            key_file = ""
        if key_file:
            spec = {"via": "http", "base_url": str(ai.get("url")), "model": str(ai.get("model")),
                    "ssh_host": "", "remote_url": "", "key_file": key_file}
            out = _use_hop(out, str(ai.get("name")), spec)
            picked = str(ai.get("name"))
        else:
            needs.append("paste an API key")
    elif kind == "local":
        line = ("ssh " + str(ai.get("host")) + " " + str(ai.get("url"))) if ai.get("host") else str(ai.get("url") or "")
        hop = parse_hop(line, 0) if line else None
        if hop:
            if _model_ok(str(ai.get("name") or "")):
                hop["spec"]["model"] = str(ai.get("name"))
            out = _use_hop(out, hop["name"], hop["spec"])
            picked = "local"
            local_model = str(hop["spec"].get("model") or "")
            local_host = str(ai.get("host") or "")
    if not picked:
        waiting = [row["name"] for row in scan.get("apps") or [] if row.get("drives") and row.get("status") == "login"]
        if waiting:
            needs.append("sign in to " + _APP_LABELS.get(waiting[0], waiting[0]))
        else:
            if not scan.get("models"):
                # Say so plainly. A setup that "succeeds" with no reachable model is worse than one that asks.
                needs.append("enter your local model address (no running model was found on this Mac)")
            if "paste an API key" not in needs:
                needs.append("paste an API key")
    mail = list(scan.get("mail_apps") or [])
    chat = list(scan.get("chat_apps") or [])
    for key, path_key, found_apps in (("mail", "email_dir", mail), ("chat", "chat_dir", chat)):
        if str(out.get(key) or "off").strip().lower() == "off":
            _one_surface(out, key, path_key, "front" if found_apps else "off", "")
    notes = suggest.get("notes") or {}
    path = ""
    if notes.get("kind") == "vault" and notes.get("path"):
        _one_surface(out, "notes", "notes_dir", "folder", str(notes.get("path")))
        path = str(out.get("notes_dir") or "")
    if not path:
        app = picked if picked in _APP_NAMES else ""
        fresh = not (Path(notes_dir) / "Paisty.md").exists()
        body = setup_text(bool(mail), app) if setup_text is not None and fresh else ""
        made = make_notes(Path(notes_dir), mail=bool(mail), body=body or "")
        out["notes"] = "folder"
        out["notes_dir"] = made["path"]
        path = made["path"]
        if notes.get("kind") == "apple-notes":
            needs.append("bring in Apple Notes (Customize, then Notes)")
    out.setdefault("obsidian_vault", "")
    summary = {"ai": picked, "notes": path, "mail": mail, "chat": chat, "needs": needs}
    if picked == "local":
        # The setup window names the model it picked, so a wrong pick is visible.
        summary["model"] = local_model
        summary["host"] = local_host
    return out, summary


def live_llm(llm: dict | None, scan: dict, picked: str, up=None) -> dict | None:
    """The top-level llm block to save after auto setup, or None to drop it.

    A local model the scan found (on this Mac) becomes the llm block. Otherwise a loopback
    address that does not answer /v1/models is dropped, so setup never saves a dead default.
    A saved address that is not loopback (or answers) is kept as it is.
    """
    check = up or _model_up
    ai = (scan.get("suggest") or {}).get("ai") or {}
    if picked == "local" and not ai.get("host") and str(ai.get("url") or "").startswith("http://127.0.0.1:"):
        model = str(ai.get("name") or "")
        return {"base_url": str(ai.get("url")), "model": model if _model_ok(model) else "local"}
    block = dict(llm or {})
    base = str(block.get("base_url") or "").strip()
    if not base:
        return None
    if _loopback(base) and not check(base):
        return None
    return block


_INGEST_OK = {".md", ".txt", ".markdown", ".eml", ".ics", ".pdf", ".csv"}
_INGEST_SKIP = {"route.yaml", "license", "license-secret", "id_rsa", "credentials", ".env"}


_CODE_STOP = {
    "this", "that", "with", "from", "please", "could", "would", "should", "where",
    "which", "what", "when", "have", "need", "your", "they", "them", "just", "make",
    "sure", "about", "there", "their", "these", "those", "using", "before", "after",
}


_CODE_LEAD = {
    "add", "if", "show", "where", "the", "a", "an", "to", "for", "on", "in",
    "should", "could", "please", "which", "what", "when", "and", "or",
}


def _code_trim(phrase: str) -> str:
    words = phrase.split()
    while words and words[0].lower() in _CODE_LEAD:
        words = words[1:]
    while words and words[-1].lower() in _CODE_LEAD:
        words = words[:-1]
    return " ".join(words)


def code_phrases(query: str) -> list[str]:
    """Short phrases worth finding in a code tree. No whole window."""
    raw = query or ""
    found = []

    def add(phrase: str) -> None:
        piece = " ".join(phrase.split())
        if len(piece) < 8 or piece.lower() in {item.lower() for item in found}:
            return
        found.append(piece)

    for stem in ("export button", "machine orders", "search orders", "open orders"):
        if stem in raw.lower():
            add(stem)
    for match in re.findall(r"\b[A-Z][A-Za-z0-9]+(?:\s+[A-Za-z][A-Za-z0-9]+){1,4}", raw):
        add(_code_trim(match))
    for match in re.findall(
        r"\b(?:export|search|orders|order|machine|filter|error)s?(?:\s+[A-Za-z]+){1,3}",
        raw,
        re.I,
    ):
        add(_code_trim(match))
    return found[:6]


def code_notes(query: str, roots_file: Path | None = None, runner=None) -> str:
    """Lines from code folders listed on this Mac. Empty when none are configured."""
    path = roots_file if roots_file is not None else Path.home() / ".config" / "paiste" / "code-roots"
    if not path.is_file():
        return ""
    roots = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        root = Path(raw).expanduser()
        if root.is_dir():
            roots.append(root)
    if not re.search(r"(?i)\b(export button|where (exactly|should)|add (?:an|a)|controller|\blwc\b|\bapex\b)\b", query or ""):
        return ""
    phrases = code_phrases(query)
    if not roots or not phrases:
        return ""
    if runner is not None:
        return str(runner(phrases, roots) or "").strip()[:1800]
    kept = []

    def take(blob: str) -> None:
        for line in (blob or "").splitlines():
            if not line.strip() or "/__tests__/" in line or "Test.cls:" in line:
                continue
            if line[:220] in kept:
                continue
            kept.append(line[:220])
            if len(kept) == 12:
                return

    rg = shutil.which("rg")
    if rg:
        argv = [rg, "-n", "-i", "-m", "2", "-g", "*.cls", "-g", "*.js", "-g", "*.html", "-g", "*.xml",
                "-g", "!**/__tests__/**", "-g", "!**/*Test.cls"]
        for phrase in phrases:
            argv.extend(["-e", phrase])
        argv.extend(str(root) for root in roots[:4])
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            done = None
        if done is not None:
            take(done.stdout or "")
        return "\n".join(kept)
    grep = "/usr/bin/grep"
    for phrase in phrases:
        if len(kept) >= 12:
            break
        argv = [grep, "-R", "-n", "-i", "-m", "3",
                "--include=*.cls", "--include=*.js", "--include=*.html", "--include=*.xml",
                "-e", phrase]
        argv.extend(str(root) for root in roots[:4])
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=4, check=False)
        except (OSError, subprocess.TimeoutExpired):
            break
        take(done.stdout or "")
    return "\n".join(kept)


def thin_reply(reply: str, notes: str) -> bool:
    """True when the notes name a file or type and the reply uses none of them."""
    names = re.findall(r"[\w./-]+\.(?:cls|js|html)\b", notes or "")
    names += re.findall(r"\b[A-Z][A-Za-z0-9]{7,}\b", notes or "")
    unique = []
    for name in names:
        token = name.split("/")[-1]
        token = token.split(".")[0]
        if len(token) >= 6 and token not in unique:
            unique.append(token)
    if not unique:
        return False
    low = (reply or "").lower()
    return not any(token.lower() in low for token in unique[:12])


def latest_from_other(text: str) -> list[str]:
    """What the other person just said. Older questions they already answered are not asks."""
    speaker = ""
    chunks = []
    current = []

    def flush() -> None:
        if speaker and current:
            chunks.append((speaker, " ".join(current)))

    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line or len(line) > 400:
            continue
        if re.match(r"(?i)^you\b", line) and len(line) < 48:
            flush()
            current = []
            speaker = "me"
            continue
        if re.match(r"^[A-Z][A-Za-z.'-]+,\s+[A-Z]", line) and len(line) < 80:
            flush()
            current = []
            speaker = "them"
            continue
        if speaker:
            current.append(line)
    flush()
    theirs = [body for who, body in chunks if who == "them" and len(body) > 20]
    if not theirs:
        return []
    last = theirs[-1]
    if len(theirs) >= 2:
        last = theirs[-2] + " " + last
    parts = re.split(r"(?<=[.!?])\s+", last)
    kept = []
    for part in parts:
        piece = part.strip()
        if len(piece) > 20 and piece not in kept:
            kept.append(piece)
    return kept[:4]


def open_points(text: str) -> list[str]:
    """The asks on the screen: a numbered line or a question. Not the chrome."""
    found = []
    for raw in (text or "").splitlines():
        line = " ".join(raw.split()).strip(" -*")
        if len(line) < 16 or len(line) > 300:
            continue
        if re.match(r"^[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]*)?\s+\d{1,2}:\d{2}\s*[AP]M$", line):
            continue
        numbered = re.match(r"^\d+[.)]\s+(\S.*)$", line)
        if numbered:
            found.append(numbered.group(1).strip())
            continue
        if "?" in line or re.search(
            r"(?i)\b(can you|could you|tell me|do not|don't|please|not in|moved|changed)\b|\d{1,2}:\d{2}",
            line,
        ):
            found.append(line)
    unique = []
    for point in found:
        if point not in unique:
            unique.append(point)
    return unique[:8]


def thread_points(text: str) -> list[str]:
    """Numbered asks and the latest lines. An older open item stays on the list."""
    found = []
    for point in open_points(text) + latest_from_other(text):
        if point not in found:
            found.append(point)
    return found[:6]


def misses_points(reply: str, points: list[str]) -> bool:
    """True when two or more asks are on screen and the reply skips one."""
    if len(points) < 2:
        return False
    low = (reply or "").lower()
    hit = 0
    for point in points:
        words = [word.lower() for word in re.findall(r"[A-Za-z]{5,}", point)][:6]
        times = re.findall(r"\d{1,2}:\d{2}", point)
        if any(word in low for word in words) or any(token in low for token in times):
            hit += 1
        elif not words and not times:
            hit += 1
    return hit < len(points)


_SECRET_NOTE = re.compile(
    r"(?i)password|seed phrase|mnemonic|verification code|one[- ]time code|\b2fa\b"
)
_NOTE_SPLIT = re.compile(r"\nENDNOTE\n")


def apple_note_pages(text: str) -> list[tuple[str, str]]:
    pages = []
    for block in _NOTE_SPLIT.split(text or ""):
        if not block.strip().startswith("NOTE "):
            continue
        head, _, body = block.partition("\n")
        title = head[5:].strip()[:80] or "note"
        body = body.strip()
        if not body or _SECRET_NOTE.search(body):
            continue
        pages.append((title, body[:8000]))
        if len(pages) == 30:
            break
    return pages


def monday_pages(doc: dict) -> list[tuple[str, str]]:
    data = doc.get("data") if isinstance(doc, dict) else None
    boards = data.get("boards") if isinstance(data, dict) else None
    if not isinstance(boards, list):
        return []
    pages = []
    for board in boards:
        if not isinstance(board, dict):
            continue
        name = str(board.get("name") or "board")[:80]
        page = data.get("items_page") if False else board.get("items_page")
        items = page.get("items") if isinstance(page, dict) else None
        lines = ["# " + name]
        if isinstance(items, list):
            for item in items[:20]:
                if not isinstance(item, dict):
                    continue
                lines.append(str(item.get("name") or "item"))
                updates = item.get("updates")
                if isinstance(updates, list):
                    for update in updates[:3]:
                        if isinstance(update, dict) and update.get("text_body"):
                            lines.append(str(update.get("text_body"))[:500])
        body = "\n".join(lines).strip()
        if body and not _SECRET_NOTE.search(body):
            pages.append((name, body[:8000]))
        if len(pages) == 10:
            break
    return pages


def _write_pages(dest: Path, pages: list[tuple[str, str]]) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, (title, body) in enumerate(pages, 1):
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in title)[:60] or "note"
        path = dest / ("%02d-%s.txt" % (index, safe))
        path.write_text(body.strip() + "\n", encoding="utf-8")
        paths.append(str(path))
    return paths


_CACHE_DIRS = {
    "cache", "code cache", "gpucache", "dawngraphitecache", "dawnwebgpucache", "crashpad",
    "service worker", "indexeddb", "local storage", "session storage", "blob_storage",
    "shared_proto_db", "webstorage", "dips", "logs", "node_modules", "resources", "app.asar.unpacked",
}


def _text_files(root: Path, suffixes: set[str], limit: int = 40) -> list[Path]:
    found = []
    if not root.is_dir():
        return found
    for path in root.rglob("*"):
        if len(found) >= limit:
            break
        if not path.is_file() or path.is_symlink():
            continue
        if any(part.startswith(".") or part in {".git", "node_modules"} for part in path.parts):
            continue
        name = path.name.lower()
        if path.suffix.lower() not in suffixes:
            continue
        if any(word in name for word in ("auth", "token", "cookie", "secret", "password")):
            continue
        try:
            inner = path.relative_to(root).parts
        except ValueError:
            inner = path.parts
        if any(part.lower() in _CACHE_DIRS for part in inner):
            continue
        from hailports.clip.scene import junk_name
        if junk_name(path.name):
            continue
        try:
            if path.stat().st_size <= 0 or path.stat().st_size > 1_000_000:
                continue
        except OSError:
            continue
        found.append(path)
    found.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    return found[:limit]


def server_setup_text(mail: bool, calendar: bool, app: str, token: str, url: str, post=None, entitlement: str = "") -> str:
    """Ask the setup server. The model key stays there. This Mac sends the license (or, during a trial,
    the trial entitlement) and three flags."""
    if not (token or entitlement) or not str(url).startswith("https://"):
        return ""
    name = app if app in {"claude", "codex", "cursor"} else ""
    cred = {"token": token} if token else {"entitlement": entitlement}
    payload = json.dumps({**cred, "mail": bool(mail), "calendar": bool(calendar), "app": name}).encode("utf-8")
    if post is not None:
        raw = post(url, payload)
    else:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "paisty/1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
        except (OSError, TimeoutError, urllib.error.URLError):
            return ""
    try:
        doc = json.loads(raw or "")
    except json.JSONDecodeError:
        return ""
    note = str(doc.get("note") or "") if isinstance(doc, dict) and doc.get("ok") is True else ""
    if len(note) < 40 or _SECRET_NOTE.search(note):
        return ""
    if "does not keep" not in note.lower():
        note += "\n\npaisty does not keep any data. The notes stay in this folder.\n"
    return note if note.endswith("\n") else note + "\n"


def notes_setup_body(mail: bool, calendar: bool, app: str, token: str, url: str, post=None, entitlement: str = "", run_plugsky=None, run_cursor=None) -> str:
    """Free notes starter: the setup server, then local Plugsky, then Cursor. Never rewrites engine order."""
    from hailports.clip.setup_assist import assisted_setup_text, setup_facts
    note = server_setup_text(mail, calendar, app, token, url, post=post, entitlement=entitlement)
    if note:
        return note
    return assisted_setup_text(setup_facts(mail, calendar, app), run_plugsky=run_plugsky, run_cursor=run_cursor)


def cursor_setup_text(facts: str, run=None) -> str:
    """Cursor ask mode, setup only. Used when Plugsky is unavailable. It does not edit files. A secret in the text is dropped."""
    from hailports.clip.hops import cursor_argv, only_reply
    prompt = (
        "Write a short markdown note for a notes folder on this Mac. "
        "Use only the facts below. Do not invent messages, accounts, or passwords. "
        "No code fence. Do not mention Cursor.\n\n"
        + (facts or "").strip()
        + "\n\npaisty does not keep any data. The notes stay in this folder."
    )
    if run is not None:
        raw = run(prompt) or ""
    else:
        if app_status("cursor") != "signed-in":
            return ""
        binary = app_bin("cursor")
        if not binary:
            return ""
        try:
            done = subprocess.run(
                cursor_argv(binary, prompt),
                cwd=str(Path.home()),
                capture_output=True,
                check=False,
                timeout=90,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if done.returncode != 0:
            return ""
        raw = (done.stdout or b"").decode("utf-8", "replace")
    text = only_reply(str(raw))
    if len(text) < 40 or _SECRET_NOTE.search(text):
        return ""
    if "does not keep" not in text.lower():
        text += "\n\npaisty does not keep any data. The notes stay in this folder."
    return text.strip() + "\n"


def tie_notes(path: str, app: str = "", local: str = "") -> None:
    """Name the model in the notes folder. Does not overwrite the rest of the note."""
    note = Path(path or "") / "Paisty.md"
    if not note.is_file():
        return
    try:
        text = note.read_text(encoding="utf-8")
    except OSError:
        return
    extra = []
    if app in {"claude", "codex", "cursor"}:
        line = "The click asks " + app[:1].upper() + app[1:] + "."
        if line not in text:
            extra.append(line)
    if local and "local model" not in text.lower():
        if app in {"claude", "codex", "cursor"}:
            extra.append("A local model is optional and runs after " + app[:1].upper() + app[1:] + ".")
        else:
            extra.append("A local model on this Mac is tried first.")
    if "does not keep any data" not in text.lower():
        extra.append("paisty does not keep any data. The notes stay in this folder.")
    if extra:
        note.write_text(text.rstrip() + "\n" + "\n".join(extra) + "\n", encoding="utf-8")


def make_notes(dest: Path, mail: bool = False, calendar: bool = False, body: str = "") -> dict:
    """One folder a click can search. Does not overwrite a note that is already there."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".obsidian").mkdir(exist_ok=True)
    note = dest / "Paisty.md"
    if body.strip():
        note.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    elif not note.is_file():
        lines = ["# Notes", ""]
        if mail:
            lines.append("Mail is connected in the AI. A click may read the mail window that is already open.")
        if calendar:
            lines.append("Calendar is connected in the AI.")
        lines.append("A click searches this folder. It does not invent a fact that is not written here.")
        lines.append("paisty does not keep any data. The notes stay in this folder.")
        note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"added": 1, "path": str(dest), "need": ""}


def pull_source(name: str, dest: Path, token: str = "", text: str = "", doc: dict | None = None,
                folder: Path | None = None, hook: Path | None = None) -> dict:
    """Turn one setup source into text files. The password is not kept."""
    import os
    kind = (name or "").strip().lower()
    pages: list[tuple[str, str]] = []
    copied: list[str] = []
    need = ""
    if kind == "apple-notes":
        pages = apple_note_pages(text)
    elif kind == "monday":
        if not (token or "").strip():
            need = "need-token"
        elif isinstance(doc, dict):
            pages = monday_pages(doc)
    elif kind in {"zoom", "littlebird", "onenote"} and folder is not None:
        suffixes = {".txt", ".md", ".vtt", ".srt"} if kind == "zoom" else {".txt", ".md", ".html"}
        if kind == "littlebird":
            suffixes = {".txt", ".md"}
        for path in _text_files(folder, suffixes):
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if kind == "onenote" and path.suffix.lower() == ".html":
                body = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", body)
                body = re.sub(r"(?s)<[^>]+>", " ", body)
            from hailports.clip.scene import junk_text
            if junk_text(body):
                continue
            body = " ".join(body.split())
            if body and not _SECRET_NOTE.search(body):
                pages.append((path.stem[:60], body[:8000]))
    else:
        need = "none"
    if pages:
        copied = _write_pages(dest, pages)
    sent = False
    script = hook if hook is not None else Path.home() / ".config" / "paiste" / "ingest"
    if copied and script.is_file() and os.access(script, os.X_OK):
        try:
            done = subprocess.run(
                [str(script)], input="\n".join(copied) + "\n",
                text=True, capture_output=True, timeout=120,
            )
            sent = done.returncode == 0 and "sent" in (done.stdout or "")
        except (OSError, subprocess.TimeoutExpired):
            sent = False
    return {"added": len(copied), "sent": sent, "need": need}


def apple_notes_text() -> str:
    script = (
        'tell application "Notes"\n'
        'set i to 0\nset out to ""\n'
        'repeat with n in notes\n'
        'set i to i + 1\n'
        'if i > 30 then exit repeat\n'
        'set out to out & "NOTE " & (name of n) & linefeed & (plaintext of n) & linefeed & "ENDNOTE" & linefeed\n'
        'end repeat\nreturn out\nend tell'
    )
    try:
        done = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout or ""


def monday_fetch(token: str) -> dict:
    query = (
        "{ boards (limit: 8) { name items_page (limit: 12) "
        "{ items { name updates (limit: 2) { text_body } } } } }"
    )
    req = urllib.request.Request(
        "https://api.monda" + "y.com/v2",
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={"Authorization": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, TimeoutError, urllib.error.URLError):
        return {}


def ingest_files(paths, root: Path, hook: Path | None = None) -> dict:
    """Copy notes, mail, and calendar the person picked. Skip secrets. Nothing is sent by itself."""
    import os
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    copied = []
    seen = set()

    def take(path: Path) -> None:
        if len(copied) >= 40 or not path.is_file() or path.is_symlink():
            return
        name = path.name.lower()
        if name.startswith(".") or name in _INGEST_SKIP or any(word in name for word in ("secret", "password", "token")):
            return
        if path.suffix.lower() not in _INGEST_OK:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= 0 or size > 5_000_000:
            return
        try:
            key = str(path.resolve())
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        dest = root / ("%02d-%s" % (len(copied) + 1, path.name.replace(" ", "_")))
        try:
            shutil.copyfile(path, dest)
        except OSError:
            return
        copied.append(str(dest))

    def walk(path: Path, depth: int) -> None:
        if depth > 2 or len(copied) >= 40 or path.is_symlink():
            return
        try:
            if path.resolve().is_relative_to(root):
                return
        except OSError:
            return
        if path.is_file():
            take(path)
            return
        if not path.is_dir() or path.name.startswith(".") or path.name in {".git", "node_modules"}:
            return
        try:
            children = sorted(path.iterdir())
        except OSError:
            return
        for child in children:
            walk(child, depth + 1)

    for raw in paths or []:
        text = str(raw or "").strip()
        if not text or ".." in Path(text).parts:
            continue
        walk(Path(text).expanduser(), 0)
    sent = False
    script = hook if hook is not None else Path.home() / ".config" / "paiste" / "ingest"
    if copied and script.is_file() and os.access(script, os.X_OK):
        try:
            done = subprocess.run(
                [str(script)],
                input="\n".join(copied) + "\n",
                text=True,
                capture_output=True,
                timeout=120,
            )
            sent = done.returncode == 0 and "sent" in (done.stdout or "")
        except (OSError, subprocess.TimeoutExpired):
            sent = False
    return {"added": len(copied), "sent": sent, "paths": copied}


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


def store_api_key(directory: Path, secret: str, name: str = "api") -> str:
    """Write one model key on this Mac. The route file stores the path, not the key."""
    text = (secret or "").strip()
    if not text or len(text) > 400 or any(ch in text for ch in "\r\n\t "):
        raise BoardError("That key was not kept.")
    if secret_hold(text):
        raise BoardError("That key was not kept.")
    safe = re.sub(r"[^A-Za-z0-9._-]", "", name or "")[:40] or "api"
    folder = directory / "keys"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (safe + ".key")
    path.write_text(text + "\n", encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def _local_line(row: dict) -> str:
    """One local hop. A host means the model is on another Mac you can ssh to."""
    host = str(row.get("host") or "").strip()
    remote = str(row.get("remote") or "").strip()
    if host:
        if not remote:
            remote = "http://127.0.0.1:18080/v1"
        return "ssh " + host + " " + remote
    url = str(row.get("url") or "").strip()
    if url:
        return url
    return str(row.get("line") or "").strip()


def _local_rows(answers: dict) -> list[tuple[str, str]]:
    rows = answers.get("locals")
    picked = []
    if isinstance(rows, list) and rows:
        for row in rows:
            if isinstance(row, str):
                line = row.strip()
                model = ""
            elif isinstance(row, dict):
                line = _local_line(row)
                model = str(row.get("model") or "")
            else:
                continue
            if line:
                picked.append((line, model))
        return picked[:6]
    line = str(answers.get("local") or "").strip()
    if line:
        return [(line, str(answers.get("local_model") or ""))]
    return []


def _api_rows(answers: dict) -> list[dict]:
    rows = answers.get("apis")
    picked = []
    if isinstance(rows, list) and rows:
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or row.get("api_url") or "").strip()
            if not url:
                continue
            picked.append({
                "url": url,
                "model": str(row.get("model") or row.get("api_model") or ""),
                "key_file": str(row.get("key_file") or ""),
            })
        return picked[:6]
    url = str(answers.get("api_url") or "").strip()
    if url:
        return [{
            "url": url,
            "model": str(answers.get("api_model") or ""),
            "key_file": str(answers.get("key_file") or ""),
        }]
    return []


def attach_keys(directory: Path, answers: dict) -> dict:
    """Move pasted keys into 0600 files. The answers keep paths only."""
    out = dict(answers)
    out.pop("api_key", None)
    rows = out.get("apis")
    if isinstance(rows, list):
        stored = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            item = {key: value for key, value in row.items() if key != "key"}
            secret = str(row.get("key") or "")
            if secret:
                item["key_file"] = store_api_key(directory, secret, "api-%d" % (index + 1))
            stored.append(item)
        out["apis"] = stored
        return out
    secret = str(answers.get("api_key") or "")
    if secret:
        out["key_file"] = store_api_key(directory, secret, "api")
    return out


def _split_local(line: str, model: str) -> dict:
    parts = (line or "").split()
    if len(parts) >= 3 and parts[0] == "ssh":
        return {"line": line, "host": parts[1], "remote": parts[2], "url": "", "model": model}
    return {"line": line, "host": "", "remote": "", "url": line, "model": model}


def probe_model(line: str, key: str = "", open_url=None, run_ssh=None) -> str:
    """Check one model address. No window text is sent. The key is not repeated."""
    text = (line or "").strip()
    if text.startswith("ssh "):
        hop = parse_hop(text, 0)
        if not hop:
            return "That address was not checked."
        host = hop["spec"]["ssh_host"]
        remote = hop["spec"]["remote_url"].rstrip("/") + "/models"
        try:
            code = run_ssh(host, remote) if run_ssh is not None else _ssh_status(host, remote)
        except (OSError, subprocess.TimeoutExpired):
            return "Not reachable."
        if str(code).strip() != "200":
            return "Not reachable."
        if run_ssh is not None:
            return "Connected."
        ids = model_ids(_ssh_body(host, remote))
        return "Connected. " + ids[0] if ids else "Connected."
    if not text.startswith(("http://", "https://")) or not _url_ok(text.split()[0]):
        return "That address was not checked."
    url = text.split()[0].rstrip("/") + "/models"
    opener = open_url or _probe_open
    try:
        status = opener(url, key)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "The server answered, and the key was refused."
        return "Not reachable."
    except (OSError, TimeoutError, urllib.error.URLError):
        return "Not reachable."
    if status == 200:
        if (key or "").strip():
            return "Connected. The key stayed on this Mac."
        if open_url is not None:
            return "Connected."
        ids = model_ids(_read_quiet(url))
        return "Connected. " + ids[0] if ids else "Connected."
    return "Not reachable."


def _probe_open(url: str, key: str) -> int:
    req = urllib.request.Request(url, method="GET")
    token = (key or "").strip()
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=3) as resp:
        return int(resp.status)


def _ssh_status(host: str, url: str) -> str:
    if not _HOST.match(host) or not _url_ok(url):
        return ""
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        host, "curl", "-sS", "-m", "6", "-o", "/dev/null", "-w", "%{http_code}", url,
    ]
    done = subprocess.run(argv, capture_output=True, check=False, timeout=12)
    return (done.stdout or b"").decode("utf-8", "replace").strip()


def show_board(paiste: dict | None = None, found: dict | None = None) -> dict:
    """Values for the onboarding window. No key, no mail text, no notes text."""
    current = paiste or {}
    models = current.get("models") if isinstance(current.get("models"), dict) else {}
    order = current.get("order") if isinstance(current.get("order"), list) else []
    locals_out = []
    apis_out = []
    app_name = ""
    for name in order:
        spec = models.get(name)
        if not isinstance(spec, dict):
            continue
        via = str(spec.get("via") or "http")
        model = str(spec.get("model") or "local")
        if via in {"claude", "codex", "cursor"} and not app_name:
            app_name = via
            continue
        if via == "ssh":
            host = str(spec.get("ssh_host") or "")
            remote = str(spec.get("remote_url") or "")
            if host and remote:
                locals_out.append(_split_local("ssh " + host + " " + remote, model))
        elif via == "http":
            base = str(spec.get("base_url") or "")
            if _loopback(base):
                locals_out.append(_split_local(base, model))
            elif base:
                apis_out.append({
                    "url": base,
                    "model": model,
                    "has_key": _key_path_ok(str(spec.get("key_file") or "")),
                })
    local = ""
    local_model = "local"
    api_url = ""
    api_model = ""
    has_key = False
    if locals_out:
        local = str(locals_out[0].get("line") or "")
        local_model = str(locals_out[0].get("model") or "local")
    if apis_out:
        api_url = str(apis_out[0].get("url") or "")
        api_model = str(apis_out[0].get("model") or "")
        has_key = bool(apis_out[0].get("has_key"))
    if not local and not api_url and not app_name:
        local = str((found or {}).get("model") or "")
        if local:
            locals_out = [_split_local(local, "local")]
    if local and api_url:
        link = "both"
    elif api_url:
        link = "api"
    elif local:
        link = "local"
    else:
        link = "api"

    def mode(key: str) -> str:
        value = str(current.get(key) or "off")
        return value if value in {"off", "front", "folder"} else "off"

    return {
        "link": "app" if app_name else link,
        "app": app_name,
        "local": local,
        "local_model": local_model,
        "locals": locals_out,
        "api_url": api_url,
        "api_model": api_model,
        "apis": apis_out,
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


def _same_endpoint(old: dict, spec: dict) -> bool:
    keys = ("ssh_host", "remote_url", "base_url")
    return (str(old.get("via") or "http") == str(spec.get("via") or "http")
            and all(str(old.get(key) or "") == str(spec.get(key) or "") for key in keys))


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
    if link not in {"local", "api", "both", "app"}:
        raise BoardError("Choose Claude, Codex, Cursor, or custom.")
    out = {key: current[key] for key in _KEEP if key in current}
    out["enabled"] = True
    out["copy"] = True
    _one_surface(out, "mail", "email_dir", str(answers.get("mail") or ""), "")
    _one_surface(out, "chat", "chat_dir", str(answers.get("chat") or ""), "")
    _one_surface(out, "notes", "notes_dir", str(answers.get("notes") or ""), str(answers.get("notes_path") or ""))
    _one_surface(out, "calendar", "calendar_dir", str(answers.get("calendar") or ""), str(answers.get("calendar_path") or ""))
    out.setdefault("obsidian_vault", "")
    if link == "app":
        name = str(answers.get("app") or "").strip().lower()
        if name not in _APP_NAMES:
            raise BoardError("Choose Claude, Codex, Cursor, or custom.")
        binary = str(answers.get("bin") or "").strip() or app_bin(name)
        if not binary.startswith("/") or ".." in binary:
            raise BoardError("That app is not on this Mac.")
        swapped = use_app(current, name, binary)
        models = dict(swapped["models"])
        behind = [hop for hop in swapped["order"] if hop != name]
        for index, (line, model) in enumerate(_local_rows(answers)):
            hop = parse_hop(line, index)
            if not hop:
                raise BoardError("That local address was not kept. Use a URL, or ssh host and the address on that Mac.")
            _keep_model(current, hop, model)
            named = _named(current, hop["spec"], hop["name"])
            # Same endpoint already kept: update it in place. A re-run must not add ssh2, ssh22...
            slot = named if named in models and _same_endpoint(models[named], hop["spec"]) else _unique(named, models, hop["name"])
            models[slot] = hop["spec"]
            if slot not in behind and slot != name:
                behind.append(slot)
        out["order"] = _order_hops([name] + behind, models)
        out["models"] = models
        out["preferred"] = name
        return out
    wanted = str(answers.get("app") or "").strip().lower()
    if wanted in _APP_NAMES:
        out["preferred"] = wanted
    order = []
    models = {}
    if link in {"local", "both"}:
        rows = _local_rows(answers)
        if not rows:
            raise BoardError("Add a local model, or choose API.")
        for index, (line, model) in enumerate(rows):
            hop = parse_hop(line, index)
            if not hop:
                raise BoardError("That local address was not kept. Use a URL, or ssh host and the address on that Mac.")
            _keep_model(current, hop, model)
            name = _unique(_named(current, hop["spec"], hop["name"]), models, hop["name"])
            order.append(name)
            models[name] = hop["spec"]
    if link in {"api", "both"}:
        rows = _api_rows(answers)
        if not rows:
            raise BoardError("Add the API address, or choose local.")
        old_models = current.get("models") if isinstance(current.get("models"), dict) else {}
        skipped_hosts: list = []
        kept_before = len(order)
        for index, row in enumerate(rows):
            url = row["url"]
            if not _url_ok(url) or _loopback(url):
                raise BoardError("The API address is a service, not a local server.")
            model = row["model"]
            if not _model_ok(model):
                raise BoardError("Name the API model.")
            key_file = row["key_file"]
            if key_file and not _key_path_ok(key_file):
                raise BoardError("That key was not kept.")
            if not key_file:
                for old in old_models.values():
                    if isinstance(old, dict) and old.get("base_url") == url and _key_path_ok(str(old.get("key_file") or "")):
                        key_file = str(old.get("key_file"))
                        break
            if not _key_path_ok(key_file) and needs_key(url):
                # A paid service with no key can only fail. Never put it in the order.
                skipped_hosts.append(urlparse(url).hostname or url)
                continue
            spec = {
                "via": "http",
                "base_url": url,
                "model": model,
                "ssh_host": "",
                "remote_url": "",
                "key_file": key_file if _key_path_ok(key_file) else "",
            }
            fallback = "api" if index == 0 else "api" + str(index + 1)
            name = _unique(_named(current, spec, fallback), models, fallback)
            order.append(name)
            models[name] = spec
        if skipped_hosts and len(order) == kept_before and not order:
            raise BoardError("Paste the API key for " + skipped_hosts[0] + ", or choose local.")
    order, models = _prefer_connected_app(current, order, models, answers)
    out["order"] = order
    out["models"] = models
    return out


_APP_NAMES = ("claude", "codex", "cursor")


def _wrapper(path: str) -> bool:
    """Skip the local-first shim. paisty orders the hops itself."""
    try:
        head = Path(path).read_text(encoding="utf-8", errors="replace")[:180]
    except OSError:
        return False
    return "local-first-wrapper" in head


def app_bin(name: str) -> str:
    """Absolute path. The menu app does not inherit a full shell path."""
    import os
    home = str(Path.home())
    folders = [home + "/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
    folders.extend(part for part in os.environ.get("PATH", "").split(":") if part)
    names = ("agent", "cursor-agent") if name == "cursor" else (name,)
    for folder in folders:
        for candidate in names:
            found = str(Path(folder) / candidate)
            if not os.path.isfile(found) or not os.access(found, os.X_OK):
                continue
            if ".." in found or _wrapper(found):
                continue
            return found
    return ""


def _local_spec(spec: dict) -> bool:
    via = str(spec.get("via") or "http")
    if via == "ssh":
        return True
    return via == "http" and _loopback(str(spec.get("base_url") or ""))


def _premium_spec(spec: dict) -> bool:
    via = str(spec.get("via") or "")
    if via in _APP_NAMES:
        return True
    base = str(spec.get("base_url") or "")
    return via == "http" and bool(base) and not _loopback(base)


def _premium_key(spec: dict) -> str:
    via = str(spec.get("via") or "")
    if via in _APP_NAMES:
        return "app:" + via
    return "api:" + str(spec.get("base_url") or "")


def _prefer_connected_app(current: dict, order: list, models: dict, answers: dict) -> tuple[list, dict]:
    """The login they picked is one hop. claude and codex lead, a local or brain hop is next, and cursor is last."""
    wanted = str((answers or {}).get("app") or "").strip().lower()
    if wanted in _APP_NAMES:
        binary = str((answers or {}).get("bin") or "").strip() or app_bin(wanted)
        if binary.startswith("/") and ".." not in binary:
            order = [name for name in order if str((models.get(name) or {}).get("via") or "") not in _APP_NAMES]
            models = {name: spec for name, spec in models.items() if name in order}
            models[wanted] = {
                "via": wanted,
                "bin": binary,
                "model": wanted,
                "base_url": "",
                "ssh_host": "",
                "remote_url": "",
            }
            order.append(wanted)
            kept = dict(current or {})
            old_models = current.get("models") if isinstance(current.get("models"), dict) else {}
            kept["models"] = {
                name: spec for name, spec in old_models.items()
                if str((spec or {}).get("via") or "") not in _APP_NAMES
            }
            kept["order"] = [
                name for name in (current.get("order") or [])
                if name in kept["models"]
            ]
            current = kept
    return _premium_after_local(current, order, models)


def _hop_rank(spec: dict) -> int:
    """claude, then codex, then other paid APIs, then local or brain, then cursor."""
    via = str((spec or {}).get("via") or "")
    if via == "claude":
        return 0
    if via == "codex":
        return 1
    if via == "cursor":
        return 4
    if _local_spec(spec or {}):
        return 3
    if _premium_spec(spec or {}):
        return 2
    return 5


def _order_hops(order: list, models: dict) -> list:
    return sorted(list(order), key=lambda name: _hop_rank(models.get(name) or {}))


def _premium_after_local(current: dict, order: list, models: dict) -> tuple[list, dict]:
    """claude, then codex, then other paid APIs, then a local or brain hop, then cursor."""
    locals_first = []
    premiums = []
    rest = []
    for name in order:
        spec = models.get(name) or {}
        if _local_spec(spec):
            locals_first.append(name)
        elif _premium_spec(spec):
            premiums.append(name)
        else:
            rest.append(name)
    if locals_first:
        # A local hop must not drop claude, codex, or cursor that were already saved.
        seen = {_premium_key(models[name]) for name in premiums}
        old = current.get("models") if isinstance(current.get("models"), dict) else {}
        for old_name in current.get("order") or []:
            spec = old.get(old_name)
            if not isinstance(spec, dict) or not _premium_spec(spec):
                continue
            key = _premium_key(spec)
            if key in seen:
                continue
            slot = old_name if old_name not in models else _unique(old_name, models, old_name)
            models[slot] = dict(spec)
            premiums.append(slot)
            seen.add(key)
    return _order_hops(premiums + rest + locals_first, models), models


def use_app(current: dict, name: str, binary: str = "") -> dict:
    """Add one signed-in app. Mail, notes, and context stay. Order is claude, codex, local or brain, then cursor."""
    picked = (name or "").strip().lower()
    if picked not in _APP_NAMES:
        raise BoardError("Choose Claude, Codex, or Cursor.")
    path = (binary or "").strip() or app_bin(picked)
    if not path.startswith("/") or ".." in path:
        raise BoardError("That app is not on this Mac.")
    out = dict(current or {})
    old_models = current.get("models") if isinstance(current.get("models"), dict) else {}
    models = {}
    order = []
    for hop in current.get("order") or []:
        spec = old_models.get(hop)
        if not isinstance(spec, dict):
            continue
        if _local_spec(spec) or (_premium_spec(spec) and str(spec.get("via") or "") not in _APP_NAMES):
            order.append(hop)
            models[hop] = spec
    models[picked] = {
        "via": picked,
        "bin": path,
        "model": picked,
        "base_url": "",
        "ssh_host": "",
        "remote_url": "",
    }
    order.append(picked)
    order, models = _premium_after_local(current, order, models)
    out["enabled"] = True
    out["copy"] = True
    out["order"] = order
    out["models"] = models
    return out


def start_login(name: str) -> str:
    """Open the browser sign-in for this Mac. No window text is sent."""
    picked = (name or "").strip().lower()
    binary = app_bin(picked)
    if not binary:
        return "missing"
    if picked == "claude":
        argv = [binary, "auth", "login"]
    elif picked == "codex":
        argv = [binary, "login"]
    elif picked == "cursor":
        argv = [binary, "login"]
    else:
        return "missing"
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return "browser"


def app_status(name: str) -> str:
    """signed-in, login, or missing. No account name and no token."""
    if name not in _APP_NAMES:
        return "missing"
    binary = app_bin(name)
    if not binary:
        return "missing"
    if name == "claude":
        argv = [binary, "auth", "status"]
    elif name == "codex":
        argv = [binary, "login", "status"]
    else:
        argv = [binary, "status"]
    try:
        done = subprocess.run(argv, capture_output=True, check=False, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        return "login"
    if done.returncode != 0:
        return "login"
    text = ((done.stdout or b"") + b"\n" + (done.stderr or b"")).decode("utf-8", "replace")
    low = text.lower()
    # "Not logged in" contains "logged in".
    if "not logged in" in low or "logged out" in low:
        return "login"
    if name == "claude":
        try:
            doc = json.loads(done.stdout.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            doc = {}
        return "signed-in" if doc.get("loggedIn") is True else "login"
    if "logged in" in low:
        return "signed-in"
    return "login"


def _keep_model(current: dict, hop: dict, model: str) -> None:
    if _model_ok(model):
        hop["spec"]["model"] = model
        return
    name = _named(current, hop["spec"], "")
    old_models = current.get("models") if isinstance(current.get("models"), dict) else {}
    old = old_models.get(name)
    if isinstance(old, dict) and _model_ok(str(old.get("model") or "")):
        hop["spec"]["model"] = str(old.get("model"))
        return
    hop["spec"]["model"] = "local"


def _unique(name: str, models: dict, fallback: str) -> str:
    if name and name not in models:
        return name
    base = fallback or "local"
    if base not in models:
        return base
    number = 2
    while (base + str(number)) in models:
        number += 1
    return base + str(number)


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
    io.say("A local model has to already be running, on this computer or on a powerful machine you can reach. paisty does not start it. A paid model works anywhere you have an internet connection. Local, paid, or both.")
    io.say("Any app in front is read once, then forgotten. skip leaves one off. Mail and chat can use the text already on screen, or a folder you export.")
    io.say("Use that one thread plus a few matching notes. Calendar shows a conflict or the matching event. You still accept, book, and send. Leave money, legal, HR, and conflict to yourself.")
    found = found or {}
    if found.get("obsidian"):
        io.say("Found an Obsidian vault at " + found["obsidian"] + ". Use it for notes, or skip.")
    io.say("A knowledge base is optional. It is a program that prints notes. Skip it if you do not have one.")
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
    io.say("Add models in the order Paiste should try them. A local server is http://127.0.0.1:18080/v1. A model already running on another machine is: ssh hostname http://127.0.0.1:18080/v1. A paid service is an https address that ends in /v1. A blank line stops.")
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
        # Keep claude, codex, and cursor. A blank stop must not leave only the local line.
        order, models = _premium_after_local(dict(paiste or {}), order, models)
        out["order"] = order
        out["models"] = models
    return out
