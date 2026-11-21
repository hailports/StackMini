#!/bin/zsh
# macOS memory/hold gate. exit 0 = safe to make the next model call.
# HOLD_FILE (default ~/.local_engine_hold), QUIET_START/QUIET_END as HHMM (optional),
# MIN_FREE (% free, default 10), MAX_SWAP_MB (default 3990).
HOLD=${HOLD_FILE:-$HOME/.local_engine_hold}
[ -e "$HOLD" ] && { echo "STOP hold flag"; exit 3; }
if [ -n "${QUIET_START:-}" ] && [ -n "${QUIET_END:-}" ]; then
  hm=$(date +%H%M); [ "$hm" -ge "$QUIET_START" ] && [ "$hm" -lt "$QUIET_END" ] && { echo "STOP quiet window"; exit 4; }
fi
free=$(memory_pressure | awk -F': ' '/free percentage/{gsub("%","",$2);print $2}')
swap=$(sysctl -n vm.swapusage | awk '{for(i=1;i<=NF;i++) if($i=="used") {v=$(i+2); sub("M","",v); print int(v)}}')
lvl=$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null)
echo "free=${free}% swap_used=${swap}M pressure_level=${lvl}"
[ "${free:-0}" -lt ${MIN_FREE:-10} ] && { echo "STOP low free"; exit 5; }
[ "${swap:-0}" -ge ${MAX_SWAP_MB:-3990} ] && { echo "STOP swap"; exit 6; }
[ "${lvl:-1}" -ge 2 ] && { echo "STOP pressure warn/critical"; exit 7; }
exit 0
