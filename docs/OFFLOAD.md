# Local-first offload

Cheap CLI turns finish on the local model door. Building, multi-file work,
tool-heavy agent loops, and social content always escalate to the paid CLI.

## Classify

| class   | examples                                              | route |
|---------|-------------------------------------------------------|-------|
| simple  | summarize, classify, triage, extract, rewrite, commit msg, grep-explain | local |
| build   | implement, scaffold, multi-file, agent loop           | paid  |
| other   | unsure / high-stakes non-build                        | paid  |

Fail-open: if the local model is down, the door escalates with a short brief.

## Wire CLIs

```sh
sh contrib/local-first/install-local-first-wrappers
```

Work laptop that SSHes to the model host:

```sh
MINI_SSH_TARGET=user@host sh contrib/local-first/install-work-mac-wrappers
```

Gateway (OpenAI `/v1/chat/completions` + Anthropic `/v1/messages`, loopback only):

```sh
python3 hailports/local_first.py --gateway-serve --port 8790
```

Decisions: `LOCAL_FIRST_DECISIONS` (default `~/.local/share/local-first/router-decisions.jsonl`).
Daily rollup: `LOCAL_FIRST_ROLLUP` (default `~/.local/share/local-first/offload-today.md`).

## Watch framework

See `contrib/watch/README.md`. Label pattern: `com.claude-stack.watch.<name>`.
Ported example: `examples/chat-probe-local.py` replaces a paid HTTP health probe.

## Saturate update (2026-11-21)

- Task-type heuristics route more simple paid-CLI asks to the local model.
- Local prompt budget raised for 64K context; malformed or low-confidence local output falls back to paid.
- Fail-open to paid when `:8080` is down. Build/social still never finish local.
- Optional `com.stackmini.local-precompute` launchd example digests idle context packs and backs off under memory/swap pressure.


## Jev local judgments (saturate2 2026-11-21)

Non-social Jev can try local :8080 first (strict JSON, 2-sample consistency, confidence floor) and fall back to TypeSafe paid. Social lanes stay on paid. Kill switch file: `$CLAUDE_STACK_HOME/data/flags/jev_local_off` (or `JEV_LOCAL=0`). Every attempt logs to router-decisions.jsonl.

## Never-idle engine (2026-11-21)

`hailports/never_idle.py` is the public-safe queue + backoff helper for a
KeepAlive LaunchAgent that keeps a local chat server busy on a bottomless
work-lane precompute queue (fresh inbound/pre-drafts, context packs, ADO prep,
SF atlas + findings, account health, daily/weekly work products, history
synthesis, refresh). Low-watermark refill (~50) keeps depth from hitting 0;
fresh items outrank backlog. It yields to interactive traffic, honors the
mem-governor throttle file, applies a rock-solid quality gate (grounding,
self-critique, gold regression, capped paid fallback), tracks usefulness
reads, and pauses a producer when bad rate exceeds 20%. Generated packs, the
SQLite DB, draft patches, and few-shot banks stay gitignored and local.
Example label: `com.stackmini.never-idle`.

