#!/bin/bash
# Keep the cron daemon running. This host has no systemd, and the cron
# package cannot be installed from apt (the mirror returns 500 and the
# common package requires systemd-sysusers). The daemon binary is already
# unpacked at /usr/sbin/cron.
while true; do
  if ! pgrep -x cron >/dev/null 2>&1; then
    sudo -n /usr/sbin/cron || true
  fi
  sleep 300
done
