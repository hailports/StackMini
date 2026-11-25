"""The menu's model picker: the chat models the drafting server offers, and switching between them.

A switch edits the one `model:` line in route.yaml, so comments and hand edits stay exactly as
they were. It is refused unless reading the file back shows that one change and nothing else.
"""

from __future__ import annotations

import copy
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from hailports.config import _scalar, loads
from hailports.clip.board import aux_model, model_ids
from hailports.clip.hops import _url_ok, endpoints_from, needs_key

LIMIT = 12
_HERE = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def route_path() -> Path:
    raw = os.environ.get("PAISTE_ROUTE") or ""
    return Path(raw) if raw else Path.home() / ".config" / "paiste" / "route.yaml"


def engine(cfg: dict) -> tuple[dict | None, list[str]]:
    """The first engine a draft tries that is a model server (plain http, not a paid service),
    and where its model name lives in route.yaml ([] when it is not saved there)."""
    paiste = cfg.get("paiste") if isinstance(cfg.get("paiste"), dict) else {}
    models = paiste.get("models") if isinstance(paiste.get("models"), dict) else {}
    for hop in endpoints_from(cfg):
        if hop.get("via") != "http" or needs_key(str(hop.get("base_url") or "")):
            continue
        name = str(hop.get("name") or "")
        if isinstance(models.get(name), dict):
            return hop, ["paiste", "models", name, "model"]
        if name in {"llm", "default"} and isinstance(cfg.get("llm"), dict):
            return hop, ["llm", "model"]
        return hop, []
    return None, []


def _get(url: str, timeout: float = 2.0) -> str:
    if not _url_ok(url):
        return ""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if int(resp.status) != 200:
                return ""
            return resp.read(262144).decode("utf-8", "replace")
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        return ""


def chat_models(base_url: str, read=None) -> list[str]:
    """Models the server lists that can write a reply, in the server's order."""
    body = (read or _get)(base_url.rstrip("/") + "/models")
    return [name for name in model_ids(body, limit=64) if not aux_model(name)][:LIMIT]


def listing(cfg: dict, read=None) -> dict:
    hop, path = engine(cfg)
    if hop is None:
        return {"ok": False, "why": "No model server in your setup. Open setup… to add one.", "models": []}
    base = str(hop.get("base_url") or "")
    host = (urlparse(base).hostname or "").lower()
    where = "this Mac" if host in _HERE else host
    current = str(hop.get("model") or "")
    names = chat_models(base, read)
    if not names:
        return {"ok": False, "why": "The model server on " + where + " did not answer.", "models": [], "current": current}
    return {"ok": True, "models": names, "current": current, "where": where, "switchable": bool(path)}


def set_value(text: str, path: list[str], value: str) -> str | None:
    """Set one scalar in place. None unless re-reading the result shows exactly that change."""
    try:
        before = loads(text)
    except ValueError:
        return None
    lines = text.split("\n")
    stack: list[tuple[int, str]] = []
    hit = parent = child_indent = None
    for i, line in enumerate(lines):
        body = line.strip()
        if not body or body.startswith("#") or body.startswith("- ") or ":" not in body:
            continue
        indent = len(line) - len(line.lstrip(" "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, body.partition(":")[0].strip()))
        keys = [key for _, key in stack]
        if keys == path:
            hit = i
        elif keys == path[:-1]:
            parent = (i, indent)
        elif parent is not None and child_indent is None and len(keys) == len(path) and keys[:-1] == path[:-1]:
            child_indent = indent
    if hit is not None:
        indent = len(lines[hit]) - len(lines[hit].lstrip(" "))
        lines[hit] = " " * indent + path[-1] + ": " + _scalar(value)
    elif parent is not None:
        row, indent = parent
        pad = child_indent if child_indent is not None else indent + 2
        lines.insert(row + 1, " " * pad + path[-1] + ": " + _scalar(value))
    else:
        return None
    out = "\n".join(lines)
    want = copy.deepcopy(before)
    node = want
    for key in path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return None
    node[path[-1]] = value
    try:
        return out if loads(out) == want else None
    except ValueError:
        return None


def use(cfg: dict, name: str, dest: Path | None = None, read=None) -> dict:
    """Switch the drafting model to `name`, one of the names the server lists."""
    dest = dest or route_path()
    hop, path = engine(cfg)
    if hop is None or not path or not dest.is_file():
        return {"ok": False, "why": "Open setup… and save a model first."}
    if name not in chat_models(str(hop.get("base_url") or ""), read):
        return {"ok": False, "why": name + " is not a chat model this server lists."}
    old = dest.read_text(encoding="utf-8")
    new = set_value(old, path, name)
    if new is None:
        return {"ok": False, "why": "route.yaml has a shape paisty will not rewrite. Set model: " + name + " there by hand."}
    if new != old:
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(new, encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(dest)
    return {"ok": True, "model": name}
