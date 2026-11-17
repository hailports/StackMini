# Cloud / remote agent rules — paste into the agent prompt

Hard limits for cloud or remote agents working on protected org repos:

1. **Do not push** to GitHub. Do not open PRs, comments, labels, reviews, or rulesets.
2. **Do not** run prod deploys. Do not invent approval records.
3. Produce a **git patch artifact** only:
   - `git format-patch origin/<base> --stdout > work.patch`, or
   - `git bundle create work.bundle <base>..<tip>`
4. In the handoff, list:
   - **story id** (e.g. `US1234`)
   - **team** token for the branch name
   - **short desc** (slug)
   - **type** (`minor` | `major` | `hotfix` | `maintenance`)
   - **paths** the change is allowed to touch (comma-separated scope list)
5. A human (or local `relguard-relay.sh`) applies the patch, pushes the branch, and opens the PR.

Never pass `--no-verify`. Never force-push. Never commit on `main` / `release/*`.
