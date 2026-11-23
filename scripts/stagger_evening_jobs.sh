#!/bin/bash
# Stagger the Mini's evening (7-11 PM CT) model-hitting batch jobs off shared minutes. Disables nothing.
# Run by hand: agent shells are not allowed to write LaunchAgents.
#   stagger_evening_jobs.sh            # dry run: prints before/after
#   stagger_evening_jobs.sh --apply    # backs up plists, edits minutes with PlistBuddy, reloads each agent
#   stagger_evening_jobs.sh --revert   # restores the backups taken by --apply
# Never touches never-idle, the memory governor, the throughput agent, browser bridges, or Splash.
set -u
LA="$HOME/Library/LaunchAgents"; BK="$HOME/claude-stack/data/plist-backups/stagger-20261123"; U=$(id -u)
PB=/usr/libexec/PlistBuddy
# Changes come from a local file (one per line, # comments allowed):
#   label|StartCalendarInterval index|key|old value|new value|why
#   e.g. com.example.ingest-a|0|Minute|8|26|collided with ingest-b at :08
CF=${STAGGER_CHANGES:-$HOME/claude-stack/data/stagger_changes.txt}
[ -f "$CF" ] || { echo "no changes file $CF"; exit 2; }
CHANGES=(); while IFS= read -r line; do case "$line" in ""|"#"*) ;; *) CHANGES+=("$line");; esac; done < "$CF"
mode=${1:-dry}
show(){ plutil -extract StartCalendarInterval json -o - "$LA/$1.plist" 2>/dev/null; }
for c in "${CHANGES[@]}"; do IFS='|' read -r L I K O N W <<<"$c"; echo "$L [$I].$K $O -> $N  ($W)"; done
case "$mode" in
 dry|--dry-run) for L in $(printf '%s\n' "${CHANGES[@]}" | cut -d'|' -f1 | sort -u); do echo "$L now: $(show $L)"; done; echo "dry run only; re-run with --apply";;
 --apply)
  mkdir -p "$BK"
  for L in $(printf '%s\n' "${CHANGES[@]}" | cut -d'|' -f1 | sort -u); do [ -f "$BK/$L.plist" ] || cp -p "$LA/$L.plist" "$BK/$L.plist"; done
  for c in "${CHANGES[@]}"; do IFS='|' read -r L I K O N W <<<"$c"
    cur=$($PB -c "Print :StartCalendarInterval:$I:$K" "$LA/$L.plist" 2>/dev/null)
    [ "$cur" = "$O" ] || { echo "SKIP $L[$I].$K is '$cur' (expected $O)"; continue; }
    $PB -c "Set :StartCalendarInterval:$I:$K $N" "$LA/$L.plist" && echo "set $L[$I].$K=$N"
  done
  for L in $(printf '%s\n' "${CHANGES[@]}" | cut -d'|' -f1 | sort -u); do
    plutil -lint "$LA/$L.plist" >/dev/null || { echo "LINT FAIL $L, restoring"; cp -p "$BK/$L.plist" "$LA/$L.plist"; }
    launchctl bootout "gui/$U/$L" 2>/dev/null; launchctl bootstrap "gui/$U" "$LA/$L.plist" && echo "reloaded $L: $(show $L)"
  done;;
 --revert)
  for f in "$BK"/*.plist; do L=$(basename "$f" .plist); cp -p "$f" "$LA/$L.plist"; launchctl bootout "gui/$U/$L" 2>/dev/null; launchctl bootstrap "gui/$U" "$LA/$L.plist" && echo "reverted $L"; done;;
esac
