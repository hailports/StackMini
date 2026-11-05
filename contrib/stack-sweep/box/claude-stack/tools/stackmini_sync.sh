#!/bin/bash
# Port a prepared tree into the public StackMini checkout, scan it, and push.
# Usage:
#   stackmini_sync.sh --message "what changed"
#   stackmini_sync.sh --message-file msg.txt
#   stackmini_sync.sh --port-dir /path/to/files --message "what changed"
#   stackmini_sync.sh --repo /workspace/stackmini --dry-run
# The scan runs before the commit and again before the push. A hit aborts both.
# This script does not print the hailports token and does not force-push.

set -eu

REPO=/workspace/stackmini
PORT_DIR=
MESSAGE=
MESSAGE_FILE=
DRY=0
DENY=${HAILPORTS_DENYLIST:-/workspace/grokjobs/hposs.denylist.txt}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO=$2; shift 2 ;;
    --port-dir) PORT_DIR=$2; shift 2 ;;
    --message) MESSAGE=$2; shift 2 ;;
    --message-file) MESSAGE_FILE=$2; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --force|-f|--force-with-lease)
      echo "force push refused" >&2
      exit 2
      ;;
    -h|--help)
      echo "usage: stackmini_sync.sh [--repo DIR] [--port-dir DIR] [--message TEXT] [--message-file FILE] [--dry-run]"
      exit 0
      ;;
    *)
      echo "unknown argument" >&2
      exit 2
      ;;
  esac
done

if [ ! -d "$REPO/.git" ]; then
  echo "not a git checkout: $REPO" >&2
  exit 2
fi

cd "$REPO"
remote=$(git remote get-url origin)
case "$remote" in
  *hailports/StackMini|*"hailports/StackMini.git") ;;
  *) echo "refusing remote" >&2; exit 2 ;;
esac
case "$remote" in
  *[Vv]almont*|*[Dd]eoperator*) echo "refusing remote" >&2; exit 2 ;;
esac

if [ -n "$PORT_DIR" ]; then
  python3 - "$PORT_DIR" "$REPO" << 'PY'
import shutil, sys
from pathlib import Path
src = Path(sys.argv[1]).resolve()
dst = Path(sys.argv[2]).resolve()
if not src.is_dir():
    sys.stderr.write("port dir missing\n")
    raise SystemExit(2)
count = 0
for path in src.rglob("*"):
    if not path.is_file():
        continue
    rel = path.relative_to(src)
    if ".git" in rel.parts or ".." in rel.parts:
        sys.stderr.write("refusing path\n")
        raise SystemExit(2)
    target = (dst / rel).resolve()
    if target != dst and dst not in target.parents:
        sys.stderr.write("refusing path\n")
        raise SystemExit(2)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    count += 1
print(f"ported {count} files")
PY
fi

