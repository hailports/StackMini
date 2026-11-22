# hailports.com dated-section refresh

Box-side tools that keep the public hailports.com work ledger, live board,
findings, changelog and X cards up to date. They run on the hailports box,
not inside a StackMini checkout on a laptop.

## Schedule

`live-export-loop.sh` is kept running by the existing social supervisor and
calls `refresh_site_data.sh` every 15 minutes. That script:

1. Ensures `Host mini` in `~/.ssh/config` uses the SOCKS hop from the
   nightly peer file (`ensure_mini_ssh_config.sh`). It never prints the user
   or host.
2. Pulls a generic work-ledger snapshot from the Mini (`activity.py`).
3. Refreshes public commits, the X card strip, findings and the live board.
4. Deploys through the site's guarded `deploy.sh` only when the data changed.

Failures are appended to `$HOME/social/site/refresh.log` and also to
`refresh.fail.log`. A content-refresh error no longer skips the live-stats
deploy.

A cron package was not available on the box used for this change, so
`crontab.example` is included as a backup timer if cron is added later.

## Freshness

Each dated section is stamped with `data-as-of`. If a pull is more than 48
hours old, the section is marked stale and its figures are hidden rather than
shown as current. The work ledger is reduced to generic task types only: no
names, companies, subjects or ids.

## Safety

The public-content deny list now requires a trailing word boundary on ticket
id patterns such as `AB123`, so a public commit SHA starting with `ab87` no
longer aborts the refresh.

Mini connection details stay in the nightly peer file. They do not belong in
these scripts.

`activity.py` and `refresh_public_content.py` stay on the hailports box. They
include private-source filter lists and are not copied into this public repo.

## List-price equivalent

The live-board COST card is computed on each refresh, not stored as a fixed
figure. `build_live_stats.py` sums every dated day of logged local-model
prompt and generated tokens: Oct 17–Nov 1 from `local_token_history.json`
(the timing-log daily rollup used in the Nov 3, 2026 cost write-up) and
later days from hp_daily `local_tokens_today` facts (`/metrics` deltas).
That load is priced at the same Claude Sonnet 5.5 standard list rates as
the would-be spend tiles ($2 / $10 per 1M tokens, no cache or batch
discount) and scaled to 30 days. Today counts as the elapsed fraction of
the collection day. Days with no log are omitted, not zero-filled, and
named in the card subline. `stats.json.list_price_equivalent` carries the
totals, day weight, gaps and formula. On the hailports box the same
history file also lives at `site/tools/local_token_history.json`.
