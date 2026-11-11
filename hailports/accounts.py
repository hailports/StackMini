"""Headful Edge sessions for any account the person signs in to.

Setup does not ship a browser. It installs Microsoft Edge with the platform
package manager when asked, then parks one window in the lower right for
each account. The selected model writes the read and write instructions.
A tick may ingest messages or stage a draft. It cannot send, and it does
not run script text the model returns.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from hailports.chat_badge import secret_hold

SAFE_READ = "Read the visible messages on this signed-in page."
SAFE_WRITE = "Write the reply into the composer and leave it unsent."
_SLUG = re.compile(r"[^a-z0-9]+")
_PAIR = re.compile(r"^\s*([^=;\s]+)\s*[= ]\s*(https?://\S+)\s*$")
_BAD_WORD = ("password", "token", "api key", "secret", "cookie", "javascript:", "<script")


def corner_frame(screen_w: int, screen_h: int, index: int = 0, width: int = 420, height: int = 320) -> dict:
    """Flush the first window to the lower right. Later ones step in by 20px."""
    width = max(1, min(int(width), int(screen_w)))
    height = max(1, min(int(height), int(screen_h)))
    shift = max(0, int(index)) * 20
    return {
        "x": max(0, int(screen_w) - width - shift),
        "y": max(0, int(screen_h) - height - shift),
        "w": width,
        "h": height,
    }


def parse_screen(text: str) -> tuple[int, int]:
    raw = str(text or "1440x900").lower().replace(" ", "")
    if "x" not in raw:
        raise ValueError("screen must look like 1440x900")
    width, _, height = raw.partition("x")
    w, h = int(width), int(height)
    if w < 320 or h < 240 or w > 10000 or h > 10000:
        raise ValueError("screen size is out of range")
    return w, h


def _slug(label: str) -> str:
    slug = _SLUG.sub("-", label.strip().lower()).strip("-")
    return (slug or "account")[:32]


def _url_ok(url: str) -> bool:
    if not url or "@" in url or any(ch.isspace() for ch in url):
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return False
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        return False
    return True


def parse_accounts(text: str) -> list[dict]:
    """Lines or comma-separated pairs: name=https://host/login."""
    rows = []
    seen = set()
    chunks = re.split(r"[\n,;]+", str(text or ""))
    for chunk in chunks:
        if not chunk.strip():
            continue
        match = _PAIR.match(chunk.strip())
        if not match:
            raise ValueError("account must look like name=https://host/login")
        label, url = match.group(1).strip(), match.group(2).strip().rstrip(".,)")
        if not _url_ok(url):
            raise ValueError("account URL must be https, with no user or password in it")
        slug = _slug(label)
        if slug in seen:
            raise ValueError("duplicate account " + slug)
        seen.add(slug)
        rows.append({"label": slug, "url": url})
        if len(rows) >= 8:
            break
    return rows


def place_accounts(accounts: list[dict], screen: tuple[int, int], port_base: int = 9340) -> dict[str, dict]:
    placed = {}
    for index, account in enumerate(accounts):
        frame = corner_frame(screen[0], screen[1], index)
        host = urlparse(account["url"]).hostname or ""
        placed[account["label"]] = {
            "label": account["label"],
            "url": account["url"],
            "host": host,
            "port": port_base + index,
            "profile": "sessions/" + account["label"],
            "x": frame["x"],
            "y": frame["y"],
            "w": frame["w"],
            "h": frame["h"],
            "headful": True,
            "browser": "edge",
            "read_list": SAFE_READ,
            "write_fill": SAFE_WRITE,
            "send": False,
            "adapt": "pending",
        }
    return placed


