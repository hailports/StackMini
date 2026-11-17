#!/usr/bin/env bash
# Apply a remote-agent patch onto a clean worktree, squash, push branch.
# Does NOT open PRs. Never uses --no-verify.
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
source "$ROOT/lib.sh"

RELGUARD_RELAY_LEDGER="${RELGUARD_RELAY_LEDGER:-$RELGUARD_DATA/relay-ledger.jsonl}"

usage() {
  cat <<'USAGE'
usage: relguard-relay.sh <patch-file|bundle> --story US1234 --team TEAM \
  --desc short-desc --type minor|major|hotfix|maintenance \
  --paths 'path1,path2' [--base main] [--repo DIR] [--destructive-approved] \
  [--remote origin] [--no-push]
USAGE
}

patch="" story="" team="" desc="" typ="" paths_csv="" base="main"
repo="" destructive=0 remote="origin" no_push=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --story) story=$2; shift 2 ;;
    --team) team=$2; shift 2 ;;
    --desc) desc=$2; shift 2 ;;
    --type) typ=$2; shift 2 ;;
    --paths) paths_csv=$2; shift 2 ;;
    --base) base=$2; shift 2 ;;
    --repo) repo=$2; shift 2 ;;
    --remote) remote=$2; shift 2 ;;
    --destructive-approved) destructive=1; shift ;;
    --no-push) no_push=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown arg: $1" ;;
    *)
      if [[ -z "$patch" ]]; then patch=$1; shift; else die "unexpected: $1"; fi
      ;;
  esac
done

[[ -n "$patch" && -n "$story" && -n "$team" && -n "$desc" && -n "$typ" && -n "$paths_csv" ]] || { usage; exit 2; }
[[ -f "$patch" ]] || die "patch/bundle not found: $patch"
case "$typ" in major|minor|hotfix|maintenance) ;; *) die "bad --type" ;; esac

story_digits="$(printf '%s' "$story" | sed -E 's/^US-?//I; s/[^0-9].*$//')"
[[ -n "$story_digits" ]] || die "could not parse story id digits from '$story'"
slug="$(printf '%s' "$desc" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^-+//; s/-+$//; s/-+/-/g')"
[[ -n "$slug" ]] || die "empty --desc slug"
team_tok="$(printf '%s' "$team" | tr -cd 'A-Za-z0-9')"
[[ -n "$team_tok" ]] || die "bad --team"

if [[ "$typ" == "hotfix" && "$story" =~ ^INC-[0-9]+$ ]]; then
  branch="hotfix/$story"
else
  branch="feature/${team_tok}-US${story_digits}-${slug}"
fi
branch_allowed "$branch" || die "computed branch '$branch' fails naming rules"

if [[ -z "$repo" ]]; then
  repo="$(git rev-parse --show-toplevel 2>/dev/null || true)"
fi
[[ -n "$repo" && -d "$repo/.git" ]] || die "need --repo or cwd inside a clone"

