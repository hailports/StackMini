# relguard (generic)

Fail-closed git hooks for release branch naming and protected-branch pushes,
plus a patch relay for cloud agents and a private read-only watcher.

Configure the remote needle before use. This tree holds no org names, aliases,
usernames, home paths, or host IPs.

## Branch patterns

- `feature/<token>-US<digits>-<slug>` or `feature/US-<digits>-<slug>`
- `release/YYYY.MM.DD`
- `hotfix/INC-<digits>`
- `sync/production-YYYYMMDD`

## Protected remotes

Pushes to `main` / `release/*` are refused (PRs only). Force-pushes to those
refs are refused. Commit hooks refuse commits on `main` / `release/*`.

## Relay

Cloud agents produce a patch/bundle only (see `CLOUD_AGENT_RULES.md`). Apply:

```sh
./relguard-relay.sh work.patch \
  --story US1234 --team TEAM --desc widget --type minor \
  --paths 'src,docs' --repo /path/to/clone
```

Prints the branch name. Does not open a PR.

## Watcher

`watch.sh` is read-only. Set `RELGUARD_REPOS_CONF` (org/name lines) and run on
a timer. Alerts go to local jsonl + optional macOS notification only.

## Setup

```sh
export RELGUARD_REMOTE_NEEDLE=your-org   # substring matched against remote URLs
git config --global core.hooksPath "$PWD/contrib/relguard/hooks"
```

Agents must never pass `--no-verify`.
