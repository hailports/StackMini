#!/bin/bash
# 2026-11-21: Qwen3.6-35B-A3B UD-Q2_K_XL on Splash at 64K with an enforced Metal budget.
# 2026-11-19: live model = Qwen3.6-35B-A3B UD-Q2_K_XL on Splash (DFlash2 draft, vision on).
# Reached from gemma_serve.sh when flags/gd_model_variant=splash, under the same launchd label
# (com.llm-appliance.mlx-qwen, KeepAlive) so every guard/restore layer keeps working.
# Layout: Splash engine on 127.0.0.1:$UP (internal) + splash_shim.py on :18080 (llama-server compatible:
# /health /slots /props /metrics /completion, field rewrites). The shim's argv carries
# "mlx-qwen-llama-server ... --port 18080" (macOS Python drops a custom argv0), so pgrep-based guards still find "the model server".
# Rollback: ~/claude-stack/bin/rollback_splash_to_minicpm.sh (MiniCPM files and minicpm_serve.sh untouched).
# Memory knobs (flags/, defaults enforce the 13.5G model+cache budget):
#   splash_max_memory (13.5G) splash_expert_memory (auto) splash_state_format (fp32)
#   splash_kv_format (int8) splash_queue_size (16) splash_language_only (0) splash_ctx (65536)
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
# Refuse silent regressions that exceed the wired budget (override: SPLASH_MEM_FORCE=1).
if [ -x "$HOME/claude-stack/bin/splash_mem_budget_guard.sh" ]; then
  "$HOME/claude-stack/bin/splash_mem_budget_guard.sh" || exit 9
fi
F=$HOME/claude-stack/data/flags
rf(){ [ -s "$1" ] && tr -d '[:space:]' < "$1" || printf '%s' "$2"; }
M=$(rf $F/splash_model_dir /Volumes/External/LLM-Model-Archive/qwen36-35b-a3b)
QUANT=$(rf $F/splash_quant UD-Q2_K_XL)
CTX=$(rf $F/splash_ctx 65536)
MEM=$(rf $F/splash_max_memory 13.5G)
DISK=$(rf $F/splash_cache_disk 4G)
UP=$(rf $F/splash_up_port 18085)
EXPERT=$(rf $F/splash_expert_memory auto)
STATE=$(rf $F/splash_state_format fp32)
KV=$(rf $F/splash_kv_format int8)
QUEUE=$(rf $F/splash_queue_size 16)
LANGONLY=$(rf $F/splash_language_only 0)
# Pinned engine build (side-by-side Homebrew kegs; rollback = point flags/splash_bin at the old keg wrapper,
# see scripts/splash-keg.sh). The plain bin/splash wrapper follows opt/splash, i.e. the newest keg.
SPLASH_BIN=$(rf $F/splash_bin /opt/homebrew/bin/splash); [ -x "$SPLASH_BIN" ] || SPLASH_BIN=/opt/homebrew/bin/splash
GGUF=$M/Qwen3.6-35B-A3B-$QUANT.gguf; MMPROJ=$M/mmproj-BF16.gguf; DRAFT=$M/dflash2
LOGD=$HOME/claude-stack/logs; mkdir -p "$LOGD" "$HOME/llm-appliance/runtime"
E=$(date +%s); echo $E > $HOME/llm-appliance/runtime/llama_start_epoch
echo "LLAMA_START_EPOCH=$E SPLASH quant=$QUANT ctx=$CTX mem=$MEM expert=$EXPERT state=$STATE kv=$KV queue=$QUEUE langonly=$LANGONLY disk=$DISK up=$UP bin=$SPLASH_BIN" >&2
# Required weights (External may mount late after a reboot): wait and log, never crash-loop.
waited=0
while :; do
  miss=""
  need="$GGUF $DRAFT/model.safetensors $DRAFT/config.json"
  [ "$LANGONLY" = "1" ] || need="$need $MMPROJ"
  for f in $need; do [ -s "$f" ] || miss="$miss $f"; done
  [ -z "$miss" ] && break
  echo "$(date -Iseconds) splash waiting for weights:$miss" >> "$LOGD/llama-boot-gate.log"
  echo "WAITING_WEIGHTS$miss" >&2; sleep 15; waited=$((waited+15))
done
# A stale engine from a killed run would hold the internal port.
pkill -f "libexec/install/launcher.py serve .*--port $UP" 2>/dev/null && sleep 2
# Shim: restarted if it dies; exits by itself when this process (Splash after exec) is gone.
SELF=$$
(
  while kill -0 $SELF 2>/dev/null; do
    SHIM_PARENT_PID=$SELF SHIM_UP_PORT=$UP SHIM_PORT=18080 SHIM_CTX=$CTX SHIM_MODEL=Qwen3.6-35B-A3B \
    SHIM_MODEL_PATH="$GGUF" SHIM_LOG="$LOGD/splash-shim.log" \
      /opt/homebrew/bin/python3 -u "$HOME/claude-stack/bin/splash_shim.py" --tag mlx-qwen-llama-server --port 18080 \
      >> "$LOGD/splash-shim.log" 2>&1
    sleep 1
  done
) &
ARGS=(serve --model local/Qwen3.6-35B-A3B --model-dir "$GGUF" --draft-model "$DRAFT"
  --offline --host 127.0.0.1 --port "$UP" --no-webui
  --served-model-name Qwen3.6-35B-A3B --served-model-name MiniCPM-V-4.7-35B-A3B --served-model-name Qwen3.8-27B-oQ3e-mtp
  --max-memory "$MEM" --max-context "$CTX" --idle-release off --max-cache-disk "$DISK"
  --kv-format "$KV" --state-format "$STATE" --queue-size "$QUEUE"
  --default-reasoning-effort none)
if [ "$LANGONLY" = "1" ]; then
  ARGS+=(--language-only)
else
  ARGS+=(--mmproj "$MMPROJ")
fi
if [ -n "$EXPERT" ] && [ "$EXPERT" != "auto" ] && [ "$EXPERT" != "off" ]; then
  ARGS+=(--expert-memory "$EXPERT")
elif [ "$EXPERT" = "off" ]; then
  ARGS+=(--expert-memory off)
fi
exec "$SPLASH_BIN" "${ARGS[@]}" >> "$LOGD/splash-server.log" 2>&1
