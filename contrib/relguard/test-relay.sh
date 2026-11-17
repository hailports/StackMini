#!/usr/bin/env bash
# Fake bare remote relay test (no network org).
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
source "$ROOT/lib.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/relguard-relay-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
export RELGUARD_DATA="$TMP/data"
export RELGUARD_RELAY_LEDGER="$TMP/data/relay-ledger.jsonl"
export RELGUARD_APPROVALS="$TMP/data/approvals"
mkdir -p "$RELGUARD_APPROVALS"
touch "$RELGUARD_RELAY_LEDGER"

BARE="$TMP/bare.git"; git init -q --bare "$BARE"
REPO="$TMP/repo"; git init -q -b main "$REPO"
git -C "$REPO" config user.name "HailPorts"
git -C "$REPO" config user.email "noreply@hailports.com"
echo hi > "$REPO/README"; git -C "$REPO" add README
git -C "$REPO" -c core.hooksPath=/dev/null commit -q -m init
git -C "$REPO" remote add origin "$BARE"
git -C "$REPO" -c core.hooksPath=/dev/null push -q origin main

CLONE="$TMP/clone"; git clone -q "$BARE" "$CLONE"
git -C "$CLONE" config user.name "HailPorts"
git -C "$CLONE" config user.email "noreply@hailports.com"
mkdir -p "$CLONE/src"
echo 'x' > "$CLONE/src/a.txt"
git -C "$CLONE" -c core.hooksPath=/dev/null add src/a.txt
git -C "$CLONE" -c core.hooksPath=/dev/null commit -q -m wip
git -C "$CLONE" -c core.hooksPath=/dev/null format-patch -1 --stdout > "$TMP/work.patch"
git -C "$CLONE" -c core.hooksPath=/dev/null checkout -q main
git -C "$CLONE" -c core.hooksPath=/dev/null reset -q --hard origin/main

if "$ROOT/relguard-relay.sh" "$TMP/work.patch" --story US1 --team ABC --desc x --type minor --paths other --repo "$CLONE" --base main; then
  echo FAIL out-of-scope; exit 1
fi
BRANCH="$("$ROOT/relguard-relay.sh" "$TMP/work.patch" --story US1234 --team ABC --desc widget --type minor --paths src --repo "$CLONE" --base main)"
[[ "$BRANCH" == feature/ABC-US1234-widget ]] || { echo FAIL branch "$BRANCH"; exit 1; }
git --git-dir="$BARE" rev-parse "refs/heads/$BRANCH" >/dev/null
grep -q feature/ABC-US1234-widget "$RELGUARD_RELAY_LEDGER"
echo "PASS relay tests"
