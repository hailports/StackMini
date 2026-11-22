# job_harness

Contract, progress file, and wrapper-owned exits for long-running agent jobs. Stdlib Python.
The wrapper, not the prompt, decides when a job is done.

```
job-run new <name>       scaffold CONTRACT.md PROGRESS.md prompt.md done_when.sh
job-run check <name>     validate CONTRACT.md (refuses missing fields, uncheckable done_when, no budget)
job-run run <name>       run under the wrapper (foreground); start = same in tmux
job-run run <name> --judge "<cmd>"   separate judge must pass too
job-run handoff <name>   validate HANDOFF.md
```

Jobs live in `$JOB_HARNESS_JOBS_ROOT/<name>/` (default `~/jobs`).

## Contract
Required: `goal output scope never done_when budget`. `done_when` line 1 is a command that can exit non-zero.
`budget` needs `max_wall_s` and `max_attempts` or `max_tokens` (estimate, chars/4); `max_same_error` defaults to 2.
The default never-list is provided by the host config (`$JOB_HARNESS_JOBS_ROOT/_templates/host_defaults.json`,
see `templates/host_defaults.example.json`). Host nevers missing from a contract are injected and recorded in AUDIT.log.

## Exits
| condition | result |
|---|---|
| done_when rc 0 AND HANDOFF.md valid with STATUS complete | DONE.json, exit 0 |
| same error signature N times | escalate, exit 3 |
| budget spent (wall, attempts, tokens) | escalate, exit 3 |
| protected path changed during the run | escalate, exit 3 |
| HANDOFF STATUS blocked | escalate, exit 3 |
| contract missing or incomplete | refuse, exit 2 |

Escalate writes `NEEDS_HUMAN` (name via `JOB_HARNESS_HUMAN_MARKER`) and `ESCALATED` in the job folder,
appends a line to `_escalated/INDEX.md`, and notes the stop in PROGRESS.md.

## HANDOFF.md
Exact headings: TASK, STATUS (complete | partial | blocked), OUTPUT (path; must exist when complete),
SOURCES, NEXT OWNER, DECISIONS, UNCERTAINTIES, DO-NOT-ASSUME. Missing or invalid = not done.

## Traces
AUDIT.log (jsonl), DONE_WHEN.log, attempt-N.agentK.log, .prompt.attempt-N.md, JUDGE.log.

## Agents
Agent chain comes from the host config `agents` (argv lists, tried in order until one exits 0), a contract
`runner`, an existing `run.sh`, or `JOB_HARNESS_AGENT_CMD`. `{prompt_file}` in argv is replaced with the
assembled prompt path; otherwise the prompt text is appended. The prompt carries CONTRACT, PROGRESS,
previous HANDOFF, and the last failure, so a fresh session resumes from files.

Tests: `python3 -m pytest -q tests/test_job_harness.py`
