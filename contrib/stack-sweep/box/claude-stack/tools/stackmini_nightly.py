#!/usr/bin/env python3
"""Port stack code changed in the last day into the public StackMini checkout.

Reads the peer host from data/runtime/stackmini_nightly.local. Reads scrub
terms from data/runtime/stackmini_needles.txt and the denylist file. Puts
HAILPORTS_STACKMINI_GH_TOKEN in the process environment for the sync helper.
Does not print tokens, and does not put them in git config or URLs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO = Path("/workspace/stackmini")
SYNC = Path("/home/operator/claude-stack/tools/stackmini_sync.sh")
LOCAL = Path("/home/operator/claude-stack/data/runtime/stackmini_nightly.local")
NEEDLES = Path("/home/operator/claude-stack/data/runtime/stackmini_needles.txt")
DENY = Path(os.environ.get("HAILPORTS_DENYLIST", "/workspace/grokjobs/hposs.denylist.txt"))
GUARD = Path("/home/operator/claude-stack/tools/deploy_guard_scan.py")
LEAK = Path("/workspace/stackmini/scripts/leak_scan.py")
SOCKS = Path("/home/operator/claude-stack/bin/socks5-connect.py")
LOG = Path("/home/operator/claude-stack/data/logs/stackmini_nightly.log")
LOCK = Path("/home/operator/claude-stack/data/runtime/stackmini_nightly.lock")
SECRETS = Path("/home/operator/sand-data/box-secrets.json")

MAX_BYTES = 160_000
JSON_MAX = 32_768
CAP = 120

COLLECTOR = r'''
import os, sys, tarfile, time

SKIP_DIRS = {
    "data", "logs", "secrets", "secret", "sand-data", "node_modules", "venv",
    ".venv", "site-packages", "__pycache__", ".git", "models", "backups",
    "cache", "mail", "Memory", "dist", "build", ".pytest_cache", "socials",
    "drafts",
}
CODE_EXT = {".py", ".sh", ".plist", ".js", ".mjs"}
JSON_PREFIXES = (
    "claude-stack/tools/",
    "claude-stack/cli/",
    "llm-appliance/cli/",
    "llm-appliance/tools/",
)
NAME_BITS = ("secret", "token", "credential", "password", "denylist", "needle", ".env")

def consider(rel, name, ext, size):
    low = name.lower()
    if low.startswith(".") or ".bak" in low or low.startswith("socials"):
        return False
    if any(bit in low for bit in NAME_BITS):
        return False
    if any(part in SKIP_DIRS or part.startswith(".") for part in rel.split("/")):
        return False
    if ext in CODE_EXT and 0 < size <= 160000:
        return True
    if ext == ".json" and size <= 32768 and any(rel.startswith(p) for p in JSON_PREFIXES):
        return True
    return False

def walk(home, hours):
    cutoff = time.time() - int(hours) * 3600
    roots = [
        os.path.join(home, "claude-stack"),
        os.path.join(home, "llm-appliance"),
        os.path.join(home, "Library", "LaunchAgents"),
        os.path.join(home, "tools"),
    ]
    rows = []
    seen = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, home).replace(os.sep, "/")
                ext = os.path.splitext(name)[1].lower()
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_mtime < cutoff or not consider(rel, name, ext, st.st_size):
                    continue
                if rel not in seen:
                    seen.add(rel)
                    rows.append((rel, path))
    extra = os.path.join(home, "claude-stack", "data", "runtime", "mem_budget.json")
    if os.path.isfile(extra):
        st = os.stat(extra)
        rel = "claude-stack/data/runtime/mem_budget.json"
        if st.st_mtime >= cutoff and 0 < st.st_size <= 32768 and rel not in seen:
            rows.append((rel, extra))
    return rows

if __name__ == "__main__":
    home = os.path.expanduser("~")
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    rows = walk(home, hours)
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tar:
        for rel, path in rows:
            tar.add(path, arcname=rel, recursive=False)
'''

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
TOKEN = re.compile(r"github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|gho_[A-Za-z0-9]+|sk-or-[A-Za-z0-9_\-]+|tskey-[A-Za-z0-9_\-]+|xox[baprs]-[A-Za-z0-9\-]+|AKIA[0-9A-Z]{16}")
USERPATH = re.compile(r"/Users/[A-Za-z0-9._\-]+|/home/[A-Za-z0-9._\-]+")
TICKET = re.compile(r"\b(?:ado-\d{5,}|AB#\d{5,}|SF-\d{3,}|wi_\d{5,})\b")
ORG = re.compile(r"\b00D[a-zA-Z0-9]{12,}\b")
HOST = re.compile(r"\b[A-Za-z0-9-]+\.local\b")
ALLOW_EMAIL = {"redacted-email"}
ALLOW_IP = {"127.0.0.1", "0.0.0.0"}


def load_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        item = line.strip()
        if item and not item.startswith("#"):
            out.append(item)
    return out


def load_local() -> tuple[str, str]:
    user = host = ""
    for line in load_lines(LOCAL):
        if line.startswith("user="):
            user = line.split("=", 1)[1].strip()
        elif line.startswith("host="):
            host = line.split("=", 1)[1].strip()
    if not user or not host:
        raise SystemExit("peer config missing")
    return user, host


def replacement(terms: list[str]) -> str:
    for cand in ("operator", "item", "xxxx"):
        low = cand.lower()
        if any(term and term.lower() in low for term in terms):
            continue
        return cand
    return "xxxx"


def build_scrubber(terms: list[str], repl: str):
    ordered = sorted({t for t in terms if len(t) >= 3}, key=len, reverse=True)
    if not ordered:
        return None
    return re.compile("|".join(re.escape(t) for t in ordered), re.I)


def anonymize(text: str, scrubber, repl: str) -> str:
    text = EMAIL.sub("redacted-email", text)
    text = TOKEN.sub("redacted-token", text)
    text = UUID.sub("redacted-uuid", text)
    text = HEX.sub("redacted-hex", text)

    def ip_sub(match: re.Match) -> str:
        addr = match.group(0)
        parts = addr.split(".")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return addr
        if any(n > 255 for n in nums) or addr in ALLOW_IP:
            return addr
        return "127.0.0.1"

    text = IP.sub(ip_sub, text)
    text = PHONE.sub("redacted-phone", text)
    text = USERPATH.sub(lambda m: "/Users/operator" if m.group(0).startswith("/Users/") else "/home/operator", text)
    text = TICKET.sub("redacted-ticket", text)
    text = ORG.sub("redacted-org", text)
    text = HOST.sub("localhost", text)
    if scrubber is not None:
        for _ in range(6):
            nxt = scrubber.sub(repl, text)
            if nxt == text:
                break
            text = nxt
    return text


def leak_kinds(text: str) -> list[str]:
    sys.path.insert(0, str(LEAK.parent))
    import leak_scan
    hits = leak_scan.scan_text("f", text)
    kinds = []
    for hit in hits:
        kind = hit.split()[-1] if "b64" not in hit else "b64"
        if "b64" in hit:
            kind = "b64"
        elif ": email" in hit:
            kind = "email"
        elif ": ip" in hit:
            kind = "ip"
        elif ": phone" in hit:
            kind = "phone"
        elif ": uuid" in hit:
            kind = "uuid"
        elif ": hex" in hit:
            kind = "hex"
        else:
            kind = "leak"
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def problems(text: str, needles: list[str], deny: list[str]) -> list[str]:
    found = []
    for addr in EMAIL.findall(text):
        if addr not in ALLOW_EMAIL:
            found.append("email")
            break
    if PHONE.search(text):
        found.append("phone")
    for addr in IP.findall(text):
        parts = addr.split(".")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            continue
        if any(n > 255 for n in nums) or addr in ALLOW_IP:
            continue
        found.append("ip")
        break
    if TOKEN.search(text):
        found.append("token")
    if UUID.search(text):
        found.append("uuid")
    if HEX.search(text):
        found.append("hex")
    if re.search(r"/Users/(?!operator\b)[A-Za-z0-9._-]", text):
        found.append("userpath")
    if re.search(r"/home/(?!operator\b)[A-Za-z0-9._-]", text):
        found.append("userpath")
    if TICKET.search(text) or ORG.search(text):
        found.append("ticket")
    low = text.lower()
    for needle in needles:
        if needle.lower() in low:
            found.append("needle")
            break
    for term in deny:
        if len(term) >= 3 and term.lower() in low:
            found.append("denylist")
            break
    for kind in leak_kinds(text):
        if kind not in found:
            found.append(kind)
    return found


def scrub_b64(text: str) -> str:
    sys.path.insert(0, str(LEAK.parent))
    import leak_scan
    for match in leak_scan.B64.findall(text):
        if match.lower() in leak_scan.ALLOW_HEX or match.isalpha() or match.startswith("http"):
            continue
        text = text.replace(match, "redacted-b64")
    return text


def ssh_base(user: str, host: str) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=20",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"ProxyCommand=python3 {SOCKS} %h %p",
        f"{user}@{host}",
    ]


def scp_base() -> list[str]:
    return [
        "scp",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=20",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"ProxyCommand=python3 {SOCKS} %h %p",
    ]


def fetch_peer(user: str, host: str, hours: int) -> tuple[list[tuple[str, bytes]], str]:
    scratch = Path(tempfile.mkdtemp(prefix="stackmini-collect-"))
    local = scratch / "collect.py"
    local.write_text(COLLECTOR, encoding="utf-8")
    remote = f"/tmp/stackmini-collect-{os.getpid()}.py"
    try:
        up = subprocess.run(
            scp_base() + [str(local), f"{user}@{host}:{remote}"],
            capture_output=True, timeout=90,
        )
        if up.returncode != 0:
            return [], "peer-copy-failed"
        got = subprocess.run(
            ssh_base(user, host) + ["python3", remote, str(int(hours))],
            capture_output=True, timeout=180,
        )
        subprocess.run(ssh_base(user, host) + ["rm", "-f", remote], capture_output=True, timeout=30)
        if got.returncode != 0 or not got.stdout:
            return [], "peer-list-failed"
        files = []
        with tarfile.open(fileobj=__import__("io").BytesIO(got.stdout), mode="r:gz") as tar:
            for mem in tar.getmembers():
                if not mem.isfile() or mem.size > MAX_BYTES:
                    continue
                if ".." in mem.name.split("/") or mem.name.startswith("/"):
                    continue
                handle = tar.extractfile(mem)
                if handle is None:
                    continue
                files.append(("mini/" + mem.name, handle.read()))
        return files, ""
    except (subprocess.TimeoutExpired, tarfile.TarError, OSError):
        return [], "peer-list-failed"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def fetch_box(hours: int) -> list[tuple[str, bytes]]:
    ns: dict = {"__name__": "collector"}
    exec(COLLECTOR, ns)
    rows = ns["walk"]("/home/operator", hours)
    files = []
    for rel, path in rows:
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        if len(data) > MAX_BYTES:
            continue
        files.append(("box/" + rel, data))
    return files


def repo_hashes(repo: Path) -> set[str]:
    skip = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "models", "build", "dist"}
    found = set()
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip or part.endswith(".egg-info") for part in path.parts):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        found.add(hashlib.sha256(data).hexdigest())
    return found


def compiles(text: str, ext: str) -> bool:
    if ext == ".py":
        fd, name = tempfile.mkstemp(suffix=".py")
        os.close(fd)
        cfile = name + "c"
        try:
            Path(name).write_text(text, encoding="utf-8")
            py_compile.compile(name, cfile=cfile, doraise=True)
            return True
        except py_compile.PyCompileError:
            return False
        finally:
            for item in (name, cfile):
                try:
                    os.unlink(item)
                except OSError:
                    pass
    if ext == ".json":
        try:
            json.loads(text)
            return True
        except json.JSONDecodeError:
            return False
    return True


def scrub_rel(rel: str, scrubber, repl: str) -> str:
    parts = []
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            return ""
        cleaned = anonymize(part, scrubber, repl).replace("/", "-").replace("\\", "-").strip()
        if not cleaned or cleaned in (".", ".."):
            return ""
        parts.append(cleaned)
    return "/".join(parts)


def stage(files, scrubber, repl, needles, deny, hashes) -> tuple[Path, list[str], dict[str, int]]:
    port = Path(tempfile.mkdtemp(prefix="stackmini-port-"))
    reasons: dict[str, int] = {}
    staged: list[str] = []

    def bump(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for rel, data in files:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            bump("binary")
            continue
        cleaned = anonymize(text, scrubber, repl)
        cleaned = scrub_b64(cleaned)
        hits = problems(cleaned, needles, deny)
        if hits:
            bump("scan:" + ",".join(hits))
            continue
        ext = Path(rel).suffix.lower()
        if not compiles(cleaned, ext):
            bump("compile")
            continue
        dest_rel = scrub_rel(rel, scrubber, repl)
        if not dest_rel:
            bump("path")
            continue
        blob = cleaned.encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()
        if digest in hashes:
            bump("identical")
            continue
        target = port / "contrib" / "stack-sweep" / dest_rel
        if target.exists():
            bump("duplicate")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        hashes.add(digest)
        staged.append(str(target.relative_to(port)))
        if len(staged) > CAP:
            bump("cap")
            break
    return port, staged, reasons


def untracked(repo: Path) -> set[str]:
    out = subprocess.check_output(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"],
        text=True,
    )
    return {line for line in out.splitlines() if line}


def ensure_token(secrets_out: list[str]) -> None:
    if os.environ.get("HAILPORTS_STACKMINI_GH_TOKEN"):
        return
    if not SECRETS.is_file():
        raise SystemExit("hailports token missing")
    blob = json.loads(SECRETS.read_text(encoding="utf-8"))
    card = blob.get("card") or {}
    token = (
        card.get("HAILPORTS_STACKMINI_GH_TOKEN")
        or card.get("HAILPORTS_GH_TOKEN")
        or blob.get("HAILPORTS_GH_TOKEN")
        or ""
    )
    if not token:
        raise SystemExit("hailports token missing")
    os.environ["HAILPORTS_STACKMINI_GH_TOKEN"] = token
    secrets_out.append(token)


def scrub_text(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    def ip_sub(match: re.Match) -> str:
        addr = match.group(0)
        try:
            nums = [int(p) for p in addr.split(".")]
        except ValueError:
            return addr
        if any(n > 255 for n in nums) or addr in ALLOW_IP:
            return addr
        return "127.0.0.1"
    return IP.sub(ip_sub, text)


def guard_port(port: Path) -> bool:
    """Drop staged files the deploy guard rejects, then require a clean tree."""
    for _ in range(10):
        manifest = port / ".guard.json"
        run = subprocess.run(
            [sys.executable, str(GUARD), str(port), str(manifest)],
            capture_output=True, text=True,
        )
        manifest.unlink(missing_ok=True)
        if run.returncode == 0:
            return True
        dropped = False
        for line in (run.stdout + "\n" + run.stderr).splitlines():
            if "ABORT:" not in line:
                continue
            rel = line.split("ABORT:", 1)[1].strip().split()[0]
            target = port / rel
            if target.is_file():
                target.unlink()
                dropped = True
        if not dropped:
            return False
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    if args.hours < 1 or args.hours > 168:
        raise SystemExit("bad hours")

    LOG.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = LOCK.open("a")
    try:
        import fcntl
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another sweep is running")
        return 0

    user, host = load_local()
    secrets = [user, host]
    needles = load_lines(NEEDLES)
    deny = load_lines(DENY)
    repl = replacement(needles + deny)
    scrubber = build_scrubber(needles + deny, repl)

    peer, peer_err = fetch_peer(user, host, args.hours)
    box = fetch_box(args.hours)
    files = peer + box
    if len(files) > CAP:
        files = files[:CAP]
        print(f"capped at {CAP}")
    port, staged, reasons = stage(files, scrubber, repl, needles, deny, repo_hashes(REPO))
    lines = [
        f"considered {len(files)}",
        f"peer {len(peer)}" + (f" {peer_err}" if peer_err else ""),
        f"box {len(box)}",
        f"staged {len(staged)}",
        "reasons " + ",".join(f"{k}={v}" for k, v in sorted(reasons.items())),
        *staged,
    ]
    text = "\n".join(lines)
    print(text)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text.replace("\n", " | ") + "\n")

    if args.dry_run:
        print("dry-run")
        print(f"port-dir {port}")
        return 0 if not peer_err else 1

    if not staged:
        shutil.rmtree(port, ignore_errors=True)
        print("nothing to port")
        return 0 if not peer_err else 1

    if not guard_port(port):
        print("guard failed on the staged tree")
        shutil.rmtree(port, ignore_errors=True)
        return 1

    ensure_token(secrets)
    message = Path(tempfile.mkdtemp(prefix="stackmini-msg-")) / "msg.txt"
    message.write_text(
        "Sweep stack files changed in the last day.\n\n"
        "Memory reclaim now aims at the mail-replay swap line and only recycles stale allowlisted jobs.\n",
        encoding="utf-8",
    )
    before = untracked(REPO)
    run = subprocess.run(
        [str(SYNC), "--repo", str(REPO), "--port-dir", str(port), "--message-file", str(message)],
        capture_output=True, text=True,
    )
    shown = scrub_text((run.stdout or "") + (run.stderr or ""), secrets)
    print(shown)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(shown + "\n")
    if run.returncode != 0:
        after = untracked(REPO)
        extra = after - before
        if extra:
            subprocess.run(["git", "-C", str(REPO), "reset", "--hard", "HEAD"], check=False)
            subprocess.run(["git", "-C", str(REPO), "clean", "-fd", "--", *sorted(extra)], check=False)
        shutil.rmtree(port, ignore_errors=True)
        return run.returncode
    shutil.rmtree(port, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
