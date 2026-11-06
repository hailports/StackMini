#!/usr/bin/env bash
# Kills Grok CLI runs that are orphaned (parent gone, >5 min) or CPU-idle with
# no progress for 20 min. Does NOT kill active jobs solely for age — standing
# rule: no job time limits. An earlier MAX=2700 age kill reaped long overnight
# tuning jobs that were still making progress.
# Preserves stop/cancel: external kill/tmux kill still work; this loop only
# targets orphaned or truly stuck (idle) processes.
LOG=/home/box/cli-watchdog/reaper.log
while true; do
  for pid in $(pgrep -f '/home/box/.local/bin/grok( |$)' ); do
    [ "$pid" = "$$" ] && continue
    read -r ppid et < <(ps -o ppid=,etimes= -p "$pid" 2>/dev/null) || continue
    cpu=0; for t in $pid $(pgrep -P "$pid"); do c=$(awk '{print $14+$15}' /proc/$t/stat 2>/dev/null); cpu=$((cpu+${c:-0})); done; cpu="$cpu.$(stat -c %s /proc/$pid/fd/1 2>/dev/null)"
    pcomm=$(ps -o comm= -p "$ppid" 2>/dev/null)
    reason=""
    # age kill removed (was: [ "$et" -gt "${MAX:-2700}" ] && reason="age ...")
    { [ "$ppid" = 1 ] || [ -z "$pcomm" ]; } && [ "$et" -gt 300 ] && reason="orphaned ${et}s"
    f=/home/box/cli-watchdog/cpu.$pid; now=$(date +%s)
    if [ -f "$f" ]; then read -r lastcpu since < "$f"; if [ "$cpu" = "$lastcpu" ]; then [ $((now-since)) -gt 1200 ] && reason="idle 20m"; else echo "$cpu $now" > "$f"; fi; else echo "$cpu $now" > "$f"; fi
    if [ -n "$reason" ]; then echo "$(date '+%F %T %Z') kill $pid ($reason): $(ps -o args= -p $pid | cut -c1-120)" >> "$LOG"; pkill -TERM -P "$pid"; kill -TERM "$pid"; sleep 5; kill -KILL "$pid" 2>/dev/null; rm -f "$f"; fi
  done
  find /home/box/cli-watchdog -maxdepth 1 -name 'cpu.*' -mmin +120 -delete
  sleep 60
done
