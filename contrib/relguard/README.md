# relguard (generic)

Fail-closed git hooks for release branch naming and protected-branch pushes.
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

## Setup

```sh
export RELGUARD_REMOTE_NEEDLE=your-org   # substring matched against remote URLs
git config --global core.hooksPath "$PWD/contrib/relguard/hooks"
# or point an includeIf hooksPath at contrib/relguard/hooks
```

Repo-local hooks still run: set `relguard.chainHooksPath` (default `.git/hooks`).
Prior hook directories can be listed in `RELGUARD_CHAIN_HOOKS` (colon-separated).

Agents must never pass `--no-verify`.
