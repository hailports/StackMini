#!/bin/sh
# Install one watch: sh install.sh <name> <check-script> [interval-seconds]
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
NAME=${1:-}
CHECK=${2:-}
INTERVAL=${3:-300}
PREFIX=${WATCH_LABEL_PREFIX:-com.claude-stack.watch}
if [ -z "$NAME" ] || [ -z "$CHECK" ]; then
  echo "usage: install.sh <name> <check-script> [interval]" >&2
  exit 2
fi
case "$NAME" in
  *[!A-Za-z0-9._-]*) echo "bad name" >&2; exit 2 ;;
esac
CHECK=$(CDPATH= cd -- "$(dirname "$CHECK")" && pwd)/$(basename "$CHECK")
[ -x "$CHECK" ] || [ -f "$CHECK" ] || { echo "check missing: $CHECK" >&2; exit 2; }
LABEL="$PREFIX.$NAME"
LAUNCH="$HOME/Library/LaunchAgents"
LOGDIR="$HOME/Library/Logs/local-first"
mkdir -p "$LAUNCH" "$LOGDIR"
PLIST="$LAUNCH/$LABEL.plist"
LOG="$LOGDIR/watch-$NAME.log"
sed \
  -e "s|LABEL_PLACEHOLDER|$LABEL|g" \
  -e "s|RUNNER_PLACEHOLDER|$ROOT/runner.py|g" \
  -e "s|NAME_PLACEHOLDER|$NAME|g" \
  -e "s|CHECK_PLACEHOLDER|$CHECK|g" \
  -e "s|INTERVAL_PLACEHOLDER|$INTERVAL|g" \
  -e "s|LOG_PLACEHOLDER|$LOG|g" \
  "$ROOT/template.plist" > "$PLIST"
chmod 644 "$PLIST"
UID_NUM=$(id -u)
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl kickstart -k "gui/$UID_NUM/$LABEL" 2>/dev/null || true
echo "installed $LABEL interval=${INTERVAL}s check=$CHECK"
