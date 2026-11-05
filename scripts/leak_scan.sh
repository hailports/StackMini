#!/bin/sh
# Fail if the tree or git history contains a denylist term, a secret pattern,
# or a gitleaks/trufflehog finding.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
DENY=${HAILPORTS_DENYLIST:-}
HITS=0

echo "root $ROOT"

if [ -n "$DENY" ] && [ -f "$DENY" ]; then
  echo "denylist $DENY"
  if grep -R -I -n -i -F -f "$DENY" "$ROOT" \
      --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=.pytest_cache \
      --exclude-dir=.ruff_cache --exclude-dir=.local --exclude-dir=models --exclude-dir=build \
      --exclude-dir=dist --exclude-dir=.venv --exclude-dir='*.egg-info'; then
    echo "denylist hit in work tree"
    HITS=1
  else
    echo "denylist work tree: 0"
  fi
  if [ -d "$ROOT/.git" ]; then
    REVS=$(git -C "$ROOT" rev-list --all)
    if [ -n "$REVS" ]; then
      # git grep exits 0 when it finds a match.
      if git -C "$ROOT" grep -I -i -n -F -f "$DENY" $REVS; then
        echo "denylist hit in git history"
        HITS=1
      else
        echo "denylist git history: 0"
      fi
    fi
    if git -C "$ROOT" log --all --format=fuller | grep -i -F -f "$DENY"; then
      echo "denylist hit in commit metadata"
      HITS=1
    else
      echo "denylist commit metadata: 0"
    fi
  fi
else
  echo "denylist missing, skipped term scan"
fi

python3 "$ROOT/scripts/leak_scan.py" || HITS=1

gitleaks_bin=""
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks_bin=$(command -v gitleaks)
else
  tmp=$(mktemp -d)
  url="https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz"
  if curl -fsSL -m 60 -o "$tmp/g.tgz" "$url"; then
    tar -C "$tmp" -xzf "$tmp/g.tgz"
    gitleaks_bin="$tmp/gitleaks"
  else
    echo "gitleaks download failed"
  fi
fi
if [ -n "$gitleaks_bin" ]; then
  if "$gitleaks_bin" detect --source "$ROOT" --no-banner --redact; then
    echo "gitleaks: 0"
  else
    echo "gitleaks hit"
    HITS=1
  fi
fi

th_bin=""
if command -v trufflehog >/dev/null 2>&1; then
  th_bin=$(command -v trufflehog)
else
  tmp=$(mktemp -d)
  url="https://github.com/trufflesecurity/trufflehog/releases/download/v3.90.8/trufflehog_3.90.8_linux_amd64.tar.gz"
  if curl -fsSL -m 60 -o "$tmp/t.tgz" "$url"; then
    tar -C "$tmp" -xzf "$tmp/t.tgz"
    th_bin=$(find "$tmp" -type f -name trufflehog | head -1)
  else
    echo "trufflehog download failed"
  fi
fi
if [ -n "$th_bin" ]; then
  if "$th_bin" filesystem "$ROOT" --no-update --fail; then
    echo "trufflehog: 0"
  else
    echo "trufflehog hit"
    HITS=1
  fi
fi

if [ "$HITS" -ne 0 ]; then
  echo "LEAK SCAN FAILED"
  exit 1
fi
echo "LEAK SCAN CLEAN"
exit 0
