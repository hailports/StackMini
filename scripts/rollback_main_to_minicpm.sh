#!/bin/bash
# Point the live chat agent back at MiniCPM on 127.0.0.1:18080, context 32768.
# One model only. Does not delete Gemma weights or the Gemma serve script.
# Usage: rollback_main_to_minicpm.sh [--check]
# --check does not unload anything.
set -u
U=$(id -u)
LABEL=com.llm-appliance.mlx-qwen
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SERVE="$HOME/claude-stack/bin/minicpm_serve.sh"
DOOR_LABEL=com.llm-appliance.gateway-8787
DOOR_PLIST="$HOME/Library/LaunchAgents/${DOOR_LABEL}.plist"
BAK="$HOME/claude-stack/data/minicpm_ab/rollback/gemma-switch-20261115"
LOG="$HOME/llm-appliance/logs/gemma-switch-20261115.log"

log() { echo "$(TZ=America/Chicago date '+%Y-%m-%d %H:%M:%S %Z') $*" | tee -a "$LOG"; }

if [ "${1:-}" = "--check" ]; then
  [ -x "$SERVE" ] || { echo "CHECK_FAIL serve missing"; exit 1; }
  grep -q 'MiniCPM-V-4.7-35B-A3B,Qwen3.8-27B-oQ3e-mtp' "$SERVE" || { echo "CHECK_FAIL alias"; exit 1; }
  grep -q 'minicpm_ctx_want' "$SERVE" || { echo "CHECK_FAIL ctx"; exit 1; }
  grep -q 'minicpm_serve_extra' "$SERVE" || { echo "CHECK_FAIL extra"; exit 1; }
  [ -s "$HOME/models/minicpm/m.gguf" ] || { echo "CHECK_FAIL weights"; exit 1; }
  BIN=$(tr -d '[:space:]' < "$HOME/claude-stack/data/flags/minicpm_bin")
  [ -x "$BIN" ] || { echo "CHECK_FAIL bin"; exit 1; }
  CTX=$(tr -d '[:space:]' < "$HOME/claude-stack/data/flags/minicpm_ctx_want")
  [ "$CTX" = "32768" ] || { echo "CHECK_FAIL ctx_want=$CTX"; exit 1; }
  [ -s "$HOME/claude-stack/data/flags/minicpm_serve_extra" ] || { echo "CHECK_FAIL extra flag"; exit 1; }
  echo "CHECK_OK"
  exit 0
fi

log "ROLLBACK_START"
echo minicpm > "$HOME/claude-stack/data/flags/gd_model_variant"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:1 $SERVE" "$PLIST"
if [ -f "$BAK/gateway-8787.plist" ]; then cp "$BAK/gateway-8787.plist" "$DOOR_PLIST"; fi
if [ -f "$BAK/gateway.json" ]; then cp "$BAK/gateway.json" "$HOME/llm-appliance/config/gateway.json"; fi
if [ -f "$BAK/server.py" ]; then cp "$BAK/server.py" "$HOME/llm-appliance/gateway/server.py"; fi
if [ -f "$BAK/agent_identity.py" ]; then cp "$BAK/agent_identity.py" "$HOME/llm-appliance/cli/agent_identity.py"; fi
if [ -f "$BAK/stack_aware.py" ]; then cp "$BAK/stack_aware.py" "$HOME/llm-appliance/cli/stack_aware.py"; fi

launchctl disable "gui/$U/$LABEL"
launchctl bootout "gui/$U/$LABEL" 2>/dev/null || true
i=0
while [ "$i" -lt 30 ]; do
  if ! lsof -nP -iTCP:18080 -sTCP:LISTEN >/dev/null 2>&1; then
    if ! ps ax -o command= | grep -F 'models/minicpm/m.gguf' | grep -v grep >/dev/null 2>&1; then
      break
    fi
  fi
  sleep 1
  i=$((i + 1))
done
if lsof -nP -iTCP:18080 -sTCP:LISTEN >/dev/null 2>&1; then
  log "ROLLBACK_PORT_STILL_UP"
  exit 1
fi
# Stop only a leftover chat server that still has the MiniCPM weight path.
ps ax -o pid=,command= | while read -r pid cmd; do
  case "$cmd" in
    *models/minicpm/m.gguf*) kill "$pid" 2>/dev/null || true ;;
  esac
done
sleep 2

waited=0
while true; do
  LV=$(sysctl -n kern.memorystatus_level 2>/dev/null || echo 0)
  case "$LV" in ''|*[!0-9]*) LV=0 ;; esac
  if [ "$LV" -ge 40 ]; then
    log "ROLLBACK_MEM_OK level=$LV"
    break
  fi
  if [ "$waited" -ge 240 ]; then
    log "ROLLBACK_MEM_TIMEOUT level=$LV"
    exit 1
  fi
  sleep 5
  waited=$((waited + 5))
done

launchctl enable "gui/$U/$LABEL"
launchctl bootstrap "gui/$U" "$PLIST" 2>/dev/null || launchctl kickstart -k "gui/$U/$LABEL"
launchctl kickstart -k "gui/$U/$DOOR_LABEL" 2>/dev/null || true

i=0
ok=0
while [ "$i" -lt 54 ]; do
  if curl -sf -m 3 http://127.0.0.1:18080/health >/dev/null 2>&1; then
    if ps ax -o command= | grep -F 'models/minicpm/m.gguf' | grep -F -- '-c 32768' | grep -v grep >/dev/null 2>&1; then
      ok=1
      break
    fi
  fi
  sleep 5
  i=$((i + 1))
done
if [ "$ok" != 1 ]; then
  log "ROLLBACK_HEALTH_FAIL"
  exit 1
fi
log "ROLLBACK_DONE ctx=32768"
exit 0
