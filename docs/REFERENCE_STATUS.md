# Reference deployment status

Snapshot from a live audit on 2026-11-09 (about 4:10 to 4:40 AM US Central). It covers the always-on Mac mini (model, notes brain, gateway, scheduled jobs) and the Linux helper box that reaches it over a userspace tailnet hop. Every row comes from a probe, a process table, a scheduler table, or a recent log. It is a dated snapshot. It does not guarantee health later.

Overall: working, with caveats. The model, the brain query paths, the cloud-agent gateway, and the phone-safe handoff links answer. Several pieces are broken or degraded, and they are listed below.

## Components

| Component | Status | Evidence |
| --- | --- | --- |
| Chat model (llama-server, loopback 18080, proxy 8080) | working | `/health` ok. The IQ3_XS file is loaded with 16384 context and `--n-cpu-moe 0`. Average decode from the `/metrics` counters is about 53.7 tokens/s. |
| Model door on the overlay address (8787) | working | `/health` ok. Paid inference is off. Spend is 0. |
| Embedding server (8097) | working | `/health` ok. All 832,948 vector chunks have embeddings. |
| Brain ask API (8079) and MCP HTTP (8089) | working | Cited hits come back in about 3 to 6 s from the mini and from the helper box over SOCKS. |
| Token gateway for cloud agents (18821) | working | 401 without a token. With a token, `/brain/search` returns hits. Cloud agents used it for health, brain, and staged tracker updates in the last week. |
| Phone-safe handoff confirm link (public HTTPS `/n/{ticket}/{token}`) | working | A bad token gets 404. Real clicks return 302 to the cloud-agent page. Recent handoff mails carry only HTTPS links. Auto model omits `modelDetails`. |
| Mention, prep-index, and SF-bot approve links | broken on cellular | They still default to the overlay-only approve address, which a phone's in-app browser cannot open off the tailnet. |
| Public hostname mapped to the chat proxy | broken (exposure) | The public hostname answers `/health` and `/v1/models` with no auth. See `SECURITY.md`. |
| Social lanes kept off the local model | fixed during audit | Local-first wrappers on PATH routed social `claude -p` prompts to the local model. The lanes now set `LOCAL_FIRST_DONE=1`. |
| Helper box to mini hop | fixed during audit | The ephemeral node was gone (control plane: node not found). Self-heal was stuck because the daemon inherited the ensure lock. Re-login failed because a saved `--ssh` pref was missing from the command. Both are fixed in `contrib/stack-sweep/box/claude-stack/bin/ensure-box-tailnet-hop.sh`. |
| Meetings session bridge (loopback CDP) | caveat | The browser is up, but its keep-alive job is not loaded. The watchdog showed 0 rows, then recovered. |
| Mail session bridge (loopback CDP) | working | The watchdog reads rows. |
| Helper-box bridge browser (loopback CDP) | broken | Nothing is listening. Watchdogs on the mini report it down. |
| Mail ingest | broken (stale) | Sentinel is RED. No mail ingest for about 25 h, and the index reports a metadata error. |
| Vector backfill for tracker, CRM, and chatter | stale | Full-text copies are fresh (same night). Vectors stopped at mid-October because the backfill skips under memory pressure. |
| Calendar blocker and mirror | broken | A Node module (`playwright-core`) is missing from the path the script expects. |
| Synced-drive and warehouse corpora | not built | 0 documents for each. |
| Incremental vector index | caveat | The last run exited with `database is locked`. |
| Memory budget | caveat | 24 GB machine. About 14.5 GB is the model. Free memory is about 15% and swap is about 4.2 of 5.1 GB. The governor skips ingest and nightly jobs with "pressure" or "lane-busy". |
| Legacy overlay proxy map (one port) and legacy service proxy | stale | One map points at a backend that is not listening. The legacy proxy reports all of its old services unreachable. |
| Standing agent bus | not running | There is no process or scheduler entry. That matches the intent: no standing paid-worker ladder. |

## Theory vs practice

- Intent: the local model preps tracker work, then a cloud agent picks it up through the token gateway and finishes it on the tailnet. Practice: this path works end to end. The weak spots are corpus freshness under memory pressure and a few non-HTTPS approve links.
- Intent: the brain is reachable from every tailnet agent. Practice: true for the overlay-bound ports (gateway, ask, MCP, model door). The model, broker, tool server, and browser CDPs are loopback-only. That is by design for the CDPs. It means "everything on the mini" really means the overlay-bound ports, plus overlay SSH where policy allows it.
- Intent: public exposure only for the phone confirm link. Practice: one more hostname exposes the chat proxy. Fix that first.

## How this was checked

Process and port tables. Scheduler (launchd) list with last exit codes and log tails. Health endpoints on the loopback, overlay, and public addresses. Read-only SQLite counts. Recent handoff mail bodies, checked for which link host they carry. Receipts from the local-first router. No model unload, no benchmark, and no restart of a live bridge process.
