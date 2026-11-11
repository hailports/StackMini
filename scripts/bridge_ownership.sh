#!/bin/sh
# Launchd ownership proof for a persistent local bridge.
# A manual browser can match the profile and port. It cannot also be the pid
# of this exact installed job.

# BRIDGE_LAUNCHD_OWNERSHIP_BEGIN
launchd_job_print() {
  /bin/launchctl print "gui/$(id -u)/${BROWSER_LABEL}" 2>/dev/null
}

launchd_field() {
  local output="$1" wanted="$2"
  printf '%s\n' "$output" | /usr/bin/awk -v wanted="$wanted" '
    {
      line = $0
      key = line
      sub(/[[:space:]]*=.*/, "", key)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
      if (key == wanted) {
        sub(/^[^=]*=[[:space:]]*/, "", line)
        print line
        exit
      }
    }
  '
}

launchd_has_argument() {
  local output="$1" wanted="$2"
  printf '%s\n' "$output" | /usr/bin/awk -v wanted="$wanted" '
    {
      line = $0
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
      if (line == wanted) found = 1
    }
    END { exit(found ? 0 : 1) }
  '
}

# Live argv of the exact bridge pid. Tests override this; production uses ps.
live_bridge_command() {
  ps -o command= -p "$1" 2>/dev/null || true
}

# True when that live argv is this bridge's headless browser (profile, port, url).
# Flag order is the keepalive exec order. A debug port alone is not enough.
live_bridge_command_matches() {
  local cmd
  cmd=$(live_bridge_command "$1")
  case "$cmd" in
    "$BROWSER "*"--headless=new"*"--user-data-dir=${PROFILE}"*"--remote-debugging-port=${PORT}"*"${URL}"*)
      return 0
      ;;
  esac
  return 1
}

launchd_owned_bridge_pid() {
  local exact output launch_pid state program job_path expected_path keepalive keepalive_dir
  [ "$PERSISTENT" = 1 ] || return 1
  exact=$(exact_bridge_pid) || return 1
  output=$(launchd_job_print) || return 1

  state=$(launchd_field "$output" state)
  program=$(launchd_field "$output" program)
  launch_pid=$(launchd_field "$output" pid)
  job_path=$(launchd_field "$output" path)
  expected_path="$HOME/Library/LaunchAgents/${BROWSER_LABEL}.plist"

  # launchd reports a live kept-alive job as "languishing"; the pid match still proves ownership.
  case "$state" in running|languishing) ;; *) return 1 ;; esac
  [ "$launch_pid" = "$exact" ] || return 1
  [ "$job_path" = "$expected_path" ] || return 1

  if [ "$program" = "$BROWSER" ] \
    && launchd_has_argument "$output" "$BROWSER" \
    && launchd_has_argument "$output" "--headless=new" \
    && launchd_has_argument "$output" "--user-data-dir=$PROFILE" \
    && launchd_has_argument "$output" "--remote-debugging-port=$PORT" \
    && launchd_has_argument "$output" "$URL"; then
    printf '%s\n' "$exact"
    return 0
  fi

  # Obscura serve is a different exact tuple, and only when this bridge's
  # BROWSER is that binary. It does not relax the browser checks above.
  # Required whole arguments: the binary, serve, this profile, this port, --stealth.
  case "$BROWSER" in
    */obscura)
      if [ "$program" = "$BROWSER" ] \
        && launchd_has_argument "$output" "$BROWSER" \
        && launchd_has_argument "$output" "serve" \
        && launchd_has_argument "$output" "$PROFILE" \
        && launchd_has_argument "$output" "$PORT" \
        && launchd_has_argument "$output" "--stealth"; then
        printf '%s\n' "$exact"
        return 0
      fi
      return 1
      ;;
  esac

  # A bash keepalive execs the browser. launchd keeps printing program=/bin/bash
  # and the script path, while the job pid is the browser. Adopt only when the
  # live argv is this exact headless profile, port, and url. Any other bash job
  # is rejected. A manual browser cannot also be this job's pid.
  keepalive_dir="${BRIDGE_KEEPALIVE_DIR:-$HOME/stack/bin}"
  keepalive="$keepalive_dir/${BROWSER_LABEL##*.}-bridge-keepalive.sh"
  if [ "$program" = "/bin/bash" ] \
    && launchd_has_argument "$output" "/bin/bash" \
    && launchd_has_argument "$output" "$keepalive" \
    && live_bridge_command_matches "$exact"; then
    printf '%s\n' "$exact"
    return 0
  fi
  return 1
}
# BRIDGE_LAUNCHD_OWNERSHIP_END

# BRIDGE_OWNED_RECEIPT_BEGIN
owned_bridge_pid() {
  local recorded="" exact adopted receipt_tmp
  exact=$(exact_bridge_pid) || return 1
  if [ -f "$OWNED" ]; then
    recorded=$(cat "$OWNED" 2>/dev/null || echo "")
    case "$recorded" in
      *[!0-9]*|"") recorded="" ;;
    esac
  fi
  if [ "$recorded" = "$exact" ]; then
    printf '%s\n' "$exact"
    return 0
  fi

  adopted=$(launchd_owned_bridge_pid) || return 1
  [ "$adopted" = "$exact" ] || return 1
  receipt_tmp="${OWNED}.adopt.$$"
  ( umask 077; printf '%s\n' "$exact" > "$receipt_tmp" ) || {
    rm -f "$receipt_tmp"
    return 1
  }
  chmod 600 "$receipt_tmp" 2>/dev/null || true
  if ! mv -f "$receipt_tmp" "$OWNED"; then
    rm -f "$receipt_tmp"
    return 1
  fi
  log "ADOPTED exact launchd-owned bridge pid $exact after reboot"
  printf '%s\n' "$exact"
}
# BRIDGE_OWNED_RECEIPT_END
