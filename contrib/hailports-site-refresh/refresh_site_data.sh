#!/usr/bin/env bash
# 15-minute site data entry point. dist is the canonical site source.
# hp_daily itself is collected by the existing ~hourly supervisor job.
# Do NOT rebuild the obsolete /workspace/hp/build template: it predates product pages.
set -u
LOCK=/tmp/hp_site_refresh.lock
exec 9>"$LOCK"
flock -n 9 || exit 0
mkdir -p $HOME/social/site
LOG=$HOME/social/site/refresh.log
FAIL=$HOME/social/site/refresh.fail.log
if [ -x $HOME/claude-stack/bin/ensure_mini_ssh_config.sh ]; then
  $HOME/claude-stack/bin/ensure_mini_ssh_config.sh >> "$LOG" 2>&1 || echo "ensure_mini_ssh_config failed $(date '+%Y-%m-%d %H:%M:%S %Z')" | tee -a "$LOG" "$FAIL" >/dev/null
fi
set +e
python3 $HOME/hailports/site/tools/refresh_public_content.py --refresh >> "$LOG" 2>&1
rc1=$?
python3 $HOME/hailports/site/tools/live_stats_push.py --deploy >> "$LOG" 2>&1
rc2=$?
set -e
if [ "$rc1" -ne 0 ] || [ "$rc2" -ne 0 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S %Z') REFRESH FAIL public=$rc1 live=$rc2" | tee -a "$LOG" "$FAIL"
  exit 1
fi
exit 0
