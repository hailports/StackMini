#!/usr/bin/env bash
# Keep Chrome CDP alive on 127.0.0.1 — proxy-up ≠ Chrome-up. Kill hung procs.
set -euo pipefail
LOG="${ENSURE_CHROME_CDP_LOG:-/tmp/ensure_chrome_cdp.log}"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

chrome_http_ok() {
  local port="$1"
  local body
  body="$(timeout 3 curl -sf "http://127.0.0.1:${port}/json/version" 2>/dev/null || true)"
  [[ -n "$body" && "$body" == *"webSocketDebuggerUrl"* ]]
}

# 2026-11-03: one slow /json/version (>3s) at 20:18:49Z relaunched :9227 and dropped the Zoom
# session cookies. A live-but-slow browser now needs FAIL_STREAK_MAX consecutive failed ticks
# (each tick = one 3s probe + one 8s retry) before a relaunch. No browser process at all -> relaunch now.
FAIL_STREAK_MAX="${CHROME_CDP_FAIL_STREAK_MAX:-3}"
chrome_probe_patient() {
  local port="$1"
  chrome_http_ok "$port" && return 0
  sleep 2
  local body
  body="$(timeout 8 curl -sf "http://127.0.0.1:${port}/json/version" 2>/dev/null || true)"
  [[ -n "$body" && "$body" == *"webSocketDebuggerUrl"* ]]
}
chrome_proc_alive() {
  local port="$1"
  pgrep -f -- "--remote-debugging-port=${port}( |$)" >/dev/null 2>&1
}
post_launch_hook() {
  local port="$1"
  # Zoom sign-in/tab/sidebar repair after a :9227 relaunch. Background, locked, quiet when healthy.
  if [[ "$port" == "9227" && -x /opt/hailports/keeper/zoom_post_launch.py ]]; then
    setsid nohup /opt/hailports/keeper/.venv/bin/python /opt/hailports/keeper/zoom_post_launch.py run \
      >>/tmp/zoom_post_launch.out 2>&1 </dev/null &
    echo "$(ts) post-launch zoom hook queued pid=$!" | tee -a "$LOG"
  fi
}

kill_fork() {
  local display="$1"
  # Hidden CDP Chrome only; NEVER touch /home/operator/chrome-profile/* (agent desktops).
  pkill -f "user-data-dir=/home/operator/cdp-profile/" 2>/dev/null || true
  sleep 1
}

# 2026-10-14: display :5 no longer exists after the box rebuild and the sand
# box-chrome launcher times out on a private Xvfb. Run a persistent Xvfb :5 and
# 2026-10-14b: dedicated profile /home/operator/cdp-profile/9227 on Xvfb :99 (Fork-N belong to desktops).
# launch Chrome directly with CDP on 127.0.0.1:9227.
# 2026-11-03 (the operator approved): --disable-http2. Every *.zoom.us host resolves to the egress fake-IP
# the egress fake-IP, so Chrome coalesced HTTP/2 connections across zoom.us hosts and xmppapi/ark/asyncim
# calls got Cloudflare 530/526 pages (no CORS) -> Team Chat stuck loading with 0 rows.
ensure_xvfb5() {
  if ! pgrep -f "Xvfb :99 " >/dev/null 2>&1; then
    setsid nohup Xvfb :99 -screen 0 1600x1000x24 -nolisten tcp >>/tmp/xvfb99.log 2>&1 &
    sleep 2
  fi
}

start_chrome() {
  local display="$1" url="${2:-}"
  if [[ "$display" == "5" ]]; then
    ensure_xvfb5
    mkdir -p /tmp/xdg-runtime-cdp-99 /home/operator/cdp-profile/9227
    DISPLAY=:99 HOME=/home/operator XDG_RUNTIME_DIR=/tmp/xdg-runtime-cdp-99 setsid nohup google-chrome-stable \
      --no-sandbox --disable-dev-shm-usage --password-store=basic --no-first-run \
      --no-default-browser-check --hide-crash-restore-bubble \
      --user-data-dir=/home/operator/cdp-profile/9227 \
      --remote-debugging-port=9227 --remote-debugging-address=127.0.0.1 \
      --proxy-pac-url=file:///opt/hailports/keeper/sso.pac \
      --disable-http2 \
      ${url:+--new-window "$url"} >>/tmp/chrome:5.ensure.log 2>&1 &
    echo $!
    return 0
  fi
  local xdg="/tmp/xdg-runtime-box-${display}"
  mkdir -p "$xdg"
  export DISPLAY=":${display}"
  export HOME=/home/operator
  export XDG_RUNTIME_DIR="$xdg"
  if [[ -S "${xdg}/bus" ]]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=${xdg}/bus"
  fi
  if [[ -n "$url" ]]; then
    nohup /usr/local/bin/box-chrome --new-window "$url" >>"/tmp/chrome:${display}.ensure.log" 2>&1 &
  else
    nohup /usr/local/bin/box-chrome >>"/tmp/chrome:${display}.ensure.log" 2>&1 &
  fi
  echo $!
}

ensure_one() {
  local port="$1" display="$2" url="${3:-}"
  local streak_file="/tmp/ensure_chrome_cdp.${port}.failstreak"
  if chrome_probe_patient "$port"; then
    rm -f "$streak_file"
    echo "$(ts) ok chrome :${port} (display :${display})"
    return 0
  fi
  if chrome_proc_alive "$port"; then
    local streak
    streak=$(( $(cat "$streak_file" 2>/dev/null || echo 0) + 1 ))
    echo "$streak" > "$streak_file"
    if (( streak < FAIL_STREAK_MAX )); then
      echo "$(ts) SLOW chrome :${port} probe failed (streak ${streak}/${FAIL_STREAK_MAX}) — not restarting yet" | tee -a "$LOG"
      return 0
    fi
  fi
  rm -f "$streak_file"
  echo "$(ts) DEAD/HUNG chrome :${port} (display :${display}) — restarting" | tee -a "$LOG"
  kill_fork "$display"
  local pid
  pid="$(start_chrome "$display" "$url")"
  echo "$(ts) started box-chrome display=:${display} pid=${pid}" | tee -a "$LOG"
  local i
  for i in $(seq 1 25); do
    sleep 1
    if chrome_http_ok "$port"; then
      echo "$(ts) recovered chrome :${port} after ${i}s" | tee -a "$LOG"
      post_launch_hook "$port"
      return 0
    fi
  done
  echo "$(ts) FAIL chrome :${port} still dead after restart" | tee -a "$LOG"
  return 1
}

ensure_one 9227 5 "https://app.zoom.us/wc/team-chat" || true
# :9225 (display :3) paused 2026-10-14: not up after rebuild, restart loop churned display :3. Set KEEPER_ENSURE_9225=1 to re-enable.
[[ "${KEEPER_ENSURE_9225:-0}" == "1" ]] && { ensure_one 9225 3 "" || true; }
