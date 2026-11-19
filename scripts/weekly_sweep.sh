#!/bin/bash
# Weekly health sweep: launchd exit codes, health URLs, memory/swap, per-source index freshness,
# and a short decode-speed probe. Read-only. Writes one markdown report and prints its path.
#
# Env: LABEL_PREFIX (space-separated launchd label prefixes), HEALTH_URLS (space-separated), INDEX_DB (sqlite with
#      docs(source, ts)), CHAT_URL (OpenAI-compatible chat endpoint), OUT_DIR (report folder),
#      ENGINE_URL (Splash engine base, e.g. http://127.0.0.1:18085), SHIM_URL (front shim base),
#      ENGINE_LOG (engine log file to count errors in)
set -u
OUT_DIR=${OUT_DIR:-./sweeps}; mkdir -p "$OUT_DIR"
R=$OUT_DIR/sweep-$(date +%F).md
{
echo "# Weekly sweep $(date '+%Y-%m-%d %H:%M %Z')"
echo; echo "## Memory"
echo "- memorystatus_level: $(sysctl -n kern.memorystatus_level 2>/dev/null)"
echo "- $(sysctl vm.swapusage 2>/dev/null)"
echo "- disk free /: $(df -h / | awk 'NR==2{print $4}')"
echo; echo "## launchd jobs with a non-zero last exit"
for P in ${LABEL_PREFIX:-}; do
  launchctl list | awk -v p="$P" 'NR>1 && index($3,p)==1 && $2!="0" && $2!="-" {print "- "$3" pid="$1" last_exit="$2}'
done
echo; echo "## Health"
for u in ${HEALTH_URLS:-}; do echo "- $u: $(curl -s -m5 "$u" | cut -c1-120 || echo DOWN)"; done
if [ -n "${INDEX_DB:-}" ] && [ -f "$INDEX_DB" ]; then
  echo; echo "## Index freshness (newest ts per source)"
  sqlite3 -readonly "$INDEX_DB" "SELECT '- '||source||': '||CASE WHEN typeof(m)='text' THEN m ELSE datetime(m,'unixepoch','localtime')||' ('||printf('%.1f',(strftime('%s','now')-m)/3600.0)||' h ago)' END||', '||n||' docs' FROM (SELECT source, max(ts) m, count(*) n FROM docs GROUP BY source) ORDER BY m DESC;"
fi
if [ -n "${ENGINE_URL:-}" ]; then
  echo; echo "## Model server"
  echo "- engine /ready: $(curl -s -m5 "$ENGINE_URL/ready" | cut -c1-120 || echo DOWN)"
  echo "- engine /status: $(curl -s -m5 "$ENGINE_URL/status" | tr -d '\n' | cut -c1-200 || echo DOWN)"
  echo "- models: $(curl -s -m5 "$ENGINE_URL/v1/models" | python3 -c 'import json,sys;print(", ".join(m["id"] for m in json.load(sys.stdin)["data"]))' 2>/dev/null || echo n/a)"
fi
if [ -n "${SHIM_URL:-}" ]; then
  curl -s -m5 "$SHIM_URL/metrics" | awk '/^splash_shim_|^llamacpp:(prompt_tokens_total|tokens_predicted_total) /{print "- "$1" "$2}'
fi
if [ -n "${ENGINE_LOG:-}" ] && [ -f "$ENGINE_LOG" ]; then
  echo "- engine log errors (excluding not_found): $(grep -E 'Error|failed|unavailable|native loop' "$ENGINE_LOG" | grep -vc not_found)"
fi
if [ -n "${CHAT_URL:-}" ]; then
  echo; echo "## Decode probe"
  python3 - "$CHAT_URL" <<'PY'
import json, sys, time, urllib.request
url = sys.argv[1]
body = {"messages": [{"role": "user", "content": "Explain in 200 words how an LRU cache works."}],
        "max_tokens": 300, "temperature": 0}
t = time.time()
try:
    r = json.load(urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(),
        {"Content-Type": "application/json"}), timeout=300))
    tm = r.get("timings") or {}
    n = r.get("usage", {}).get("completion_tokens", 0)
    print(f"- decode {tm.get('predicted_per_second') or n / (time.time() - t):.1f} tok/s, "
          f"prompt {tm.get('prompt_per_second', 0):.0f} tok/s, wall {time.time() - t:.1f} s")
except Exception as e:
    print(f"- probe failed: {type(e).__name__}")
PY
fi
} > "$R"
echo "$R"
