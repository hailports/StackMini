#!/usr/bin/env bash
# every 15 min: refresh the aggregate export (no model calls), then rebuild hailports.com data from box logs
LOG=$HOME/social/site/refresh.log
FAIL=$HOME/social/site/refresh.fail.log
mkdir -p $HOME/social/site
while true; do
  python3 $HOME/claude-stack/bin/hailports_live_export.py >/dev/null 2>&1
  touch $HOME/claude-stack/data/runtime/hb/live_export
  if [ ! -f $HOME/social/site/REFRESH_OFF ]; then
    if ! timeout 480 /workspace/hp/gen/refresh_site_data.sh >> "$LOG" 2>&1; then
      rc=$?
      echo "$(date '+%Y-%m-%d %H:%M:%S %Z') live-export-loop refresh_site_data exit $rc" | tee -a "$LOG" "$FAIL" >/dev/null
    fi
  fi
  sleep 900
done