scan() {
  python3 - "$REPO" "$DENY" << 'PY'
import re, subprocess, sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
deny_path = Path(sys.argv[2])
skip = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "models", "build", "dist"}
text_ext = {
    ".py", ".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".sh", ".ps1",
    ".html", ".htm", ".xml", ".js", ".css", ".svg", ".cfg", ".ini", ".example",
}
identity = re.compile(
    r"operator|de\s*operator|operator|operator|ag\s*sense|operator|"
    r"operator|operator",
    re.I,
)
people = re.compile(r"\b(operator|operator|operator)\b", re.I)
email = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
phone = re.compile(r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
ip = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
token = re.compile(r"github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|gho_[A-Za-z0-9]+|sk-or-[A-Za-z0-9_\-]+|tskey-[A-Za-z0-9_\-]+|xox[baprs]-[A-Za-z0-9\-]+|AKIA[0-9A-Z]{16}")
_mac = "/" + "Users/"
userpath = re.compile(_mac + r"[A-Za-z0-9._\-]+|/home/(?!operator\b)[A-Za-z0-9._\-]+", re.I)
ticket = re.compile(r"\b(?:ado-\d{5,}|AB#\d{5,}|SF-\d{3,}|wi_\d{5,})\b")
org = re.compile(r"\b00D[a-zA-Z0-9]{12,}\b")
allow_email = {"redacted-email"}
allow_ip = {"127.0.0.1", "0.0.0.0"}
hits = []

def tailscale(text):
    parts = text.split(".")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return False
    if any(n > 255 for n in nums):
        return False
    return nums[0] == 100 and 64 <= nums[1] <= 127

for path in root.rglob("*"):
    if not path.is_file():
        continue
    if any(part in skip or part.endswith(".egg-info") for part in path.parts):
        continue
    if path.suffix.lower() not in text_ext and path.name not in {"Dockerfile", "Makefile"}:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    rel = str(path.relative_to(root))
    if identity.search(text):
        hits.append(rel + ": identity")
    if people.search(text):
        hits.append(rel + ": person")
    for match in email.findall(text):
        if match not in allow_email:
            hits.append(rel + ": email")
            break
    if phone.search(text):
        hits.append(rel + ": phone")
    for match in ip.findall(text):
        if match in allow_ip:
            continue
        parts = [int(p) for p in match.split(".")]
        if any(p > 255 for p in parts):
            continue
        if tailscale(match) or match not in allow_ip:
            # Any non-loopback address is a hit. The public tree uses names, not addresses.
            hits.append(rel + ": ip")
            break
    if token.search(text):
        hits.append(rel + ": token")
    if userpath.search(text):
        hits.append(rel + ": userpath")
    if ticket.search(text):
        hits.append(rel + ": ticket")
    if org.search(text):
        hits.append(rel + ": org")

if deny_path.is_file():
    terms = [ln.strip() for ln in deny_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip() and not ln.startswith("#")]
    diff = subprocess.check_output(["git", "-C", str(root), "diff", "HEAD"], text=True, errors="replace")
    untracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"], text=True)
    extra = []
    for rel in untracked.splitlines():
        path = root / rel
        if path.is_file():
            try:
                extra.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    blob = diff + "\n" + "\n".join(extra)
    low = blob.lower()
    for term in terms:
        if term.lower() in low:
            hits.append("diff: denylist")
            break

if hits:
    print("\n".join(hits))
    print(f"grep hits: {len(hits)}")
    raise SystemExit(1)
print("grep hits: 0")
PY
  python3 "$HOME/claude-stack/tools/deploy_guard_scan.py" "$REPO" "$HOME/claude-stack/data/runtime/stackmini-guard.json"
  python3 "$REPO/scripts/leak_scan.py"
}

echo "scan before commit"
scan

if [ "$DRY" -eq 1 ]; then
  echo "dry run, no commit and no push"
  git status --short
  exit 0
fi

cred=$(mktemp)
ask=$(mktemp)
cat > "$cred" << 'EOF'
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
action = sys.argv[1] if len(sys.argv) > 1 else ""
sys.stdin.read()
if action != "get":
    raise SystemExit(0)
blob = {}
path = Path("/home/operator/sand-data/box-secrets.json")
if path.exists():
    blob = json.loads(path.read_text())
card = blob.get("card") or {}
token = (
    os.environ.get("HAILPORTS_STACKMINI_GH_TOKEN")
    or card.get("HAILPORTS_STACKMINI_GH_TOKEN")
    or os.environ.get("HAILPORTS_GH_TOKEN")
    or card.get("HAILPORTS_GH_TOKEN")
    or blob.get("HAILPORTS_GH_TOKEN")
    or ""
)
if not token:
    sys.stderr.write("hailports token missing\n")
    raise SystemExit(1)
sys.stdout.write("username=hailports\npassword=" + token + "\n")
EOF
cat > "$ask" << 'EOF'
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
prompt = " ".join(sys.argv[1:]).lower()
if "username" in prompt:
    sys.stdout.write("hailports\n")
    raise SystemExit(0)
