#!/bin/bash
# Live chat model: Gemma 4 26B-A4B QAT. Text only. Context 8192. No MTP. No projector.
# LaunchAgent label stays the existing mlx-qwen label. argv0 stays mlx-qwen-llama-server
# so the memory budget rule that matches that name skips this process.
# Does not read MiniCPM extra flags (no mmproj, no draft, no 32k context).
E=$(date +%s)
mkdir -p "$HOME/llm-appliance/runtime" "$HOME/claude-stack/logs"
echo "$E" > "$HOME/llm-appliance/runtime/llama_start_epoch"
echo "LLAMA_START_EPOCH=$E" >&2

read_flag() {
  if [ -f "$1" ]; then
    tr -d '[:space:]' < "$1"
  else
    printf '%s' "$2"
  fi
}

BIN=$(read_flag "$HOME/claude-stack/data/flags/gemma_bin" "")
MODEL=$(read_flag "$HOME/claude-stack/data/flags/gemma_model" "")
ALIAS=$(read_flag "$HOME/claude-stack/data/flags/gemma_alias" "Gemma-4-26B-A4B-it-QAT")

if [ ! -x "$BIN" ] || [ ! -s "$MODEL" ]; then
  echo "GEMMA_PREFLIGHT_FAIL" >&2
  sleep 20
  exit 1
fi

# Higher kern.memorystatus_level means more free memory. Do not load while it is bad.
waited=0
while true; do
  LV=$(sysctl -n kern.memorystatus_level 2>/dev/null || echo 0)
  case "$LV" in
    ''|*[!0-9]*) LV=0 ;;
  esac
  if [ "$LV" -ge 40 ]; then
    echo "MEM_OK level=$LV" >&2
    break
  fi
  if [ "$waited" -ge 240 ]; then
    echo "MEM_WAIT_TIMEOUT level=$LV" >&2
    exit 1
  fi
  echo "MEM_WAIT level=$LV waited=$waited" >&2
  sleep 5
  waited=$((waited + 5))
done

echo "LLAMA_BIN_OK alias=$ALIAS ctx=8192 vision=no mtp=no" >&2
exec nice -n 15 /bin/bash -c 'exec -a mlx-qwen-llama-server "$0" "$@"' \
  "$BIN" \
  -m "$MODEL" \
  --alias "$ALIAS" \
  --host 127.0.0.1 \
  --port 18080 \
  -c 8192 \
  -fa on \
  -ctk q8_0 \
  -ctv q8_0 \
  -b 256 \
  -ub 128 \
  -t 4 \
  -tb 4 \
  -ngl 99 \
  --jinja \
  --reasoning off \
  --reasoning-budget 0 \
  --no-reasoning-preserve \
  --cache-ram 512 \
  --parallel 1 \
  --metrics
