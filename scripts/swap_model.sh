#!/bin/bash
# Apply a model/context flag set for the live :8080 stack, with memory regression guard.
# Usage: swap_model.sh splash|minicpm [--force] [flag=value ...]
# Examples:
#   swap_model.sh splash splash_max_memory=13.5G splash_expert_memory=4G
#   swap_model.sh minicpm
set -euo pipefail
F=$HOME/claude-stack/data/flags
L=$HOME/claude-stack/logs/splash-cutover.log
log(){ echo "$(TZ=America/Chicago date '+%Y-%m-%d %H:%M:%S %Z') $*" | tee -a "$L"; }
TARGET=${1:-}; shift || true
FORCE_ARGS=()
[ "${1:-}" = "--force" ] && { FORCE_ARGS=(--force); shift; }
case "$TARGET" in
  splash|minicpm) ;;
  *) echo "usage: swap_model.sh splash|minicpm [--force] [flag=value ...]"; exit 2;;
esac
for kv in "$@"; do
  case "$kv" in
    *=*)
      k=${kv%%=*}; v=${kv#*=}
      printf '%s\n' "$v" > "$F/$k"
      ;;
    *) echo "bad flag $kv"; exit 2;;
  esac
done
echo "$TARGET" > "$F/gd_model_variant"
if [ "$TARGET" = "splash" ]; then
  "$HOME/claude-stack/bin/splash_mem_budget_guard.sh" "${FORCE_ARGS[@]}" || exit $?
fi
log "SWAP_START target=$TARGET force=${FORCE_ARGS[*]:-no}"
launchctl kickstart -k "gui/$(id -u)/com.llm-appliance.mlx-qwen"
for i in $(seq 1 120); do
  curl -s -m2 127.0.0.1:8080/health | grep -q 'ok' && break
  sleep 5
done
log "SWAP_END health=$(curl -s -m3 127.0.0.1:8080/health)"
