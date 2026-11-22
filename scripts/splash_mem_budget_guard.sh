#!/bin/bash
# Refuse a Splash/model swap whose projected wired budget exceeds the Mini memory budget
# unless SPLASH_MEM_FORCE=1 or --force is passed.
# Wired projection (GB) ~= weights + draft + vision + expert_cache + kv_headroom
# Budget default: data/flags/splash_wired_budget_gb (13.5).
set -euo pipefail
F=${HOME}/claude-stack/data/flags
BUDGET=$(tr -d '[:space:]' < "$F/splash_wired_budget_gb" 2>/dev/null || echo 13.5)
FORCE=0
for a in "$@"; do [ "$a" = "--force" ] && FORCE=1; done
[ "${SPLASH_MEM_FORCE:-0}" = "1" ] && FORCE=1

parse_size_gb() {
  # 13.5G / 4G / 65536 (tokens ignored) / auto/off -> number or -1
  local v="$1"
  case "$v" in
    auto|off|"") echo -1; return;;
  esac
  # strip trailing G/g/M/m
  if [[ "$v" =~ ^([0-9.]+)[Gg]$ ]]; then echo "${BASH_REMATCH[1]}"; return; fi
  if [[ "$v" =~ ^([0-9.]+)[Mm]$ ]]; then awk -v m="${BASH_REMATCH[1]}" 'BEGIN{printf "%.4f", m/1024}'; return; fi
  if [[ "$v" =~ ^[0-9.]+$ ]]; then echo "$v"; return; fi
  echo -1
}

MEM=$(tr -d '[:space:]' < "$F/splash_max_memory" 2>/dev/null || echo 13.5G)
EXP=$(tr -d '[:space:]' < "$F/splash_expert_memory" 2>/dev/null || echo 4G)
CTX=$(tr -d '[:space:]' < "$F/splash_ctx" 2>/dev/null || echo 65536)
LANG=$(tr -d '[:space:]' < "$F/splash_language_only" 2>/dev/null || echo 0)
QUANT=$(tr -d '[:space:]' < "$F/splash_quant" 2>/dev/null || echo UD-Q2_K_XL)

MEM_GB=$(parse_size_gb "$MEM")
EXP_GB=$(parse_size_gb "$EXP")
# Hard floor for this GGUF family measured 2026-11-21:
# target weights 12.29G when fully resident; with expert streaming, use expert_memory + 3.2G shared/vision/draft/runtime.
WEIGHTS_FULL=12.29
DRAFT=0.48
VISION=0.90
RUNTIME=1.80
if [ "$LANG" = "1" ]; then VISION=0; fi
if [ "$EXP_GB" = "-1" ]; then
  # auto/off: assume full resident weights when max-memory is high enough
  if awk -v m="$MEM_GB" 'BEGIN{exit !(m<0 || m>=15.5)}'; then
    MODEL_GB=$(awk -v w=$WEIGHTS_FULL -v d=$DRAFT -v v=$VISION -v r=$RUNTIME 'BEGIN{printf "%.2f", w+d+v+r}')
  else
    # tight max-memory forces streaming; assume 4G expert floor
    MODEL_GB=$(awk -v e=4 -v d=$DRAFT -v v=$VISION -v r=$RUNTIME 'BEGIN{printf "%.2f", e+3.2+d+v+r}')
  fi
else
  # expert store + shared (~3.2G of non-expert) + draft + vision + runtime
  MODEL_GB=$(awk -v e="$EXP_GB" -v d=$DRAFT -v v=$VISION -v r=$RUNTIME 'BEGIN{printf "%.2f", e+3.2+d+v+r}')
fi
# Cap by configured max-memory when set
if awk -v m="$MEM_GB" 'BEGIN{exit !(m>0)}'; then
  MODEL_GB=$(awk -v a="$MODEL_GB" -v m="$MEM_GB" 'BEGIN{printf "%.2f", (a<m?a:m)}')
fi

# Context: 64K int8 KV for this model ~ kv_page * pages; splash status measured ~dynamic up to ~4G at 18G budget.
# Scale rough KV headroom: ctx/65536 * 1.5G under a 13.5G metal budget.
KV_GB=$(awk -v c="$CTX" 'BEGIN{printf "%.2f", (c/65536.0)*1.5}')
PROJ=$(awk -v m="$MODEL_GB" -v k="$KV_GB" 'BEGIN{printf "%.2f", m+k}')

echo "splash_mem_budget_guard: project=${PROJ}G budget=${BUDGET}G mem=$MEM expert=$EXP ctx=$CTX quant=$QUANT langonly=$LANG"

if awk -v p="$PROJ" -v b="$BUDGET" 'BEGIN{exit !(p>b+0.05)}'; then
  if [ "$FORCE" = "1" ]; then
    echo "WARNING: over budget but --force/SPLASH_MEM_FORCE=1; proceeding" >&2
    exit 0
  fi
  echo "REFUSE: projected wired ${PROJ}G exceeds budget ${BUDGET}G. Pass --force or SPLASH_MEM_FORCE=1 to override." >&2
  exit 9
fi
exit 0
