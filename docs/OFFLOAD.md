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
