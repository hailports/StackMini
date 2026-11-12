"""Try Paiste models in a fixed order. A hop is local HTTP or ssh.

Nothing is sent. A secret in the scene stops the route before any hop.
A hop that is down is skipped. The first non-empty reply wins.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from hailports.chat_badge import secret_hold
from hailports.llm import ModelOffline, chat

_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def _url_ok(url: str) -> bool:
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        return False
    if any(ch in raw for ch in " \t\r\n\"'`"):
        return False
    parsed = urlparse(raw)
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return False
    return bool(parsed.hostname)


def endpoints_from(cfg: dict) -> list[dict]:
    """Ordered hops. An empty or invalid order falls back to llm.base_url."""
    paiste = cfg.get("paiste") or {}
    models = paiste.get("models") or {}
    order = paiste.get("order") or []
    hops = []
    if isinstance(models, dict):
        for name in order:
            spec = models.get(name)
            if not isinstance(spec, dict):
                continue
            via = str(spec.get("via") or "http").strip().lower()
            model = str(spec.get("model") or "local")
            if via == "ssh":
                host = str(spec.get("ssh_host") or "").strip()
                remote = str(spec.get("remote_url") or "").strip()
                if not _HOST.match(host) or not _url_ok(remote):
                    continue
                hops.append({"name": str(name), "via": "ssh", "ssh_host": host, "base_url": remote, "model": model})
            elif via in {"cursor", "claude", "codex"}:
                binary = str(spec.get("bin") or "").strip()
                if not binary.startswith("/") or ".." in binary:
                    continue
                hops.append({"name": str(name), "via": via, "bin": binary, "model": via})
            else:
                base = str(spec.get("base_url") or "").strip()
                if not _url_ok(base):
                    continue
                hop = {"name": str(name), "via": "http", "base_url": base, "model": model}
                key_file = str(spec.get("key_file") or "")
                if key_file.startswith("/") and ".." not in key_file:
                    hop["key_file"] = key_file
                hops.append(hop)
    if hops:
        return hops
    llm = cfg.get("llm") or {}
    return [{
        "name": "default",
        "via": "http",
        "base_url": str(llm.get("base_url") or "http://127.0.0.1:18080/v1"),
        "model": str(llm.get("model") or "local"),
    }]


def ssh_argv(host: str, remote_url: str) -> list[str]:
    if not _HOST.match(host) or not _url_ok(remote_url):
        raise ModelOffline("bad ssh hop")
    url = remote_url.rstrip("/") + "/chat/completions"
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        host, "curl", "-sS", "-m", "110",
        "-H", "Content-Type: application/json",
        "-d", "@-", url,
    ]


def _reply_text(doc: dict) -> str:
    try:
        return str(doc["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return str(doc.get("content") or "")


def _user_text(messages: list[dict]) -> str:
    return "\n".join(str(message.get("content") or "") for message in messages)


def read_key(endpoint: dict) -> str:
    """The API key stays in a file on this Mac. It is not part of the scene."""
    path = str(endpoint.get("key_file") or "")
    if not path.startswith("/") or ".." in path or len(path) > 240:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _http(endpoint: dict, messages: list[dict]) -> str:
    system = ""
    user = ""
    for message in messages:
        if message.get("role") == "system":
            system = str(message.get("content") or "")
        elif message.get("role") == "user":
            user = str(message.get("content") or "")
    return chat(endpoint["base_url"], endpoint["model"], user, system=system, key=read_key(endpoint)).text


def _ssh(endpoint: dict, messages: list[dict], run=None) -> str:
    payload = json.dumps({
        "model": endpoint["model"],
        "messages": messages,
        "temperature": 0.2,
    }).encode("utf-8")
    argv = ssh_argv(endpoint["ssh_host"], endpoint["base_url"])
    if run is not None:
        raw = run(argv, payload)
    else:
        done = subprocess.run(argv, input=payload, capture_output=True, check=False)
        if done.returncode != 0:
            raise ModelOffline("ssh hop failed")
        raw = done.stdout
    try:
        doc = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return _reply_text(doc)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, UnicodeError) as exc:
        raise ModelOffline("ssh hop reply") from exc


def _bin_ok(binary: str) -> bool:
    return binary.startswith("/") and ".." not in binary


def claude_argv(binary: str, system: str, user: str) -> list[str]:
    """Print-only. Plan mode does not edit files."""
    argv = [
        binary, "-p", "--output-format", "text",
        "--permission-mode", "plan", "--permission-prompts", "none",
    ]
    if system:
        argv += ["--system-prompt", system[:6000]]
    argv.append(user[:6000])
    return argv


def codex_argv(binary: str, outfile: str) -> list[str]:
    """Read-only sandbox. The reply is written to outfile."""
    return [
        binary, "exec", "--ephemeral", "--sandbox", "read-only",
        "--skip-git-repo-check", "--color", "never", "-C", "/tmp",
        "--output-last-message", outfile, "-",
    ]


_CURSOR_META = re.compile(
    r"ask mode|switch to agent|didn.?t stage|pasteable draft|i can.?t post|i cannot post",
    re.I,
)


def cursor_prompt(system: str, user: str) -> str:
    """The reply only. Cursor was narrating its own mode and that got copied."""
    rules = (system or "").strip()[:3500]
    body = (user or "").strip()[:4000]
    lead = (
        "You are viewing a communication on the screen. "
        "Read the whole conversation, not only the last line. "
        "Use what was already said. "
        "Search all connected mail, all connected calendar events, and all connected notes. "
        "Do not stop at a sample. Use what matches this conversation. "
        "Do not invent a fact that is not on screen and not in those. "
        "Draft the reply to what is still open. "
        "No title. No code fence. "
        "Do not mention Cursor, ask mode, or the app. "
        "Do not say you staged, posted, or sent anything.\n\n"
    )
    return (rules + "\n\n" + lead + body).strip()[:8000]


def only_reply(text: str) -> str:
    body = re.sub(r"(?is)^pasteable draft:\s*", "", (text or "").strip())
    body = body.strip().strip("`").strip()
    if not body or _CURSOR_META.search(body):
        return ""
    return body


def cursor_argv(binary: str, prompt: str) -> list[str]:
    """Ask mode does not edit files. --trust only lets that read-only run start."""
    return [binary, "-p", "--mode", "ask", "--trust", "--output-format", "text", prompt[:8000]]


def _split_messages(messages: list[dict]) -> tuple[str, str]:
    system = ""
    user = ""
    for message in messages:
        if message.get("role") == "system" and not system:
            system = str(message.get("content") or "")
        elif message.get("role") == "user":
            user = str(message.get("content") or "")
    return system, user


def _claude(endpoint: dict, messages: list[dict]) -> str:
    binary = str(endpoint.get("bin") or "")
    if not _bin_ok(binary):
        raise ModelOffline("claude")
    system, user = _split_messages(messages)
    done = subprocess.run(claude_argv(binary, system, user), capture_output=True, check=False, timeout=110)
    if done.returncode != 0:
        raise ModelOffline("claude hop failed")
    return (done.stdout or b"").decode("utf-8", "replace").strip()


def _codex(endpoint: dict, messages: list[dict]) -> str:
    import tempfile
    binary = str(endpoint.get("bin") or "")
    if not _bin_ok(binary):
        raise ModelOffline("codex")
    system, user = _split_messages(messages)
    prompt = (system + "\n\n" + user).strip()[:8000]
    handle = tempfile.NamedTemporaryFile(prefix="paisty-codex-", delete=False)
    handle.close()
    try:
        done = subprocess.run(
            codex_argv(binary, handle.name),
            input=prompt.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=110,
        )
        if done.returncode != 0:
            raise ModelOffline("codex hop failed")
        try:
            text = Path(handle.name).read_text(encoding="utf-8")
        except OSError:
            text = (done.stdout or b"").decode("utf-8", "replace")
        return text.strip()
    finally:
        try:
            Path(handle.name).unlink()
        except OSError:
            pass


def _cursor(endpoint: dict, messages: list[dict]) -> str:
    """Ask the Cursor CLI already signed in on this Mac. Ask mode does not edit files."""
    binary = str(endpoint.get("bin") or "")
    if not _bin_ok(binary):
        raise ModelOffline("cursor")
    system, user = _split_messages(messages)
    done = subprocess.run(
        cursor_argv(binary, cursor_prompt(system, user)),
        cwd=str(Path.home()),
        capture_output=True,
        check=False,
        timeout=90,
    )
    if done.returncode != 0:
        raise ModelOffline("cursor hop failed")
    text = only_reply((done.stdout or b"").decode("utf-8", "replace"))
    if not text:
        raise ModelOffline("cursor talked about itself")
    return text


def complete(endpoints: list[dict], messages: list[dict], http=None, ssh=None) -> tuple[str, str]:
    """Return reply text and the hop name. Raises ModelOffline if every hop fails."""
    if secret_hold(_user_text(messages)):
        raise ModelOffline("secret")
    http_call = http or _http
    ssh_call = ssh or _ssh
    last = "offline"
    for endpoint in endpoints:
        try:
            via = endpoint.get("via")
            if via == "ssh":
                text = ssh_call(endpoint, messages)
            elif via == "claude":
                text = _claude(endpoint, messages)
            elif via == "codex":
                text = _codex(endpoint, messages)
            elif via == "cursor":
                text = _cursor(endpoint, messages)
            else:
                text = http_call(endpoint, messages)
        except Exception as exc:
            last = str(endpoint.get("name") or "?") + ":" + type(exc).__name__
            continue
        if str(text or "").strip():
            return str(text).strip(), str(endpoint.get("name") or "")
        last = str(endpoint.get("name") or "?") + ":empty"
    raise ModelOffline(last)
