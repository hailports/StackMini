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
        host, "curl", "-sS", "-m", "240",
        "-H", "Content-Type: application/json",
        # A paisty click is a person waiting. Ask a priority proxy to treat it as chat.
        "-H", "X-Chat-Priority: 1",
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
    system, user = same_context(*_split_messages(messages))
    return chat(endpoint["base_url"], endpoint["model"], user, system=system, key=read_key(endpoint)).text


def _ssh(endpoint: dict, messages: list[dict], run=None) -> str:
    """One retry on an empty or error reply: a busy local server can drop one request."""
    try:
        text = _ssh_once(endpoint, messages, run)
    except ModelOffline:
        text = ""
    if str(text or "").strip():
        return text
    import time as _time
    _time.sleep(1.5)
    return _ssh_once(endpoint, messages, run)


def _ssh_once(endpoint: dict, messages: list[dict], run=None) -> str:
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


# Claude sends this much system and this much user, as two fields. A joined
# prompt must not be shorter than that, or the thread at the end of the user
# turn is the part that gets cut.
_CONTEXT_CAP = 48000


def same_context(system: str, user: str) -> tuple[str, str]:
    """The system and user text the claude hop receives. Every hop uses this pair."""
    return (system or "")[:_CONTEXT_CAP], (user or "")[:_CONTEXT_CAP]


def joined_prompt(system: str, user: str, lead: str = "") -> str:
    """One string for a hop that has no separate system field.

    The user turn is first. It holds the on-screen thread at its tail, after
    the rules. Joining system-first and then keeping the first 8000 characters
    left the rules and dropped the thread, so the hop said it could not see
    the message.
    """
    system, user = same_context(system, user)
    chunks = [user]
    if (lead or "").strip():
        chunks.append(lead.strip())
    if system:
        chunks.append(system)
    return "\n\n".join(chunk for chunk in chunks if chunk).strip()


def claude_argv(binary: str, system: str, user: str) -> list[str]:
    """Print-only. No tools, so nothing can edit files. Plan mode made the model write a plan and an ExitPlanMode call as text."""
    system, user = same_context(system, user)
    argv = [
        binary, "-p", "--output-format", "text",
        "--permission-mode", "default", "--permission-prompts", "none",
        "--tools", "", "--no-session-persistence",
        # No MCP servers, claude.ai connectors, skills, or Chrome: the hop drafts text only.
        "--strict-mcp-config", "--disable-slash-commands", "--no-chrome",
        # No user CLAUDE.md, hooks, or settings: they cost ~25k tokens a click and steer the reply.
        "--setting-sources", "",
    ]
    if system:
        argv += ["--system-prompt", system]
    argv.append(user)
    return argv


def codex_argv(binary: str, outfile: str) -> list[str]:
    """Read-only sandbox. The reply is written to outfile."""
    return [
        binary, "exec", "--ephemeral", "--sandbox", "read-only",
        "--skip-git-repo-check", "--color", "never", "-C", "/tmp",
        "--output-last-message", outfile, "-",
    ]


# Every hop: a plan or staging note instead of the reply. Narrow, so "I staged the fix in QA" survives.
_HOP_META = re.compile(
    r"exitplanmode|\bi staged (?:the|this|a|your) (?:reply|draft|response)\b"
    r"|\bbelow as a plan\b|\bso nothing has been (?:posted|sent)\b",
    re.I,
)
_CURSOR_META = re.compile(
    r"ask mode|switch to agent|didn.?t stage|pasteable draft|i can.?t post|i cannot post|" + _HOP_META.pattern,
    re.I,
)
# The hop did not get the thread. Show nothing from it and try the next hop.
_CANT_SEE = re.compile(
    r"(?is)(?:"
    r"can(?:not|['’]t)\s+see\b.{0,80}\b(?:message|screen|thread|asked|on-screen|onscreen)"
    r"|(?:could you|can you|please)\s+send your message again"
    r"|send your message again"
    r")"
)


def blind_reply(text: str) -> bool:
    """True when the hop says it cannot see the message. That reply is not a draft."""
    return bool(_CANT_SEE.search(text or ""))