IFS=',' read -r -a SCOPE_PATHS <<< "$paths_csv"
normalize_path() { local p="$1"; p="${p#./}"; printf '%s\n' "$p"; }
path_in_scope() {
  local f="$1" s
  f="$(normalize_path "$f")"
  for s in "${SCOPE_PATHS[@]}"; do
    s="$(normalize_path "$s")"
    [[ -z "$s" ]] && continue
    [[ "$f" == "$s" || "$f" == "$s"/* || "$s" == "$f"/* ]] && return 0
  done
  return 1
}

secret_scan_text() {
  if grep -E -i -q \
    'BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY|AWS_SECRET|api[_-]?key\s*[:=]|password\s*[:=]\s*['\''\"][^'\''\"]{8,}|xox[baprs]-|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'; then
    return 1
  fi
  return 0
}

WT=""
cleanup() {
  if [[ -n "${WT:-}" && -d "$WT" ]]; then
    git -C "$repo" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"
  fi
}
trap cleanup EXIT

say "fetch $remote (ff-only baseline)"
git -C "$repo" fetch --quiet "$remote" "$base" 2>/dev/null || git -C "$repo" fetch --quiet "$remote"
base_ref="$remote/$base"
git -C "$repo" rev-parse --verify "$base_ref" >/dev/null 2>&1 || base_ref="$base"
git -C "$repo" rev-parse --verify "$base_ref" >/dev/null 2>&1 || die "baseline ref not found: $remote/$base"

WT="$(mktemp -d "${TMPDIR:-/tmp}/relguard-relay.XXXXXX")"
git -C "$repo" worktree add -q -B "$branch" "$WT" "$base_ref"

apply_artifact() {
  local art="$1"
  case "$art" in
    *.bundle)
      git -C "$WT" bundle verify "$art" >/dev/null
      local tip
      tip="$(git -C "$WT" bundle list-heads "$art" | awk 'NR==1{print $1}')"
      [[ -n "$tip" ]] || die "bundle has no heads"
      git -C "$WT" fetch "$art" "$tip:refs/relguard/bundle-tip"
      if git -C "$WT" merge-base --is-ancestor HEAD refs/relguard/bundle-tip; then
        git -C "$WT" merge --ff-only refs/relguard/bundle-tip
      else
        git -C "$WT" checkout -q "$branch"
        git -C "$WT" read-tree -u -m HEAD refs/relguard/bundle-tip
      fi
      ;;
    *)
      if grep -q '^diff --git ' "$art" && ! grep -q '^From ' "$art"; then
        git -C "$WT" apply --index --whitespace=nowarn "$art" >/dev/null
      else
        git -C "$WT" am --3way --whitespace=nowarn "$art" >/dev/null
      fi
      ;;
  esac
}

apply_artifact "$patch"

CHANGED_FILE="$(mktemp "${TMPDIR:-/tmp}/relguard-changed.XXXXXX")"
{
  git -C "$WT" diff --name-only "$base_ref"...HEAD 2>/dev/null || true
  git -C "$WT" diff --name-only --cached 2>/dev/null || true
  git -C "$WT" ls-files -m 2>/dev/null || true
} | sed '/^$/d' | sort -u > "$CHANGED_FILE"
[[ -s "$CHANGED_FILE" ]] || die "patch produced no file changes"

out_of_scope=""
destructive_hits=""
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  path_in_scope "$f" || out_of_scope="${out_of_scope}${out_of_scope:+ }$f"
  basef="$(basename "$f")"
  case "$basef" in
    destructiveChanges.xml|destructiveChangesPre.xml|destructiveChangesPost.xml|destructiveChanges*.xml)
      destructive_hits="${destructive_hits}${destructive_hits:+ }$f" ;;
  esac
done < "$CHANGED_FILE"
rm -f "$CHANGED_FILE"

[[ -z "$out_of_scope" ]] || die "patch touches paths outside --paths: $out_of_scope"
if [[ -n "$destructive_hits" ]]; then
  [[ "$destructive" -eq 1 ]] || die "destructive metadata present ($destructive_hits); pass --destructive-approved + approval record"
  if ! python3 - "$RELGUARD_APPROVALS" "$story" <<'PY'
import json,sys,glob
adir,story=sys.argv[1:3]
ok=False
for p in glob.glob(adir+"/*.json"):
    try: d=json.load(open(p))
    except Exception: continue
    if str(d.get("story",""))!=story: continue
    if d.get("destructive") is True or "destructive" in str(d.get("recovery_plan","")).lower():
        ok=True; break
sys.exit(0 if ok else 1)
PY
  then
    die "destructive approved flag set but no matching approval record for story $story"
  fi
fi

if ! git -C "$WT" diff "$base_ref" | secret_scan_text; then die "patch looks like it contains secrets"; fi
if ! git -C "$WT" diff --cached | secret_scan_text; then die "index looks like it contains secrets"; fi

git -C "$WT" reset -q --soft "$base_ref"
git -C "$WT" add -A
git -C "$WT" diff --cached --quiet && die "nothing to commit after squash"
msg="${typ}(${story}): ${desc}"
git -C "$WT" -c core.hooksPath="$(git -C "$repo" config --get core.hooksPath 2>/dev/null || echo "$ROOT/hooks")" \
  commit -q -m "$msg"
ncommits="$(git -C "$WT" rev-list --count "$base_ref"..HEAD)"
[[ "$ncommits" == "1" ]] || die "expected 1 commit after squash, got $ncommits"

if [[ "$no_push" -eq 0 ]]; then
  git -C "$WT" push -u "$remote" "HEAD:refs/heads/$branch" >/dev/null
fi

ts="$(iso_now)"
sha="$(git -C "$WT" rev-parse HEAD)"
mkdir -p "$(dirname "$RELGUARD_RELAY_LEDGER")"
python3 -c 'import json,sys; rec={"time":sys.argv[1],"branch":sys.argv[2],"story":sys.argv[3],"team":sys.argv[4],"type":sys.argv[5],"desc":sys.argv[6],"commit":sys.argv[7],"paths":sys.argv[8],"pushed":sys.argv[9]=="1"}; open(sys.argv[10],"a",encoding="utf-8").write(json.dumps(rec,separators=(",",":"))+"\n")' \
  "$ts" "$branch" "$story" "$team_tok" "$typ" "$desc" "$sha" "$paths_csv" "$no_push" "$RELGUARD_RELAY_LEDGER"
printf '%s\n' "$branch"
