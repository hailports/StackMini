#!/bin/bash
# Short A/B downtime window: hold the live model down, run candidate servers one at a time on a
# side port, then always restore the live model (trap on EXIT) and log health + argv.
#
# The live server is held down with a pause flag that its launchd wrapper honors (the wrapper
# waits while the flag exists), so the plist and serve script are never edited.
#
# Required env:
#   LIVE_PATTERN   pgrep -f pattern for the live server process
#   LIVE_HEALTH    live health URL (expects "ok" in the body)
#   PAUSE_FLAG     flag file the live wrapper honors
#   CANDIDATES     space-separated "name=command" entries; command must start a server on SIDE_PORT
#   RUN_SUITE      command run with $1=chat URL $2=model name $3=output json
# Optional: SIDE_PORT (18090), OUT (./ab), MIN_LEVEL (4), TZ_LABEL (local zone)
set -u
SIDE_PORT=${SIDE_PORT:-18090}; OUT=${OUT:-./ab}; MIN_LEVEL=${MIN_LEVEL:-4}
mkdir -p "$OUT"; LOG=$OUT/window.log
log(){ echo "$(date '+%Y-%m-%d %H:%M:%S %Z') $*" | tee -a "$LOG"; }
level(){ sysctl -n kern.memorystatus_level; }
SPID=""
restore(){
  [ -n "$SPID" ] && { kill "$SPID" 2>/dev/null; sleep 3; kill -9 "$SPID" 2>/dev/null; }
  rm -f "$PAUSE_FLAG"; log "pause flag removed; waiting for live"
  for _ in $(seq 1 120); do
    curl -s -m2 "$LIVE_HEALTH" | grep -q ok && { log "RESTORED pid=$(pgrep -f "$LIVE_PATTERN" | head -1)"; break; }
    sleep 5
  done
  log "argv: $(ps -o command= -p "$(pgrep -f "$LIVE_PATTERN" | head -1)" 2>/dev/null | cut -c1-400)"
  log "WINDOW_END"
}
trap restore EXIT
log "WINDOW_START live_pid=$(pgrep -f "$LIVE_PATTERN" | head -1) level=$(level)"
touch "$PAUSE_FLAG"
LP=$(pgrep -f "$LIVE_PATTERN" | head -1); [ -n "$LP" ] && kill "$LP"
for _ in $(seq 1 30); do pgrep -f "$LIVE_PATTERN" >/dev/null || break; sleep 1; done
log "live down level=$(level)"
for C in $CANDIDATES; do
  NAME=${C%%=*}; CMD=${C#*=}
  T0=$(date +%s)
  bash -c "$CMD" > "$OUT/$NAME.server.log" 2>&1 &
  SPID=$!; up=0
  for _ in $(seq 1 240); do
    curl -s -m2 "127.0.0.1:$SIDE_PORT/v1/models" | grep -q . && { up=1; break; }
    kill -0 "$SPID" 2>/dev/null || break
    [ "$(level)" -lt "$MIN_LEVEL" ] && { log "memory red during load"; break; }
    sleep 5
  done
  log "$NAME up=$up load_s=$(( $(date +%s) - T0 )) level=$(level)"
  [ $up = 1 ] && $RUN_SUITE "http://127.0.0.1:$SIDE_PORT/v1/chat/completions" "$NAME" "$OUT/$NAME.json" >> "$OUT/$NAME.run.log" 2>&1
  log "$NAME done level=$(level)"
  kill "$SPID" 2>/dev/null; for _ in $(seq 1 20); do kill -0 "$SPID" 2>/dev/null || break; sleep 1; done
  kill -9 "$SPID" 2>/dev/null; SPID=""
done
