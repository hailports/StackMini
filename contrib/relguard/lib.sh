#!/usr/bin/env bash
# Generic release-rail helpers. No org-specific names in this file.
# shellcheck shell=bash
set -euo pipefail

RELGUARD_ROOT="$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Remote URL substring that activates the rails. Must be set by the operator.
RELGUARD_REMOTE_NEEDLE="${RELGUARD_REMOTE_NEEDLE:-}"
RELGUARD_DATA="${RELGUARD_DATA:-${HOME}/.relguard}"
RELGUARD_APPROVALS="${RELGUARD_APPROVALS:-$RELGUARD_DATA/approvals}"
RELGUARD_LEDGER="${RELGUARD_LEDGER:-$RELGUARD_DATA/deploy-ledger.jsonl}"
RELGUARD_SYNC_DUE="${RELGUARD_SYNC_DUE:-$RELGUARD_DATA/sync-pr-due.jsonl}"
RELGUARD_RELAY_LEDGER="${RELGUARD_RELAY_LEDGER:-$RELGUARD_DATA/relay-ledger.jsonl}"
RELGUARD_VIOLATIONS="${RELGUARD_VIOLATIONS:-$RELGUARD_DATA/violations.jsonl}"
RELGUARD_NEEDS_OWNER="${RELGUARD_NEEDS_OWNER:-$RELGUARD_DATA/needs_owner.jsonl}"
RELGUARD_WATCH_STATE="${RELGUARD_WATCH_STATE:-$RELGUARD_DATA/watch-state.json}"
RELGUARD_PROD_ALIASES="${RELGUARD_PROD_ALIASES:-$RELGUARD_ROOT/prod-aliases.conf}"

if [[ -f "${HOME}/.config/git/relguard-chain.env" ]]; then
  # shellcheck disable=SC1090
  . "${HOME}/.config/git/relguard-chain.env"
fi
if [[ -z "${RELGUARD_CHAIN_HOOKS:-}" ]]; then
  _prior="$(git config --global --get relguard.priorHooksPath 2>/dev/null || true)"
  [[ -n "$_prior" ]] && RELGUARD_CHAIN_HOOKS="$_prior"
  unset _prior
fi

die() { echo "relguard: BLOCKED: $*" >&2; exit 1; }
say() { echo "relguard: $*" >&2; }

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

worktree_clean() {
  git diff --quiet && git diff --cached --quiet
}

deploy_key_for() {
  local story="$1" commit="$2" target="$3"
  printf '%s' "${story}|${commit}|${target}" | shasum -a 256 | awk '{print $1}'
}

load_prod_aliases() {
  local line
  PROD_ALIASES=()
  if [[ -f "$RELGUARD_PROD_ALIASES" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" || "$line" == \#* ]] && continue
      PROD_ALIASES+=("$line")
    done < "$RELGUARD_PROD_ALIASES"
  fi
}

is_prod_target() {
  local t="${1:-}" tl
  [[ -z "$t" ]] && return 1
  tl="$(printf '%s' "$t" | tr '[:upper:]' '[:lower:]')"
  load_prod_aliases
  local a
  for a in "${PROD_ALIASES[@]:-}"; do
    [[ "$(printf '%s' "$a" | tr '[:upper:]' '[:lower:]')" == "$tl" ]] && return 0
  done
  if [[ "$t" == *@* ]]; then
    local host="${t##*@}"
    host="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
    case "$host" in
      *.partial|*.full|*.dev|*.uat|*.sit|*.qa|*.sandbox) return 1 ;;
    esac
    if [[ "$host" == *.com && "$host" != *.*.* ]]; then
      return 0
    fi
  fi
  [[ "$tl" == *prod* ]] && return 0
  return 1
}

append_jsonl() {
  local file="$1" json="$2"
  mkdir -p "$(dirname "$file")"
  printf '%s\n' "$json" >> "$file"
}

iso_now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

chain_hook() {
  local name="$1"; shift
  local stdin_file="${RELGUARD_HOOK_STDIN:-}"
  local dir hook
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
  local g c
  g="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || git rev-parse --git-common-dir)" || return 0
  c="$(git config --get relguard.chainHooksPath 2>/dev/null || echo "$g/hooks")"
  case "$c" in
    /*) ;;
    *)
      local top
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
