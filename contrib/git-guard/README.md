# git-guard

Blocks a push to a `github.com/hailports/*` remote when any outgoing commit's
author, committer, tagger, or trailer (Signed-off-by, Co-authored-by, ...) is not
`HailPorts <noreply@hailports.com>`, or when a commit message or added line holds
a private term, a tailnet IP, a LAN hostname, a home path, or a gitleaks finding.
It fails closed.

    sh contrib/git-guard/install.sh

adds a global `includeIf "hasconfig:remote.*.url:..."` that forces the identity
and points `core.hooksPath` at `hooks/` for every hailports clone. Other hooks
chain to the repo's own (`hailports-guard.chainHooksPath`, default `.git/hooks`).
Private terms go in `~/.config/hailports-guard/deny.txt` (substrings) and
`deny-words.txt` (whole words), never in the repo. There is no inline allow marker.

`scripts/stack-change.sh` runs `hailports-guard verify` before it pushes, so
`--no-verify` does not skip it, and a CI job can run
`hailports-guard audit` on every push and pull request.
