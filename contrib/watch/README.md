# Mini watch framework

One launchd label per routine: `com.claude-stack.watch.<name>` (or
`com.stackmini.watch.<name>` on a stock install). The runner calls a check
script on an interval, asks the local model for a cheap yes/no, dedupes, and
only writes an alert when something needs a human or a bot.

It never sends email or chat. External messaging is out of scope.

## Layout

```
contrib/watch/
  runner.py                 # generic tick
  install.sh                # drop a plist from the template
  template.plist            # StartInterval + runner argv
  examples/
    local-model-ready.sh    # probe :8080 health
    premium-burn-pulse.py   # rollup pulse for bots
```

Alerts land in `$WATCH_ALERT_DIR/<name>.jsonl` (default
`~/.local/share/local-first/watch-alerts/`). When a bot must act, the runner
calls `$WATCH_NOTIFY_CMD` with one JSON object on stdin (the agent-bus /
box notify path on the reference box).

## Port a Grok Bot routine (3-line recipe)

```sh
# 1) drop a check that prints JSON: {"need":true|false,"summary":"...","bot":"stack"|null}
printf '%s\n' '{"need":false,"summary":"quiet","bot":null}' > ~/watches/my-check.sh && chmod +x ~/watches/my-check.sh
# 2) install the plist (interval seconds)
sh contrib/watch/install.sh my-check ~/watches/my-check.sh 300
# 3) kick once
launchctl kickstart -k "gui/$(id -u)/com.claude-stack.watch.my-check"
```

Keep social content off the local model. Use the paid path for that lane.

## Check script contract

Stdout must be one JSON object:

| field     | type    | meaning                                      |
|-----------|---------|----------------------------------------------|
| `need`    | bool    | true only when a human or bot should act     |
| `summary` | string  | short, no secrets                            |
| `bot`     | string/null | which bot should wake (or null for human) |
| `dedupe`  | string  | optional stable key; default = summary hash  |

Exit 0 always for a successful probe. Non-zero exits are logged and skipped.

Watches named in `WATCH_JUDGE_ON_CHANGE` (comma list, default `local-model-ready`) call the
local judge only when the check says something needs action, asks to force, or its result changed
since the last run. A green result that matches the last run makes no model call.
