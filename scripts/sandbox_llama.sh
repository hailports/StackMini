#!/bin/bash
# Guarded sandbox llama-server for A/B benchmarks. Never binds a protected (live) port.
# usage: scripts/sandbox_llama.sh PORT LOG -- <llama-server args...>
# Kills the sandbox when memory pressure (kern.memorystatus_level) drops below MIN_LEVEL or
# swap grows by more than MAX_SWAP_MB since start. macOS only.
set -u
PORT=$1; LOG=$2; shift 3
BIN=${SANDBOX_BIN:-llama-server}
MIN_LEVEL=${MIN_LEVEL:-5}; MAX_SWAP_MB=${MAX_SWAP_MB:-1200}
PROTECTED=${PROTECTED_PORTS:-"8080 18080"}
for p in $PROTECTED; do [ "$PORT" = "$p" ] && { echo "refusing protected port $PORT"; exit 2; }; done
swap_mb() { sysctl -n vm.swapusage | sed -E 's/.*used = ([0-9.]+)M.*/\1/' | cut -d. -f1; }
S0=$(swap_mb)
echo "$(date -Iseconds) start port=$PORT level=$(sysctl -n kern.memorystatus_level) swap0=${S0}MB" >> "$LOG.guard"
"$BIN" --host 127.0.0.1 --port "$PORT" "$@" >> "$LOG" 2>&1 &
PID=$!
echo $PID > "$LOG.pid"
while kill -0 $PID 2>/dev/null; do
  L=$(sysctl -n kern.memorystatus_level); S=$(swap_mb)
  if [ "$L" -lt "$MIN_LEVEL" ] || [ $((S - S0)) -gt "$MAX_SWAP_MB" ]; then
    echo "$(date -Iseconds) KILL level=$L swap=${S}MB (swap0=${S0})" >> "$LOG.guard"
    kill $PID; sleep 2; kill -9 $PID 2>/dev/null; break
  fi
  echo "$(date -Iseconds) level=$L swap=${S}MB" >> "$LOG.guard"
  sleep 3
done
echo "$(date -Iseconds) exit" >> "$LOG.guard"
