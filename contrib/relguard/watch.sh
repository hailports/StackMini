#!/usr/bin/env bash
# Private read-only watcher. Alerts only to local files + optional macOS notify.
# Configure: RELGUARD_GH_ORG, RELGUARD_REPOS_CONF, RELGUARD_REMOTE_NEEDLE.
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
source "$ROOT/lib.sh"

RELGUARD_VIOLATIONS="${RELGUARD_VIOLATIONS:-$RELGUARD_DATA/violations.jsonl}"
RELGUARD_NEEDS_OWNER="${RELGUARD_NEEDS_OWNER:-$RELGUARD_DATA/needs_owner.jsonl}"
RELGUARD_WATCH_STATE="${RELGUARD_WATCH_STATE:-$RELGUARD_DATA/watch-state.json}"
RELGUARD_REPOS_CONF="${RELGUARD_REPOS_CONF:-$ROOT/watch-repos.conf}"
QUIET_CLEAN="${RELGUARD_WATCH_QUIET:-1}"

in_window_ct() {
  python3 - <<'PY'
import datetime
try:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Chicago")
except Exception:
    tz = datetime.timezone(datetime.timedelta(hours=-6))
now = datetime.datetime.now(tz)
if now.weekday() >= 5:
    raise SystemExit(1)
if not (6 <= now.hour < 22):
    raise SystemExit(1)
raise SystemExit(0)
PY
}

if [[ "${RELGUARD_WATCH_FORCE:-0}" != "1" ]]; then
  in_window_ct || exit 0
fi
renice 15 $$ >/dev/null 2>&1 || true
mkdir -p "$RELGUARD_DATA"
touch "$RELGUARD_VIOLATIONS" "$RELGUARD_NEEDS_OWNER"
[[ -f "$RELGUARD_WATCH_STATE" ]] || echo '{}' > "$RELGUARD_WATCH_STATE"

load_repos() {
  REPOS=()
  if [[ -f "$RELGUARD_REPOS_CONF" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" || "$line" == \#* ]] && continue
      REPOS+=("$line")
    done < "$RELGUARD_REPOS_CONF"
  fi
}

emit_alerts_from_tsv() {
  local kind detail
  while IFS=$'\t' read -r kind detail; do
    [[ -z "${kind:-}" ]] && continue
    alert "$kind" "$detail"
  done
}

alert() {
  local kind="$1" detail="$2"
  python3 - "$RELGUARD_WATCH_STATE" "$RELGUARD_VIOLATIONS" "$RELGUARD_NEEDS_OWNER" "$kind" "$detail" <<'PY'
import json, sys, datetime, hashlib, subprocess
state_path, viol_path, needs_path, kind, detail = sys.argv[1:6]
now = datetime.datetime.now(datetime.timezone.utc)
fp = hashlib.sha256(f"{kind}|{detail}".encode()).hexdigest()[:16]
try:
    state = json.load(open(state_path))
except Exception:
    state = {}
alerts = state.setdefault("alerts", {})
prev = alerts.get(fp)
if prev:
    try:
        pts = datetime.datetime.fromisoformat(prev.replace("Z", "+00:00"))
        if now - pts < datetime.timedelta(hours=24):
            sys.exit(0)
    except Exception:
        pass
rec = {"time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": kind, "detail": detail, "fp": fp}
open(viol_path, "a", encoding="utf-8").write(json.dumps(rec, separators=(",", ":")) + "\n")
open(needs_path, "a", encoding="utf-8").write(json.dumps({"id": f"relguard:{fp}", "kind": "relguard", "what": f"{kind}: {detail}", "since": rec["time"]}, separators=(",", ":")) + "\n")
alerts[fp] = rec["time"]
state["alerts"] = alerts
state["last_run"] = rec["time"]
json.dump(state, open(state_path, "w"), indent=2)
try:
    subprocess.run(["osascript", "-e", f'display notification {json.dumps((kind+": "+detail)[:180])} with title "relguard"'], check=False, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except Exception:
    pass
PY
}

load_repos
[[ ${#REPOS[@]} -gt 0 ]] || { say "no repos in watch-repos.conf; skip"; exit 0; }
GH_USER="$(gh api user --jq .login 2>/dev/null || true)"
[[ -n "$GH_USER" ]] || { say "gh not authed; skip"; exit 0; }

for repo in "${REPOS[@]}"; do
  prs="$(gh pr list -R "$repo" --author "@me" --state open --limit 40 --json number,headRefName,baseRefName,commits 2>/dev/null || echo '[]')"
  python3 - "$repo" "$prs" <<'PY' | emit_alerts_from_tsv
import json,sys,re
repo,prs=sys.argv[1],json.loads(sys.argv[2])
pats=[r"^feature/[A-Za-z0-9]+-US[0-9]+-[A-Za-z0-9._-]+$",r"^feature/US-[0-9]+-[A-Za-z0-9._-]+$",r"^release/[0-9]{4}\.[0-9]{2}\.[0-9]{2}$",r"^hotfix/INC-[0-9]+$",r"^sync/production-[0-9]{8}$"]
for pr in prs:
  head=pr.get("headRefName") or ""; base=pr.get("baseRefName") or ""; num=pr.get("number")
  n=len(pr.get("commits") or [])
  if not any(re.match(p, head) for p in pats):
    print(f"bad_branch_name\t{repo}#{num} head={head}")
  if base in ("main","master") and not (head.startswith("release/") or head.startswith("hotfix/INC-") or head.startswith("sync/production-")):
    print(f"pr_to_main_bad_source\t{repo}#{num} {head} -> {base}")
  if n > 1:
    print(f"pr_multi_commit\t{repo}#{num} commits={n}")
PY
done

python3 - "$RELGUARD_WATCH_STATE" <<'PY'
import json,sys,datetime
p=sys.argv[1]
try: state=json.load(open(p))
except Exception: state={}
state["last_run"]=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump(state, open(p,"w"), indent=2)
PY
[[ "$QUIET_CLEAN" == "1" ]] || say "watch ok"
exit 0
