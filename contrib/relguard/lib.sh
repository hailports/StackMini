#!/usr/bin/env bash
# Generic release-rail helpers. No org-specific names in this file.
# shellcheck shell=bash
set -euo pipefail

RELGUARD_ROOT="$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Remote URL substring that activates the rails. Must be set by the operator.
RELGUARD_REMOTE_NEEDLE="${RELGUARD_REMOTE_NEEDLE:-}"

die() { echo "relguard: BLOCKED: $*" >&2; exit 1; }

is_org_remote_url() {
  [[ -n "${RELGUARD_REMOTE_NEEDLE}" ]] || return 1
  printf '%s\n' "${1:-}" | tr '[:upper:]' '[:lower:]' | grep -F -q "$(printf '%s' "$RELGUARD_REMOTE_NEEDLE" | tr '[:upper:]' '[:lower:]')"
}

repo_is_org_clone() {
  local url
  url="$(git remote get-url --push origin 2>/dev/null || true)"
  if is_org_remote_url "$url"; then return 0; fi
  while IFS= read -r url; do
    is_org_remote_url "$url" && return 0
  done < <(git remote 2>/dev/null | while read -r r; do git remote get-url --push "$r" 2>/dev/null; done)
  return 1
}

branch_allowed() {
  local b="${1:-}"
  [[ "$b" =~ ^feature/[A-Za-z0-9]+-US[0-9]+-[A-Za-z0-9._-]+$ ]] && return 0
  [[ "$b" =~ ^feature/US-[0-9]+-[A-Za-z0-9._-]+$ ]] && return 0
  [[ "$b" =~ ^release/[0-9]{4}\.[0-9]{2}\.[0-9]{2}$ ]] && return 0
  [[ "$b" =~ ^hotfix/INC-[0-9]+$ ]] && return 0
  [[ "$b" =~ ^sync/production-[0-9]{8}$ ]] && return 0
  return 1
}

branch_is_protected_target() {
  local b="${1:-}"
  [[ "$b" == "main" || "$b" == "master" ]] && return 0
  [[ "$b" == release/* ]] && return 0
  return 1
}

branch_is_commit_blocked() {
  branch_is_protected_target "$@"
}

current_branch() {
  git rev-parse --abbrev-ref HEAD 2>/dev/null || echo ""
}

chain_hook() {
  local name="$1"; shift
  local stdin_file="${RELGUARD_HOOK_STDIN:-}"
  local dir hook g c self top

  if [[ -n "${RELGUARD_CHAIN_HOOKS:-}" ]]; then
    IFS=':' read -r -a _dirs <<< "$RELGUARD_CHAIN_HOOKS"
    for dir in "${_dirs[@]}"; do
      [[ -z "$dir" ]] && continue
      hook="$dir/$name"
      if [[ -x "$hook" ]]; then
        if [[ -n "$stdin_file" ]]; then
          "$hook" "$@" < "$stdin_file" || return 1
        else
          "$hook" "$@" || return 1
        fi
      fi
    done
  fi

  g="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || git rev-parse --git-common-dir)" || return 0
  c="$(git config --get relguard.chainHooksPath 2>/dev/null || echo "$g/hooks")"
  case "$c" in
    /*) ;;
    *)
      top="$(git rev-parse --show-toplevel)" || return 0
      c="$top/$c"
      ;;
  esac
  self="$(CDPATH= cd -- "$RELGUARD_ROOT/hooks" && pwd)"
  if [[ "$c" != "$self" && -x "$c/$name" ]]; then
    if [[ -n "$stdin_file" ]]; then
      "$c/$name" "$@" < "$stdin_file" || return 1
    else
      "$c/$name" "$@" || return 1
    fi
  fi
  return 0
}