blob = {}
path = Path("/home/operator/sand-data/box-secrets.json")
if path.exists():
    blob = json.loads(path.read_text())
card = blob.get("card") or {}
token = (
    os.environ.get("HAILPORTS_STACKMINI_GH_TOKEN")
    or card.get("HAILPORTS_STACKMINI_GH_TOKEN")
    or os.environ.get("HAILPORTS_GH_TOKEN")
    or card.get("HAILPORTS_GH_TOKEN")
    or blob.get("HAILPORTS_GH_TOKEN")
    or ""
)
if not token:
    sys.stderr.write("hailports token missing\n")
    raise SystemExit(1)
sys.stdout.write(token + "\n")
EOF
chmod 700 "$cred" "$ask"
cleanup() { rm -f "$cred" "$ask"; }
trap cleanup EXIT

auth_git() {
  # Ignore the machine gitconfig so another saved login cannot push this repo.
  # timeout is a binary, so it wraps git itself (a shell function cannot be timed).
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
  GIT_TERMINAL_PROMPT=0 \
    timeout 90 git -c credential.helper= \
      -c "credential.helper=!python3 $cred" \
      -c credential.https://github.com.helper= \
      -c credential.https://gist.github.com.helper= \
      "$@"
}

python3 - << 'PY'
import json, os, sys, urllib.request
from pathlib import Path
blob = json.loads(Path("/home/operator/sand-data/box-secrets.json").read_text())
card = blob.get("card") or {}
token = (
    os.environ.get("HAILPORTS_STACKMINI_GH_TOKEN")
    or card.get("HAILPORTS_STACKMINI_GH_TOKEN")
    or os.environ.get("HAILPORTS_GH_TOKEN")
    or card.get("HAILPORTS_GH_TOKEN")
    or ""
)
req = urllib.request.Request(
    "https://api.github.com/user",
    headers={"Authorization": "Bearer " + token, "User-Agent": "stackmini-sync", "Accept": "application/vnd.github+json"},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    login = json.load(resp).get("login") or ""
if login != "hailports":
    sys.stderr.write("token is not the hailports account\n")
    raise SystemExit(2)
print("account hailports")
PY

auth_git fetch origin main
behind=$(git rev-list --count HEAD..origin/main)
if [ "$behind" != "0" ]; then
  echo "origin is ahead; refusing to rewrite history" >&2
  exit 2
fi

git add -A -- . ':!.env' ':!**/.env' ':!**/*secret*' ':!**/*.pem' ':!**/*.key'
if git diff --cached --name-only | grep -E -i '(^|/)\.env$|secret|id_rsa|\.pem$|\.key$' >/dev/null; then
  echo "refusing to commit a secret file" >&2
  exit 2
fi

if git diff --cached --quiet; then
  echo "nothing to commit"
else
  if [ -n "$MESSAGE_FILE" ]; then
    msg=$MESSAGE_FILE
  elif [ -n "$MESSAGE" ]; then
    msg=$(mktemp)
    printf '%s\n' "$MESSAGE" > "$msg"
  else
    echo "a --message is required when there are changes" >&2
    exit 2
  fi
  git -c user.name=HailPorts -c user.email=redacted-email commit -F "$msg"
  if [ -n "$MESSAGE" ]; then
    rm -f "$msg"
  fi
fi

bad_author=$(git log origin/main..HEAD --format='%an %ae' | grep -v '^HailPorts redacted-email$' || true)
if [ -n "$bad_author" ]; then
  echo "refusing commit author" >&2
  exit 2
fi

ahead=$(git rev-list --count origin/main..HEAD)
if [ "$ahead" = "0" ]; then
  echo "scan before push"
  scan
  echo "nothing to push"
  exit 0
fi

echo "scan before push"
scan

export GIT_ASKPASS="$ask"
export GIT_TERMINAL_PROMPT=0
auth_git push origin HEAD:main
sha=$(git rev-parse HEAD)
echo "pushed $sha"
echo "https://github.com/hailports/StackMini/commit/$sha"
