# Sprint prep

Read-only prep cards for the open items in one iteration. The local model writes each card. Nothing is posted and nothing is sent. A suggested state is only a suggestion.

A draft note is kept only when it is short, lowercase, and the comment gate accepts it. Ticket text is passed as data.

## Run

Set `ADO_PREP_ITERATION` to the iteration path and `ADO_PREP_STACK` to the stack root. The stack root is the directory that contains `tools/` and `data/`.

```sh
python3 hailports/sprint_prep.py
```

Outputs, under `data/ado_prep/`:

- `YYYY-MM-DD.json` and a `latest.json` link
- `YYYY-MM-DD-top10.md`
- `meter.jsonl` with local prompt and completion tokens, wall time, and hosted tokens avoided

Formula recorded on each meter row: hosted tokens avoided equals local prompt tokens plus local completion tokens. One local token replaces one hosted token for the same packet. Skipped calls add 0. There is no multiplier.

If the tracker says the session needs an SSO remint, the run logs that and exits. It does not remint and it does not restart the browser.

## Schedule

Weekdays at 06:30 and 12:00 local time. `contrib/sprint_prep/sprint-prep.plist.example` is a launchd template. The script takes an exclusive lock, so a second start exits while the first is still running.

Copy an audit note to `data/ado_prep/context/*.md` when you have one. The run also reads meeting and mail digests that are already on disk.

## Yield

Each model call is a background call. If priority work is in flight, the run waits and then retries. A busy slot that is other background work is queued behind. The run does not unload or swap the model.

An unfinished day file (status still running) is resumed. Cards that already have a summary are not sent to the model again. A finished day file is prepped again.

## Notes

A later note step may mail one note per card. It mails a card only when it is in the current iteration, System.AssignedTo is the configured owner or blocking_who names that owner, and the state is still open. Testing, resolved, closed, done, and removed are not open. The next-step sentence does not count. If that step would send more than 25 notes, it sends none.
