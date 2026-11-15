#!/bin/bash
# Install the hailports identity include and shared hook dir on this machine.
# Put private terms (names, employers, hostnames), one per line, in
# ~/.config/hailports-guard/deny.txt; the guard blocks public pushes without it.
set -eu
G=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
INC="$HOME/.config/git/hailports.gitconfig"
mkdir -p "$(dirname "$INC")"
cat > "$INC" <<CFG
# Forced for every clone whose remote is github.com/hailports/*.
[user]
	name = HailPorts
	email = noreply@hailports.com
	useConfigOnly = true
[core]
	hooksPath = $G/hooks
CFG
hp_git_host=github.com
for pat in "https://github.com/hailports/**" "https://*@$hp_git_host/hailports/**" "git@$hp_git_host:hailports/**" "ssh://git@$hp_git_host/hailports/**"; do
  key="includeIf.hasconfig:remote.*.url:$pat.path"
  git config --global --get "$key" >/dev/null 2>&1 || git config --global "$key" "$INC"
done
echo "installed include $INC and hooks $G/hooks"
