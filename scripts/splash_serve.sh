#!/bin/bash
# 2026-11-19: live model = Qwen3.6-35B-A3B UD-Q2_K_XL on Splash (DFlash2 draft, vision on).
# Reached from gemma_serve.sh when flags/gd_model_variant=splash, under the same launchd label
# (com.llm-appliance.mlx-qwen, KeepAlive) so every guard/restore layer keeps working.
# Layout: Splash engine on 127.0.0.1:$UP (internal) + splash_shim.py on :18080 (llama-server compatible:
# /health /slots /props /metrics /completion, field rewrites). The shim's argv carries
# "mlx-qwen-llama-server ... --port 18080" (macOS Python drops a custom argv0), so pgrep-based guards still find "the model server".
# Rollback: ~/claude-stack/bin/rollback_splash_to_minicpm.sh (MiniCPM files and minicpm_serve.sh untouched).
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
F=$HOME/claude-stack/data/flags
rf(){ [ -s "$1" ] && tr -d '[:space:]' < "$1" || printf '%s' "$2"; }
M=$(rf $F/splash_model_dir /Volumes/External/LLM-Model-Archive/qwen36-35b-a3b)
QUANT=$(rf $F/splash_quant UD-Q2_K_XL)
CTX=$(rf $F/splash_ctx 65536)
MEM=$(rf $F/splash_max_memory 18G)
DISK=$(rf $F/splash_cache_disk 4G)
UP=$(rf $F/splash_up_port 18085)
GGUF=$M/Qwen3.6-35B-A3B-$QUANT.gguf; MMPROJ=$M/mmproj-BF16.gguf; DRAFT=$M/dflash2
LOGD=$HOME/claude-stack/logs; mkdir -p "$LOGD" "$HOME/llm-appliance/runtime"
E=$(date +%s); echo $E > $HOME/llm-appliance/runtime/llama_start_epoch
echo "LLAMA_START_EPOCH=$E SPLASH quant=$QUANT ctx=$CTX mem=$MEM disk=$DISK up=$UP" >&2
# Required weights (External may mount late after a reboot): wait and log, never crash-loop.
waited=0
while :; do
  miss=""
  for f in "$GGUF" "$MMPROJ" "$DRAFT/model.safetensors" "$DRAFT/config.json"; do [ -s "$f" ] || miss="$miss $f"; done
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
exec /opt/homebrew/bin/splash serve --model local/Qwen3.6-35B-A3B --model-dir "$GGUF" --mmproj "$MMPROJ" --draft-model "$DRAFT" \
  --offline --host 127.0.0.1 --port "$UP" --no-webui \
  --served-model-name Qwen3.6-35B-A3B --served-model-name MiniCPM-V-4.7-35B-A3B --served-model-name Qwen3.8-27B-oQ3e-mtp \
  --max-memory "$MEM" --max-context "$CTX" --idle-release off --max-cache-disk "$DISK" \
  --default-reasoning-effort none >> "$LOGD/splash-server.log" 2>&1
