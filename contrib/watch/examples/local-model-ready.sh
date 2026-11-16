#!/bin/sh
# Probe the local model door. need=true only when the door is down after a soft retry.
set -eu
URL=${WATCH_MODEL_URL:-http://127.0.0.1:8080/v1/models}
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
