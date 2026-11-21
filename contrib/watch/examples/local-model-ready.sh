#!/bin/sh
# Probe the local model door. need=true only when the door is down after a soft retry.
set -eu
# WATCH_MODEL_URL is the runner's judge chat URL (POST only). A GET on it 404s and reads as "door down".
URL=${WATCH_READY_URL:-http://127.0.0.1:8080/v1/models}
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "$URL" || echo 000)
if [ "$code" = "200" ]; then
  printf '%s\n' '{"need":false,"summary":"local model ready","bot":null,"dedupe":"model-ready"}'
  exit 0
fi
sleep 2
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "$URL" || echo 000)
if [ "$code" = "200" ]; then
  printf '%s\n' '{"need":false,"summary":"local model recovered","bot":null,"dedupe":"model-ready"}'
  exit 0
fi
printf '%s\n' '{"need":true,"summary":"local model door down","bot":"stack","dedupe":"model-down","force":true}'
