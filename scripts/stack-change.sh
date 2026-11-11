#!/bin/sh
# Commit a stack tweak and push when the remote allows it.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$ROOT"
MSG=""
PATHS=""
while [ $# -gt 0 ]; do
  case "$1" in
    -m) MSG=$2; shift 2 ;;
    --) shift; PATHS=$*; break ;;
    *) echo "usage: scripts/stack-change.sh -m \"message\" [-- paths]" >&2; exit 2 ;;
  esac
done
[ -n "$MSG" ] || { echo "need -m" >&2; exit 2; }
TOP=$(git rev-parse --show-toplevel)
HOME_DIR=${HOME:-}
if [ "$TOP" = "$HOME_DIR" ]; then
  echo "refuse: git root is the home directory" >&2
  exit 3
fi
if git status --short | grep -E -i 'password|api[_-]?key|secret|BEGIN .*PRIVATE KEY|seed phrase' >/dev/null; then
  echo "refuse: secret-looking diff" >&2
  exit 4
fi
if [ -n "$PATHS" ]; then
  git add -- $PATHS
else
  git add -A -- . ':(exclude).env' ':(exclude).env.*'
fi
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
git commit -m "$MSG"
PUSH=$(git remote get-url --push origin 2>/dev/null || true)
case "$PUSH" in
  *DISABLED*|*no-push*|"") echo "local commit only; push remote is not usable" ;;
  *) git push origin HEAD ;;
esac
