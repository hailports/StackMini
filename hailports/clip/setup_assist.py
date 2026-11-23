"""Free setup-assist model calls. Plugsky first, Cursor as fallback.

The model name and URL come from the environment (or defaults). The key is
read from the environment or from ~/.config/free-llm/keys.env and is never
logged, printed, or returned.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_ENGINE = "plugsky"
DEFAULT_MODEL = "plugsky-ultra"
DEFAULT_URL = "https://api.plugsky.com/v1/chat/completions"
DEFAULT_FALLBACK = "cursor"
KEYS_ENV = Path.home() / ".config" / "free-llm" / "keys.env"
WEAK_MARKERS = ("lite", "micro", "gpu-27b")
_SECRET_NOTE = None


def _secret_note():
    global _SECRET_NOTE
    if _SECRET_NOTE is None:
        from hailports.clip.board import _SECRET_NOTE as pat
        _SECRET_NOTE = pat
    return _SECRET_NOTE


def setup_config(env=None) -> dict:
    """Resolved engine settings. No key is included."""
    src = env if env is not None else os.environ
    url = str(src.get("PAISTY_SETUP_URL") or src.get("SETUP_MODEL_URL") or DEFAULT_URL).strip()
    model = str(src.get("PAISTY_SETUP_MODEL") or src.get("SETUP_MODEL_NAME") or DEFAULT_MODEL).strip()
    engine = str(src.get("PAISTY_SETUP_ENGINE") or src.get("SETUP_ENGINE") or DEFAULT_ENGINE).strip() or DEFAULT_ENGINE
    fallback = str(src.get("PAISTY_SETUP_FALLBACK") or src.get("SETUP_FALLBACK_ENGINE") or DEFAULT_FALLBACK).strip() or DEFAULT_FALLBACK
    return {"engine": engine, "model": model, "url": url, "fallback": fallback}


def _read_keys_env(path: Path | None = None) -> dict[str, str]:
    p = path or Path(os.environ.get("FREE_LLM_KEYS") or KEYS_ENV)
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k:
            out[k] = v
    return out


def load_setup_key(name: str, path: Path | None = None) -> str:
    """Return one named key, or empty. Never logs the value."""
    env_val = str(os.environ.get(name) or "").strip()
    if env_val:
        return env_val
    return str(_read_keys_env(path).get(name) or "").strip()


def weak_plugsky_engine(served: str) -> bool:
    text = (served or "").lower()
    return any(mark in text for mark in WEAK_MARKERS)


def alert_weak_engine(served: str) -> None:
    """Record a weak served engine without printing a key or path."""
    line = (
        "paisty setup assist: requested "
        + DEFAULT_MODEL
        + " but x-plugsky-served-model="
        + (served or "")
        + "\n"
    )
    dest = Path.home() / ".config" / "paiste" / "setup-assist.log"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(line)
        try:
            dest.chmod(0o600)
        except OSError:
            pass
    except OSError:
        try:
            sys.stderr.write(line)
        except OSError:
            pass


def setup_facts(mail: bool, calendar: bool, app: str) -> str:
    name = app if app in {"claude", "codex", "cursor"} else ""
    lines = ["A click searches this folder."]
    if mail:
        lines.append("Mail is connected in the AI. A click may read the mail window that is already open.")
    if calendar:
        lines.append("Calendar is connected in the AI.")
    if name:
        lines.append("The click asks " + name[:1].upper() + name[1:] + ".")
    lines.append("Recent drafts are kept in a local history on your computer. The notes remain in this folder.")
    return "\n".join(lines)


def _finish_note(text: str) -> str:
    note = (text or "").strip()
    if len(note) < 40 or _secret_note().search(note):
        return ""
    if "does not keep" not in note.lower() and "notes stay in this folder" not in note.lower() and "notes remain in this folder" not in note.lower():
        note += "\n\npaisty does not keep any data. The notes stay in this folder."
    return note if note.endswith("\n") else note + "\n"


def plugsky_chat(messages: list[dict], *, max_tokens: int = 350, temperature: float = 0.2, post=None) -> dict:
    """One OpenAI-compatible chat call. Returns text, served_model, and flags. No key in the result."""
    cfg = setup_config()
    key = load_setup_key("PLUGSKY_API_KEY")
    url = cfg["url"]
    model = cfg["model"]
    result = {"ok": False, "text": "", "served_model": "", "alert": "", "engine": "plugsky", "status": 0}
    if not key or not url.startswith("https://"):
        return result
    payload = json.dumps({
        "model": model,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "messages": messages,
    }).encode("utf-8")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "paisty/1"}
    raw = ""
    served = ""
    status = 0
    if post is not None:
        raw, served, status = post(url, payload, headers)
    else:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                status = getattr(resp, "status", 200) or 200
                served = str(resp.headers.get("x-plugsky-served-model") or "")
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = int(exc.code or 0)
            served = str(getattr(exc, "headers", {}).get("x-plugsky-served-model") or "") if getattr(exc, "headers", None) else ""
            try:
                raw = exc.read().decode("utf-8", "replace")
            except OSError:
                raw = ""
        except (OSError, TimeoutError, urllib.error.URLError):
            return result
    try:
        doc = json.loads(raw or "")
    except json.JSONDecodeError:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    if not served:
        served = str(doc.get("model") or "")
    text = ""
    choices = doc.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            text = str(msg.get("content") or "")
    alert = ""
    if weak_plugsky_engine(served):
        alert = "weak_engine:" + served
        alert_weak_engine(served)
    result.update({
        "ok": 200 <= int(status or 0) < 300 and len((text or "").strip()) >= 1,
        "text": text,
        "served_model": served,
        "alert": alert,
        "status": int(status or 0),
    })
    return result


def plugsky_setup_text(facts: str, run=None) -> str:
    """Write the notes-folder starter through Plugsky. Empty on failure."""
    prompt = (
        "Write a short markdown note for a notes folder on this Mac. "
        "Use only the facts below. Do not invent messages, accounts, or passwords. "
        "No code fence. Do not mention the model vendor.\n\n"
        + (facts or "").strip()
        + "\n\npaisty does not keep any data. The notes stay in this folder."
    )
    if run is not None:
        raw = run(prompt) or ""
        if isinstance(raw, dict):
            text = str(raw.get("text") or "")
            served = str(raw.get("served_model") or "")
            if weak_plugsky_engine(served):
                alert_weak_engine(served)
        else:
            text = str(raw)
        return _finish_note(text)
    messages = [
        {"role": "system", "content": "Write a short markdown note. Use only the facts. Do not invent accounts, messages, or passwords."},
        {"role": "user", "content": (facts or "").strip()},
    ]
    hit = plugsky_chat(messages)
    return _finish_note(hit.get("text") or "") if hit.get("ok") else ""


def assisted_setup_text(facts: str, run_plugsky=None, run_cursor=None) -> str:
    """Plugsky first, then the Cursor setup helper."""
    note = plugsky_setup_text(facts, run=run_plugsky)
    if note:
        return note
    from hailports.clip.board import cursor_setup_text
    return cursor_setup_text(facts, run=run_cursor)


def build_setup_cloud(fallback=None):
    """Chat client for setup only. Plugsky when a key is present, else fallback.

    Does not change a saved draft-engine order. Returns fallback unchanged
    when no Plugsky key is configured.
    """
    from hailports.providers.base import Reply

    class _SetupEngine:
        def __init__(self, inner):
            self.inner = inner
            self.last = {"engine": "", "served_model": "", "alert": ""}

        def chat(self, messages, max_tokens=500, temperature=0):
            hit = plugsky_chat(list(messages), max_tokens=max_tokens, temperature=temperature)
            if hit.get("ok") and str(hit.get("text") or "").strip():
                self.last = {"engine": "plugsky", "served_model": hit.get("served_model") or "", "alert": hit.get("alert") or ""}
                return Reply(
                    hit["text"],
                    "plugsky",
                    hit.get("served_model") or setup_config()["model"],
                    0,
                    0,
                    {"served_model": hit.get("served_model"), "alert": hit.get("alert")},
                )
            if self.inner is not None:
                self.last = {"engine": "fallback", "served_model": "", "alert": ""}
                return self.inner.chat(messages, max_tokens=max_tokens, temperature=temperature)
            self.last = {"engine": "none", "served_model": "", "alert": ""}
            return Reply("", "plugsky", "", 0, 0, {})

    if load_setup_key("PLUGSKY_API_KEY"):
        return _SetupEngine(fallback)
    return fallback
