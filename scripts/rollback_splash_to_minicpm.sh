#!/bin/bash
# One-command rollback: live :18080 back to MiniCPM-V-4.7-35B-A3B exactly as it ran before 2026-11-19
# (minicpm_serve.sh, its flags and weights were never changed). Logs to logs/splash-cutover.log in CT.
F=$HOME/claude-stack/data/flags; L=$HOME/claude-stack/logs/splash-cutover.log
log(){ echo "$(TZ=America/Chicago date '+%Y-%m-%d %H:%M:%S %Z') $*" | tee -a "$L"; }
log "ROLLBACK_START variant=$(cat $F/gd_model_variant)"
echo minicpm > $F/gd_model_variant
printf '%s\n' 'MiniCPM-V-4.7-35B-A3B' > $F/live_chat_alias
launchctl kickstart -k "gui/$(id -u)/com.llm-appliance.mlx-qwen"
for i in $(seq 1 120); do
  P=$(pgrep -f 'mlx-qwen-llama-server -m' | head -1)
  [ -n "$P" ] && curl -s -m2 127.0.0.1:18080/health | grep -q '"ok"' && break
  sleep 5
done
P=$(pgrep -f 'mlx-qwen-llama-server -m' | head -1)
log "ROLLBACK_END pid=${P:-none} health=$(curl -s -m3 127.0.0.1:18080/health) argv=$(ps -o command= -p ${P:-1} | cut -c1-300)"
