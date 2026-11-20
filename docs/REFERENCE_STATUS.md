# Reference deployment status

Snapshot from a live audit on 2026-11-09 (about 4:10 to 4:40 AM US Central). It covers the always-on Mac mini (model, notes brain, gateway, scheduled jobs) and the Linux helper box that reaches it over a userspace tailnet hop. Every row comes from a probe, a process table, a scheduler table, or a recent log. It is a dated snapshot. It does not guarantee health later.

Update the same morning (about 4:55 to 5:20 AM): the public chat-proxy hostname is closed. Mail ingest, the calendar jobs, and the meetings-bridge keep-alive are fixed. Memory pressure is now the main blocker.

Overall at audit time: working, with caveats. The model, the brain query paths, the cloud-agent gateway, and the phone-safe handoff links answer. Several pieces are broken or degraded, and they are listed below.

## Components

| Component | Status | Evidence |
| --- | --- | --- |
| Chat model (loopback 18080, proxy 8080) | working (updated 2026-11-19) | Since 2026-11-19 the server is Qwen3.6-35B-A3B UD-Q2_K_XL on Splash behind a llama-compatible shim, 65536 context, vision on. Median real-traffic decode 71.9 tokens/s the same day (was 45.9 on the previous MiniCPM-V IQ3_XS server, which had 16384 context; at this audit its `/metrics` average was about 53.7 tokens/s). See `MODEL.md`. |
| Model door on the overlay address (8787) | working | `/health` ok. Paid inference is off. Spend is 0. |
| Embedding server (8097) | working | `/health` ok. All 832,948 vector chunks have embeddings. |
| Brain ask API (8079) and MCP HTTP (8089) | working | Cited hits come back in about 3 to 6 s from the mini and from the helper box over SOCKS. |
| Token gateway for cloud agents (18821) | working | 401 without a token. With a token, `/brain/search` returns hits. Cloud agents used it for health, brain, and staged tracker updates in the last week. |
| Phone-safe handoff confirm link (public HTTPS `/n/{ticket}/{token}`) | working | A bad token gets 404. Real clicks return 302 to the cloud-agent page. Recent handoff mails carry only HTTPS links. Auto model omits `modelDetails`. |
| Mention, prep-index, and SF-bot approve links | fixed after audit | They now default to the public HTTPS host. The public app proxies only the same signed paths. A missing or malformed signature returns 404 before the approve server is contacted, and a forged token is refused on POST (400). A test no-op approval completed its round trip publicly. Ticket progress mails are now sent only when they carry a valid `/n/` confirm link. |
| Public hostname mapped to the chat proxy | fixed after audit | The DNS record was deleted, and the tunnel ingress rule was removed (tunnel config version 20). Public DNS returns NXDOMAIN. Other tunnel hosts still answer. |
| Social lanes kept off the local model | fixed during audit | Local-first wrappers on PATH routed social `claude -p` prompts to the local model. The lanes now set `LOCAL_FIRST_DONE=1`. |
| Helper box to mini hop | fixed during audit | The ephemeral node was gone (control plane: node not found). Self-heal was stuck because the daemon inherited the ensure lock. Re-login failed because a saved `--ssh` pref was missing from the command. Both are fixed in the helper hop script. |
| Meetings session bridge (loopback CDP) | fixed after audit | The keep-alive job is loaded through a guard that only waits while the port is live. It launches the browser only after two misses in a row. The live browser was not restarted. |
| Mail session bridge (loopback CDP) | working | The watchdog reads rows. |
| Helper-box bridge browser (loopback CDP) | caveat | It was restarted through its own guard. The chat page renders blank: no app root content and no sign-in form. The owner needs to look. |
| Mail ingest | fixed after audit | Cause: the mail reader loaded `playwright-core` from a deleted path. After the fix, a run brought in 38 metadata rows and 12 bodies with no error. The sentinel is still RED, but only for the session-insight lane. |
| Vector backfill for tracker, CRM, and chatter | stale (waiting on memory) | No stale embeddings are pending. New rows wait for the nightly backfill, which skips under memory pressure. Newest vectors are still from mid-October. |
| Calendar blocker and mirror | fixed after audit | Same dead path, fixed in 9 tools. The mirror ran: 3 created, 1 updated. The blocker dry run plans 11 blocks. |
| Synced-drive and warehouse corpora | synced drive fixed 2026-11-20; warehouse not built | Cause: jobs started by launchd or ssh inherit an I/O policy that forbids downloading cloud-only placeholders, so reads failed with EDEADLK. The jobs now turn that policy on for themselves (`contrib/synced-drive/materialize.py`). The cycle and the nightly document refresh both exit 0. The warehouse has a query tool but no ingest source. |
| Incremental vector index | fixed 2026-11-20 | It exited 1 when another index pass held the lock, or when a few documents were left that no lane could embed. Lock contention now waits (or exits 0 for a single pass), and a small residual is logged instead of failing. The index is caught up. |
| Memory budget | caveat | 24 GB machine. About 14.5 GB was the model at audit time (12.3 GB weights plus a 0.9 GB projector and a 1.05 GB draft since 2026-11-19). Free memory is about 15% and swap is about 4.2 of 5.1 GB. The governor skips ingest and nightly jobs with "pressure" or "lane-busy". |
| Legacy overlay proxy map (one port) and legacy service proxy | stale | One map points at a backend that is not listening. The legacy proxy reports all of its old services unreachable. |
| Document-share crawl retry | fixed 2026-11-20 (draining) | A stale sign-in alert stayed set after sign-in recovered, so every crawl skipped. The crawl now runs a one-row probe search and clears the alert when it succeeds. First run ingested 77 files; about 680 are still queued for the 20-minute drain. |
| Repo-sync job | fixed after audit | The governor no longer pauses this job mid-run, and the job retries a command once if it was paused. One run finished with exit 0. Publish stays held. |
| Overlay public share on the brain MCP port | fixed after audit | Nothing depended on it. It is now a tailnet-only share, and the overlay health check still answers. |
| Session-insight sentinel | caveat | It was RED because no transcript had passed the writer's minimum size. The writer reports 0 new and 0 unreachable, and the check now treats that as quiet-confirmed instead of a stall. |
| Standing agent bus | not running | There is no process or scheduler entry. That matches the intent: no standing paid-worker ladder. |

## Theory vs practice

- Intent: the local model preps tracker work, then a cloud agent picks it up through the token gateway and finishes it on the tailnet. Practice: this path works end to end. The weak spots are corpus freshness under memory pressure and a few non-HTTPS approve links.
- Intent: the brain is reachable from every tailnet agent. Practice: true for the overlay-bound ports (gateway, ask, MCP, model door). The model, broker, tool server, and browser CDPs are loopback-only. That is by design for the CDPs. It means "everything on the mini" really means the overlay-bound ports, plus overlay SSH where policy allows it.
- Intent: public exposure only for the phone confirm link. Practice: one more hostname exposes the chat proxy. Fix that first.

## How this was checked

Process and port tables. Scheduler (launchd) list with last exit codes and log tails. Health endpoints on the loopback, overlay, and public addresses. Read-only SQLite counts. Recent handoff mail bodies, checked for which link host they carry. Receipts from the local-first router. No model unload, no benchmark, and no restart of a live bridge process.