def cursor_prompt(system: str, user: str) -> str:
    """The reply only. Cursor was narrating its own mode and that got copied."""
    lead = (
        "You are viewing a communication on the screen. "
        "Read the whole conversation, not only the last line. "
        "Use what was already said. "
        "Answer every question that is still open. One short line each. Do not stop after the first. "
        "Search all connected mail, all connected calendar events, and all connected notes. "
        "Do not stop at a sample. Use what matches this conversation. "
        "Do not invent a fact that is not on screen and not in those. "
        "Draft the reply to what is still open. "
        "No title. No code fence. "
        "Do not mention Cursor, ask mode, or the app. "
        "Do not say you staged, posted, or sent anything."
    )
    return joined_prompt(system, user, lead)


def only_reply(text: str) -> str:
    body = re.sub(r"(?is)^pasteable draft:\s*", "", (text or "").strip())
    body = body.strip().strip("`").strip()
    if not body or _CURSOR_META.search(body):
        return ""
    return body


def cursor_argv(binary: str, prompt: str) -> list[str]:
    """Ask mode does not edit files. --trust only lets that read-only run start."""
    return [binary, "-p", "--mode", "ask", "--trust", "--output-format", "text", prompt]


def _split_messages(messages: list[dict]) -> tuple[str, str]:
    """System prompt, then every turn. A corrective retry must not drop the earlier ones."""
    system = ""
    turns = []
    for message in messages:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role == "system" and not system:
            system = content
            continue
        if content:
            turns.append((role, content))
    if len(turns) <= 1:
        return system, turns[0][1] if turns else ""
    *earlier, (_, final) = turns
    lines = []
    for role, content in earlier:
        if role == "assistant":
            lines.append("Previous draft:\n" + content[:1500])
        else:
            lines.append(content)
    # The hops cut the prompt from the end (codex keeps 4000), so the last turn goes in whole.
    room = max(0, 3900 - len(final) - 2)
    return system, ("\n\n".join(lines)[:room] + "\n\n" + final).strip()


def premium_env() -> dict:
    """A premium hop is premium. The local-first wrapper must not divert it to a local model."""
    import os
    env = dict(os.environ)
    env["LOCAL_FIRST_ROUTER"] = "0"
    return env


def _claude(endpoint: dict, messages: list[dict]) -> str:
    binary = str(endpoint.get("bin") or "")
    if not _bin_ok(binary):
        raise ModelOffline("claude")
    system, user = _split_messages(messages)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="paisty-claude-") as quiet:
        done = subprocess.run(
            claude_argv(binary, system, user), capture_output=True, check=False,
            timeout=110, cwd=quiet, env=premium_env(),
        )
    if done.returncode != 0:
        raise ModelOffline("claude hop failed")
    return (done.stdout or b"").decode("utf-8", "replace").strip()


def _codex(endpoint: dict, messages: list[dict]) -> str:
    import tempfile
    binary = str(endpoint.get("bin") or "")
    if not _bin_ok(binary):
        raise ModelOffline("codex")
    system, user = _split_messages(messages)
    # Stdin, not an argv slot. The user turn goes first so the thread is not
    # the part a length cut drops. No 8000-character slice: that slice kept
    # the rules and cut the on-screen text.
    prompt = joined_prompt(system, user)
    handle = tempfile.NamedTemporaryFile(prefix="paisty-codex-", delete=False)
    handle.close()
    try:
        done = subprocess.run(
            codex_argv(binary, handle.name),
            input=prompt.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=110,
            env=premium_env(),
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
    # An empty directory. The home directory made this hop read other files
    # instead of the thread in the prompt.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="paisty-cursor-") as quiet:
        done = subprocess.run(
            cursor_argv(binary, cursor_prompt(system, user)),
            cwd=quiet,
            capture_output=True,
            check=False,
            timeout=90,
            env=premium_env(),
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
            from hailports.clip.scene import strip_scaffold
            cleaned = strip_scaffold(str(text))
            if _HOP_META.search(cleaned):
                # Narration about staging or planning is not a reply. Try the next hop.
                last = str(endpoint.get("name") or "?") + ":meta"
                continue
            if blind_reply(cleaned):
                # A hop that cannot see the thread must not be pasted.
                last = str(endpoint.get("name") or "?") + ":blind"
                continue
            return str(text).strip(), str(endpoint.get("name") or "")
        last = str(endpoint.get("name") or "?") + ":empty"
    raise ModelOffline(last)