def edge_candidates(os_name: str) -> list[str]:
    if os_name == "darwin":
        return ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    if os_name == "windows":
        return [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    return ["/usr/bin/microsoft-edge-stable", "/usr/bin/microsoft-edge"]


def find_edge(os_name: str, exists=None) -> str:
    check = exists or (lambda path: Path(path).exists())
    for path in edge_candidates(os_name):
        if check(path):
            return path
    found = shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable") or ""
    return found


def edge_install_argv(os_name: str, distro: str = "") -> list[str]:
    """One package-manager command. Linux uses the generated script instead."""
    if os_name == "darwin":
        return ["brew", "install", "--cask", "microsoft-edge"]
    if os_name == "windows":
        return ["winget", "install", "--id", "Microsoft.Edge", "-e", "--accept-package-agreements", "--accept-source-agreements"]
    if distro in {"fedora", "rhel"}:
        return ["sudo", "dnf", "install", "-y", "microsoft-edge-stable"]
    return ["sudo", "apt-get", "install", "-y", "microsoft-edge-stable"]


def edge_install_script(os_name: str, distro: str = "") -> str:
    """Vendor repo steps. The browser itself is not in this repository."""
    if os_name == "linux" and distro not in {"fedora", "rhel"}:
        return "\n".join([
            "#!/bin/sh",
            "set -eu",
            "curl -fsSL https://packages.microsoft.com/keys/microsoft.asc -o /tmp/microsoft-edge.asc",
            "sudo gpg --dearmor --yes -o /usr/share/keyrings/microsoft-edge.gpg /tmp/microsoft-edge.asc",
            'echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-edge.gpg] https://packages.microsoft.com/repos/edge stable main" | sudo tee /etc/apt/sources.list.d/microsoft-edge.list >/dev/null',
            "sudo apt-get update",
            "sudo apt-get install -y microsoft-edge-stable",
            "",
        ])
    if os_name == "linux":
        return "\n".join([
            "#!/bin/sh",
            "set -eu",
            "sudo rpm --import https://packages.microsoft.com/keys/microsoft.asc",
            "sudo dnf config-manager --add-repo https://packages.microsoft.com/yumrepos/edge",
            "sudo dnf install -y microsoft-edge-stable",
            "",
        ])
    return ""


def edge_record(os_name: str, distro: str = "", path: str = "") -> dict:
    argv = edge_install_argv(os_name, distro)
    return {
        "browser": "edge",
        "path": path,
        "install": " ".join(argv),
        "bundled": False,
        "headful": True,
        "corner": "lower-right",
    }


def edge_argv(binary: str, profile: str, url: str, frame: dict, port: int) -> list[str]:
    argv = [
        binary,
        "--new-window",
        "--user-data-dir=" + profile,
        "--remote-debugging-port=" + str(int(port)),
        "--window-position=%d,%d" % (int(frame["x"]), int(frame["y"])),
        "--window-size=%d,%d" % (int(frame["w"]), int(frame["h"])),
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    if any(part == "--headless" or part.startswith("--headless=") for part in argv):
        raise RuntimeError("Edge sessions are headful")
    return argv


def _instruction(text: str) -> str:
    clean = " ".join(str(text or "").split())[:180]
    low = clean.lower()
    if not clean or any(word in low for word in _BAD_WORD):
        return ""
    return clean


def build_account_prompt(sessions: dict) -> str:
    lines = [
        "Reply with one JSON object and no other text.",
        'Shape: {"accounts": {"name": {"read_list": "short line", "write_fill": "short line", "send": false}}}',
        "Each account is a signed-in page in a small Microsoft Edge window in the lower right.",
        "read_list says how to read visible messages. write_fill says how to fill a composer.",
        "send must be false. Do not include keys, cookies, passwords, or message text.",
        "Do not return script or javascript.",
    ]
    for name, item in sessions.items():
        lines.append(f"Account {name} host {item.get('host') or ''}")
    return "\n".join(lines)


def apply_adapter(sessions: dict, reply_text: str) -> dict:
    """Keep model read and write lines. send stays false even if the model says otherwise."""
    start = str(reply_text or "").find("{")
    end = str(reply_text or "").rfind("}")
    doc = None
    if start >= 0 and end > start:
        try:
            doc = json.loads(reply_text[start : end + 1])
        except json.JSONDecodeError:
            doc = None
    block = doc.get("accounts") if isinstance(doc, dict) else None
    if not isinstance(block, dict):
        block = {}
    updated = {}
    for name, item in sessions.items():
        row = dict(item)
        spec = block.get(name) if isinstance(block.get(name), dict) else {}
        read = _instruction(spec.get("read_list") or "")
        write = _instruction(spec.get("write_fill") or "")
        if read:
            row["read_list"] = read
        if write:
            row["write_fill"] = write
        row["send"] = False
        row["headful"] = True
        row["browser"] = "edge"
        row["adapt"] = "model" if read or write else "fallback"
        updated[name] = row
    return updated


def assist_accounts(cloud, sessions: dict, secret: str = "") -> dict:
    if not sessions:
        return sessions
    prompt = build_account_prompt(sessions)
    if cloud is None or (secret and len(secret) >= 6 and secret in prompt):
        return sessions
    try:
        reply = cloud.chat([{"role": "user", "content": prompt}], max_tokens=500, temperature=0)
        text = str(getattr(reply, "text", "") or "")
    except Exception:
        return sessions
    if secret and len(secret) >= 6 and secret in text:
        return sessions
    return apply_adapter(sessions, text)


def open_plan(sessions: dict, binary: str) -> list[list[str]]:
    plans = []
    for item in sessions.values():
        plans.append(edge_argv(
            binary,
            str(item.get("profile") or "sessions/account"),
            str(item.get("url") or ""),
            {"x": item.get("x"), "y": item.get("y"), "w": item.get("w"), "h": item.get("h")},
            int(item.get("port") or 9340),
        ))
    return plans


def _load_op(text: str) -> dict | None:
    start = str(text or "").find("{")
    end = str(text or "").rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        doc = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def interpret(session: dict, page_text: str, complete) -> dict:
    """Ask the selected model what this page is. Script text and send are dropped."""
    page = str(page_text or "")
    if secret_hold(page):
        return {"op": "none", "reason": "secret", "sent": False}
    low = page.lower()
    if "<script" in low or "javascript:" in low:
        return {"op": "none", "reason": "script", "sent": False}
    messages = [
        {
            "role": "system",
            "content": (
                "Follow the adapter. Reply with one JSON object. "
                'op is ingest, draft, or none. ingest has messages: [{from, text}]. '
                "draft has text. send is false. Do not return script."
            ),
        },
        {
            "role": "user",
            "content": json.dumps({
                "account": session.get("label") or "",
                "host": session.get("host") or "",
                "read_list": session.get("read_list") or SAFE_READ,
                "write_fill": session.get("write_fill") or SAFE_WRITE,
                "page": page[:4000],
            }),
        },
    ]
    try:
        raw = complete(messages)
    except Exception:
        return {"op": "none", "reason": "model", "sent": False}
    doc = _load_op(str(raw or ""))
    if not doc:
        return {"op": "none", "reason": "bad_op", "sent": False}
    op = str(doc.get("op") or "none")
    if op not in {"ingest", "draft", "none"} or doc.get("send") is True:
        return {"op": "none", "reason": "refused", "sent": False}
    if op == "draft":
        return {"op": "draft", "text": str(doc.get("text") or "")[:4000], "sent": False}
    if op == "ingest":
        messages_out = []
        for item in (doc.get("messages") or [])[:12]:
            if not isinstance(item, dict):
                continue
            body = str(item.get("text") or "").strip()
            if not body or secret_hold(body):
                continue
            messages_out.append({"from": str(item.get("from") or "")[:80], "text": body[:1000]})
        return {"op": "ingest", "messages": messages_out, "sent": False}
    return {"op": "none", "reason": "none", "sent": False}


def draft_row(session: dict, messages: list[dict]) -> dict | None:
    clean = [item for item in messages or [] if str(item.get("text") or "").strip()]
    if not clean:
        return None
    newest = clean[-1]
    label = str(session.get("label") or "account")
    return {
        "message_id": "acct-" + label + "-" + str(abs(hash(newest["text"])) % 1000000),
        "subject": label,
        "from": newest.get("from") or label,
        "ask": newest["text"][:500],
        "sources": [item["text"] for item in clean],
        "channel": "account",
        "sent": False,
    }


def tick(sessions: dict, pages: dict, complete) -> list[dict]:
    """pages maps an account label to visible text. Nothing in the result is sent."""
    results = []
    for name, session in (sessions or {}).items():
        page = str((pages or {}).get(name) or "")
        if not page.strip():
            results.append({"account": name, "op": "none", "reason": "no_page", "sent": False})
            continue
        outcome = interpret(session, page, complete)
        outcome["account"] = name
        if outcome.get("op") == "ingest":
            outcome["row"] = draft_row(session, outcome.get("messages") or [])
        results.append(outcome)
    return results
